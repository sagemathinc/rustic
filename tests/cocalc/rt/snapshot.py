"""Round trips from a btrfs read-only snapshot, with CoCalc's own argv.

CoCalc backs up a project like this (cocalc-ai src/packages/file-server/btrfs/
subvolume-rustic.ts at 0e438dd0):

- `btrfs subvolume snapshot -r <subvol> <mount>/.rustic-backup-staging/<name>/<tmp>`;
  when it first adds captured data (the edit history under .local/share/cocalc/
  persist) the snapshot is writable, then `btrfs property set -ts <snap> ro true`;
- `rustic backup -x --json [--tag T]... [--parent P] --glob '!.snapshots'
  --glob '!.snapshots/**' .` with the snapshot as the working directory;
- `rustic restore <id>[:<path>] <dest>`, with no other flags.

Each scenario returns a list of problems; empty means it passed.  The restores
use that bare argv, except R2, which adds `--sparse by-content-required` to
prove a sparse restore keeps every hole (it is not CoCalc's argv).
"""
import json
import os
from pathlib import Path
import subprocess

from .fixture import build
from .manifest import compare, manifest

STAGING = ".rustic-backup-staging"
BACKUP = ["backup", "-x", "--json"]
EXCLUDES = ["--glob", "!.snapshots", "--glob", "!.snapshots/**"]
SPARSE = ["--sparse", "by-content-required"]
EXCLUDED = ".snapshots"


class RusticFailed(Exception):
    pass


def excluded(path: str) -> bool:
    return path == EXCLUDED or path.startswith(EXCLUDED + "/")


def expected(m: dict) -> dict:
    """The manifest a restore must equal: the source minus .snapshots and everything under it.

    The globs exclude `.snapshots` by name; this harness only ever puts one at the
    top, so a `.snapshots` anywhere else is refused rather than guessed about.
    """
    nested = [p for p in m if p.split("/")[-1] == EXCLUDED and p != EXCLUDED]
    if nested:
        raise ValueError(f"a nested {EXCLUDED!r} in the source is not modelled: {nested}")
    return {p: e for p, e in m.items() if not excluded(p)}


def excluded_restored(got: dict) -> list[str]:
    return [f"excluded path restored: {p!r}" for p in sorted(got) if excluded(p)]


def rebase(m: dict, sub: str) -> dict:
    """The part of a manifest under sub/, as if sub were the root."""
    out = {".": m[sub]}
    out.update({p[len(sub) + 1 :]: e for p, e in m.items() if p.startswith(sub + "/")})
    return out


def btrfs(*args) -> None:
    subprocess.run(["btrfs", *map(str, args)], check=True, capture_output=True, timeout=600)


def stderr_json(stderr: str) -> list:
    """The JSON objects among rustic's stderr lines (with --json, an error is one)."""
    found = []
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found


class Snap:
    """A repository, a source subvolume and its staging directory under work/."""

    def __init__(self, binary: str, work: Path, as_root: bool, repo: Path | None = None):
        self.work = work
        self.as_root = as_root
        self.binary = binary
        self.repo = repo or work / "repo"  # the scenarios' repository; generated trees each get their own
        self.common = [binary, "--repository", str(self.repo), "--no-progress"]
        self.env = {**os.environ, "RUSTIC_PASSWORD": "fixture-only", "LC_ALL": "C.UTF-8"}
        self.src = work / "src"
        self.staging = work / STAGING / "src"
        self.count = 0
        self.skipped: list[str] = []

    def run(self, args, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run([*self.common, *args], cwd=cwd or self.work, env=self.env, capture_output=True, timeout=1200)

    def backup(self, snapshot: Path, *extra) -> str:
        """CoCalc's backup argv from the snapshot; exit 0 is required (nothing may be skipped)."""
        r = self.run([*BACKUP, *extra, *EXCLUDES, "."], cwd=snapshot)
        if r.returncode:
            err = r.stderr.decode(errors="replace")
            what = "entries skipped" if r.returncode == 3 else f"exit {r.returncode}"
            raise RusticFailed(f"backup failed ({what}); stderr JSON: {stderr_json(err)}; tail: {err[-500:]}")
        return json.loads(r.stdout)["id"]

    def restore(self, what: str, dest: Path, *extra) -> dict:
        r = self.run(["restore", *extra, what, str(dest)])
        if r.returncode:
            raise RusticFailed(f"restore {what} exited {r.returncode}: {r.stderr.decode(errors='replace')[-500:]}")
        return manifest(dest)

    def subvolume(self, name: str) -> Path:
        path = self.work / name
        btrfs("subvolume", "create", path)
        return path

    def snapshot(self, readonly=True) -> Path:
        """A snapshot of src at CoCalc's staging path shape."""
        self.count += 1
        path = self.staging / f"tmp-{self.count}"
        btrfs("subvolume", "snapshot", *(["-r"] if readonly else []), self.src, path)
        return path

    def delete(self, path: Path) -> None:
        btrfs("property", "set", "-ts", path, "ro", "false")
        btrfs("subvolume", "delete", path)


def setup(s: Snap) -> None:
    s.work.joinpath(STAGING).mkdir()
    s.staging.mkdir()
    btrfs("subvolume", "create", s.src)
    s.skipped = build(s.src, s.as_root)
    snaps = s.src / EXCLUDED
    (snaps / "daily" / "older").mkdir(parents=True)
    (snaps / "daily" / "data").write_bytes(b"a .snapshots entry: never backed up")
    (snaps / "daily" / "older" / "deep").write_bytes(b"also excluded")
    (snaps / "top").write_bytes(b"excluded")
    s.run(["init"]).check_returncode()


def check(want: dict, got: dict, **kw) -> list[str]:
    return compare(want, got, **kw) + excluded_restored(got)


def non_vacuous(m: dict) -> list[str]:
    n = sum(1 for p in m if excluded(p))
    return [] if n >= 3 else [f"the source holds only {n} {EXCLUDED!r} entries (need 3)"]


class State:
    """The artifacts shared between scenarios, in their fixed order."""

    snap1 = b1 = None
    want1: dict = {}


def snapshot_source(s: Snap, st: State) -> list[str]:
    """S1: RO snapshot, CoCalc backup (B1), bare restore (R1) and sparse restore (R2)."""
    st.snap1 = s.snapshot()
    full = manifest(st.snap1)
    problems = non_vacuous(full)
    st.want1 = expected(full)
    st.b1 = s.backup(st.snap1)
    d1 = s.subvolume("d1")
    problems += [f"R1: {p}" for p in check(st.want1, s.restore(st.b1, d1))]
    s.delete(d1)
    d2 = s.subvolume("d2")
    problems += [f"R2: {p}" for p in check(st.want1, s.restore(st.b1, d2, *SPARSE), check_holes=True)]
    s.delete(d2)
    return problems


def snapshot_source_captured(s: Snap, st: State) -> list[str]:
    """S2: writable snapshot, captured data added, then set read-only (B2, R3)."""
    snap2 = s.snapshot(readonly=False)
    persist = snap2 / ".local" / "share" / "cocalc" / "persist"
    persist.mkdir(parents=True)
    (persist / "history.db").write_bytes(os.urandom(200_000))
    (persist / "patches").mkdir()
    (persist / "patches" / "0001").write_bytes(b"patch one\n")
    os.link(persist / "patches" / "0001", persist / "patches" / "0001-link")
    btrfs("property", "set", "-ts", snap2, "ro", "true")
    want = expected(manifest(snap2))
    b2 = s.backup(snap2)
    d3 = s.subvolume("d3")
    problems = [f"R3: {p}" for p in check(want, s.restore(b2, d3))]
    s.delete(d3)
    s.delete(snap2)
    return problems


def snapshot_subtree(s: Snap, st: State) -> list[str]:
    """S3: restore <B1>:hl, CoCalc's path form (R4)."""
    if st.b1 is None:
        return ["error: B1 unavailable (S1 backup failed)"]
    d4 = s.subvolume("d4")
    problems = [f"R4: {p}" for p in check(rebase(st.want1, "hl"), s.restore(f"{st.b1}:hl", d4))]
    s.delete(d4)
    return problems


def edit_source(src: Path) -> None:
    """Every kind of change a parent backup must notice."""

    def same_stat_edit(path: Path, offset: int, data: bytes):
        info = os.stat(path)
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(data)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    same_stat_edit(src / "dir" / "random", 12345, b"same size, same mtime")
    (src / "added").write_bytes(b"new in the second snapshot")
    (src / "plain").unlink()
    (src / "dir" / "nested").rename(src / "dir" / "moved")
    os.setxattr(src / "xattr", "user.cocalc", b"VALUE")
    os.chmod(src / "private", 0o640)
    member = src / "hl" / "a" / "three-2"  # break one link of a group, keeping content and mtime
    info = os.stat(member)
    member.unlink()
    member.write_bytes((src / "hl" / "three").read_bytes())
    os.utime(member, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.link(src / "exec", src / "exec-link")


def snapshot_parent(s: Snap, st: State) -> list[str]:
    """S4: edit src, RO snapshot, B3 with --parent B1 (R5); B1 still restores as before (R6)."""
    if st.b1 is None:
        return ["error: B1 unavailable (S1 backup failed)"]
    edit_source(s.src)
    snap3 = s.snapshot()
    want3 = expected(manifest(snap3))
    b3 = s.backup(snap3, "--parent", st.b1)
    d5 = s.subvolume("d5")
    problems = [f"R5: {p}" for p in check(want3, s.restore(b3, d5))]
    s.delete(d5)
    d6 = s.subvolume("d6")
    problems += [f"R6: {p}" for p in check(st.want1, s.restore(st.b1, d6))]
    s.delete(d6)
    s.delete(snap3)
    return problems


SCENARIOS = [snapshot_source, snapshot_source_captured, snapshot_subtree, snapshot_parent]


def run_all(s: Snap, only=None) -> dict:
    """S1-S4 in their fixed order (S3 and S4 reuse S1's snap1 and B1)."""
    names = {f.__name__ for f in SCENARIOS}
    unknown = sorted(set(only or ()) - names)
    if unknown:
        raise ValueError(f"unknown scenarios {unknown}; known: {sorted(names)}")
    setup(s)
    st, results = State(), {}
    for scenario in SCENARIOS:
        if only and scenario.__name__ not in only and not (scenario is snapshot_source and only & {"snapshot_subtree", "snapshot_parent"}):
            continue
        try:
            results[scenario.__name__] = scenario(s, st)
        except (RusticFailed, OSError, subprocess.SubprocessError, ValueError, KeyError) as err:
            results[scenario.__name__] = [f"error: {err}"]
    if st.snap1 is not None:
        s.delete(st.snap1)
    return {k: v for k, v in results.items() if not only or k in only}
