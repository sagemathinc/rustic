"""Restore scenarios.  Each returns a list of problems; empty means it passed."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess

from .fixture import build
from .manifest import compare, manifest

SPARSE = ["--sparse", "by-content-required"]


class RusticFailed(Exception):
    pass


class Context:
    def __init__(self, binary: str, work: Path, as_root: bool):
        self.work = work
        self.as_root = as_root
        self.source = work / "source"
        self.common = [binary, "--repository", str(work / "repo"), "--no-progress"]
        self.env = {**os.environ, "RUSTIC_PASSWORD": "fixture-only", "LC_ALL": "C.UTF-8"}
        self.skipped: list[str] = []
        self.snapshot = ""
        self.want: dict = {}

    def run(self, *args, cwd=None) -> bytes:
        result = subprocess.run(
            [*self.common, *args], cwd=cwd or self.work, env=self.env, capture_output=True, timeout=1200
        )
        if result.returncode:
            tail = result.stderr.decode(errors="replace")[-2000:]
            raise RusticFailed(f"rustic {' '.join(map(str, args))} exited {result.returncode}: {tail}")
        return result.stdout

    def backup(self, source: Path, *args) -> str:
        return json.loads(self.run("backup", "--json", *args, ".", cwd=source))["id"]

    def fresh(self, name: str) -> Path:
        path = self.work / name
        path.mkdir()
        return path

    def restore(self, what: str, target: Path, *args) -> dict:
        self.run("restore", "--numeric-id", *args, what, str(target))
        return manifest(target)


def setup(ctx: Context) -> None:
    ctx.source.mkdir()
    ctx.skipped = build(ctx.source, ctx.as_root)
    ctx.run("init")
    ctx.want = manifest(ctx.source)
    ctx.snapshot = ctx.backup(ctx.source)
    ctx.run("check", "--read-data")


def full_dense(ctx):
    """The default restore into an empty directory."""
    return compare(ctx.want, ctx.restore(ctx.snapshot, ctx.fresh("dense")))


def full_sparse(ctx):
    """A sparse restore must leave every hole of the source unallocated."""
    got = ctx.restore(ctx.snapshot, ctx.fresh("sparse"), *SPARSE)
    return compare(ctx.want, got, check_holes=True)


def subtree(ctx):
    """snapshot:path restores that directory as the target's root."""
    got = ctx.restore(f"{ctx.snapshot}:hl", ctx.fresh("subtree"), *SPARSE)
    return compare(manifest(ctx.source / "hl"), got, check_holes=True)


def over_existing(ctx):
    """--delete --verify-existing over a changed copy gives back the snapshot exactly."""
    target = ctx.fresh("existing")
    ctx.restore(ctx.snapshot, target, *SPARSE)

    def same_stat_edit(path: Path, offset: int, data: bytes):
        info = os.stat(path)
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(data)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    (target / "extra-file").write_bytes(b"not in the snapshot")
    (target / "extra-dir" / "sub").mkdir(parents=True)
    (target / "extra-dir" / "sub" / "f").write_bytes(b"not in the snapshot")
    (target / "dir" / "nested" / "extra").write_bytes(b"not in the snapshot")
    same_stat_edit(target / "plain", 0, b"PLAIN")
    same_stat_edit(target / "dir" / "random", 1_000_000, b"\x00\x01\x02")
    same_stat_edit(target / "sparse", 32 * 1024 * 1024, b"stale bytes in the hole")
    (target / "file").unlink()
    (target / "file").mkdir()
    (target / "file" / "inside").write_bytes(b"a directory where a file was")
    (target / "empty-dir").rmdir()
    (target / "empty-dir").write_bytes(b"a file where a directory was")
    (target / "link-rel").unlink()
    (target / "link-rel").write_bytes(b"a file where a symlink was")
    os.chmod(target / "exec", 0o644)
    os.removexattr(target / "xattr", "user.cocalc")
    os.setxattr(target / "xattr", "user.extra", b"not in the snapshot")
    # break one link of a hardlink group, keeping content and mtime
    member = target / "hl" / "a" / "three-2"
    info = os.stat(member)
    member.unlink()
    member.write_bytes((target / "hl" / "three").read_bytes())
    os.utime(member, ns=(info.st_atime_ns, info.st_mtime_ns))

    got = ctx.restore(ctx.snapshot, target, "--delete", "--verify-existing", *SPARSE)
    return compare(ctx.want, got, check_holes=True)


def excluded_hardlink_member(ctx):
    """A hardlink group with an excluded member restores as the remaining links only."""
    source = ctx.fresh("source-excluded")
    (source / "excluded").mkdir()
    (source / "a").write_bytes(b"three links, one excluded\n")
    os.link(source / "a", source / "b")
    os.link(source / "a", source / "excluded" / "c")
    (source / "kept").write_bytes(b"kept")
    want = manifest(source)
    del want["excluded"], want["excluded/c"]
    for name in ("a", "b"):
        want[name]["nlink"] = 2
    snapshot = ctx.backup(source, "--glob", "!excluded")
    return compare(want, ctx.restore(snapshot, ctx.fresh("restored-excluded")))


def deep_path(ctx):
    """A file whose full path is longer than PATH_MAX (4096 bytes)."""
    source = ctx.fresh("source-deep")
    here = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.chdir(source)
        for i in range(20):
            name = f"{i:02d}-" + "d" * 247
            os.mkdir(name)
            os.chdir(name)
        Path("bottom").write_bytes(b"more than PATH_MAX below the root")
    finally:
        os.fchdir(here)
        os.close(here)
    want = manifest(source)
    snapshot = ctx.backup(source)
    return compare(want, ctx.restore(snapshot, ctx.fresh("restored-deep")))


def deep_path_recorded(ctx):
    """A backup that had to skip entries says so in the snapshot, and --strict refuses it."""
    source = ctx.work / "source-deep"  # made by deep_path, or here when run alone
    if not source.exists():
        deep_path(ctx)
    snapshot = json.loads(ctx.run("backup", "--json", ".", cwd=source))
    problems = []
    if not snapshot.get("summary", {}).get("error_count"):
        problems.append(f"snapshot of a backup that skipped entries has no error_count: {snapshot.get('summary')}")
    try:
        ctx.run("backup", "--strict", ".", cwd=source)
        problems.append("backup --strict saved a snapshot although it skipped entries")
    except RusticFailed:
        pass
    return problems


def mismatch_without_delete(ctx):
    """Without --delete, a snapshot directory that can't be created is an error, not exit 0."""
    source = ctx.fresh("source-mismatch")
    (source / "empty-dir").mkdir()  # no children, so nothing else trips over it
    (source / "file").write_bytes(b"file")
    snapshot = ctx.backup(source)
    target = ctx.fresh("mismatch")
    (target / "empty-dir").write_bytes(b"a file where the snapshot has a directory")
    try:
        ctx.restore(snapshot, target)
    except RusticFailed:
        return []
    return ["restore exited 0 although directory 'empty-dir' could not be created"]


def file_over_dir_without_delete(ctx):
    """Without --delete, a snapshot file whose path is a directory in the target is an error."""
    source = ctx.fresh("source-file-over-dir")
    (source / "file").write_bytes(b"file")
    (source / "empty-file").write_bytes(b"")
    snapshot = ctx.backup(source)
    problems = []
    for name in ("file", "empty-file"):
        target = ctx.fresh(f"file-over-dir-{name}")
        (target / name).mkdir()
        try:
            ctx.restore(snapshot, target)
            problems.append(f"restore exited 0 although file {name!r} is a directory in the target")
        except RusticFailed:
            pass
    return problems


def root_metadata(ctx):
    """The backed-up directory's own mode, owner, times and xattrs, restored onto the target."""
    source = ctx.fresh("source-root")
    (source / "child").write_bytes(b"child")
    os.chmod(source, 0o750)
    os.setxattr(source, "user.root", b"root xattr")
    if ctx.as_root:
        os.chown(source, 1234, 5678)
    os.utime(source, ns=(1_300_000_000_000_000_001, 1_300_000_000_000_000_001))
    want = manifest(source)["."]
    snapshot = ctx.backup(source)
    got = ctx.restore(snapshot, ctx.fresh("restored-root"))["."]
    return [f"root {k}: {want[k]!r} != {got[k]!r}" for k in sorted(want) if want[k] != got[k]]


def incremental(ctx):
    """Change the source, back it up with the first snapshot as parent, restore both."""
    src = ctx.source

    def edit_keep_mtime(path: Path, offset: int, data: bytes):
        info = os.stat(path)
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(data)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    edit_keep_mtime(src / "dir" / "random", 12345, b"same size, same mtime")
    edit_keep_mtime(src / "hl" / "three", 0, b"THREE")  # through one link, seen by all
    with open(src / "times-appended", "wb") as f:
        f.write(b"new")
    with open(src / "empty-file", "ab") as f:
        f.write(b"no longer empty")
    (src / "plain").unlink()
    (src / "dir" / "nested").rename(src / "dir" / "moved")
    os.chmod(src / "private", 0o640)
    os.setxattr(src / "xattr", "user.cocalc", b"VALUE")
    os.link(src / "exec", src / "exec-link")
    (src / "hl" / "empty-2").unlink()
    with open(src / "sparse-tail", "r+b") as f:
        f.truncate(4)
        f.truncate(20 * 1024 * 1024)
    want = manifest(src)
    second = ctx.backup(src, "--parent", ctx.snapshot)
    problems = compare(want, ctx.restore(second, ctx.fresh("incremental"), *SPARSE), check_holes=True)
    again = ctx.restore(ctx.snapshot, ctx.fresh("first-again"), *SPARSE)
    problems += [f"first snapshot: {p}" for p in compare(ctx.want, again, check_holes=True)]
    ctx.run("check", "--read-data")
    return problems


# Scenario -> (why it fails today, patterns).  A listed scenario counts as an
# expected failure only if every problem it reports fully matches one of its
# patterns; any other problem (including an error) fails the run, so the rest
# of the scenario keeps guarding.  A listed scenario that passes is reported
# as an unexpected pass, so the entry is removed together with the fix.
DEEP = r"\d\d-d{247}"
EXPECTED_FAILURES: dict[str, tuple[str, list[str]]] = {
    "over_existing": (
        "restore --delete does not recreate a directory the target has as a file",
        [r"missing: 'empty-dir'"],
    ),
    "mismatch_without_delete": (
        "a restore that cannot create a snapshot directory exits 0",
        [r"restore exited 0 although directory 'empty-dir' could not be created"],
    ),
    "deep_path": (
        "the source walker uses full paths, so entries beyond PATH_MAX are skipped",
        [rf"missing: '{DEEP}(/{DEEP})*(/bottom)?'"],
    ),
    "deep_path_recorded": (
        "a backup that skipped entries does not record that in the snapshot",
        [r"snapshot of a backup that skipped entries has no error_count: .*"],
    ),
    "root_metadata": (
        "the restore target's own metadata is not restored",
        [r"root (mode|uid|gid|mtime_ns|xattrs): .*"],
    ),
}


def unexplained(name: str, problems: list[str]) -> list[str]:
    """The problems of a scenario that its EXPECTED_FAILURES entry doesn't cover."""
    if name not in EXPECTED_FAILURES:
        return problems
    patterns = [re.compile(p, re.DOTALL) for p in EXPECTED_FAILURES[name][1]]
    return [p for p in problems if not any(pat.fullmatch(p) for pat in patterns)]


# incremental changes the source, so it runs last
SCENARIOS = [
    full_dense,
    full_sparse,
    subtree,
    over_existing,
    excluded_hardlink_member,
    deep_path,
    deep_path_recorded,
    mismatch_without_delete,
    file_over_dir_without_delete,
    root_metadata,
    incremental,
]


def run_all(ctx: Context, only=None) -> dict:
    results = {}
    setup(ctx)
    for scenario in SCENARIOS:
        if only and scenario.__name__ not in only:
            continue
        try:
            results[scenario.__name__] = scenario(ctx)
        except (RusticFailed, OSError) as err:
            results[scenario.__name__] = [f"error: {err}"]
    for name in ("dense", "sparse", "subtree", "existing", "mismatch", "incremental", "first-again"):
        shutil.rmtree(ctx.work / name, ignore_errors=True)
    return results
