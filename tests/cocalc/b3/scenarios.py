"""Extent-reuse (B3, `--reuse-extents`) oracle scenarios.

Each scenario builds a btrfs fixture and first checks, through TREE_SEARCH,
that the fixture really is the state it is about (fixture checks: they run
today, without the feature).  Then, if the binary has `--reuse-extents`, it
backs the fixture up without and with the option, restores both and requires
the content to equal the source (the primary oracle); bytes read by rustic
(rchar) are a secondary assertion, always as a difference between the
plain and the reuse run of the same fixture, so binary and repository reads
cancel out.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import btrfs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rt.manifest import compare, manifest  # noqa: E402

FLAG = "--reuse-extents"
# Test-only switch to skip the per-file writeback barrier.  Only a binary
# built with the `test-hooks` feature honors it (and logs BARRIER_OFF_LOG); a
# release binary must ignore it.  The negative control needs such a binary,
# given as B3_HOOKS_BINARY.
BARRIER_OFF_ENV = "RUSTIC_TEST_SKIP_EXTENT_BARRIER"
BARRIER_OFF_LOG = re.compile(r"extent barrier disabled \(test-hooks\)")


def noop_log(reason: str):
    return re.compile(rf"reuse-extents: no-op \({re.escape(reason)}\)")
MiB = 1024 * 1024


class RusticFailed(Exception):
    pass


def rchar() -> int:
    """Bytes read so far by this process and its reaped children."""
    for line in open("/proc/self/io"):
        if line.startswith("rchar:"):
            return int(line.split()[1])
    raise RuntimeError("no rchar in /proc/self/io")


class Context:
    def __init__(self, binary: str, work: Path, hooks_binary: str | None = None):
        self.binary = binary
        self.hooks_binary = hooks_binary
        self.work = work
        self.env = {**os.environ, "RUSTIC_PASSWORD": "fixture-only", "LC_ALL": "C.UTF-8"}
        help_text = subprocess.run([binary, "backup", "--help"], capture_output=True, text=True).stdout
        self.has_flag = FLAG in help_text
        self.btrfs = btrfs.is_btrfs(work)
        self.compressed = self.btrfs and btrfs.compression_enabled(work)

    def path(self, name: str) -> Path:
        return self.work / name

    def rustic(self, repo: Path, *args, cwd=None, env=None, binary=None):
        before = rchar()
        result = subprocess.run([binary or self.binary, "--repository", str(repo), "--no-progress", *map(str, args)],
                                cwd=cwd, env={**self.env, **(env or {})}, capture_output=True, timeout=1200)
        read = rchar() - before
        if result.returncode:
            raise RusticFailed(f"rustic {' '.join(map(str, args))}: {result.stderr.decode(errors='replace')[-1500:]}")
        return result.stdout, result.stderr.decode(errors="replace"), read

    def repo(self, name: str) -> Path:
        repo = self.path(f"repo-{name}")
        if not repo.exists():
            self.rustic(repo, "init")
        return repo

    def backup(self, repo, source, *, parent=None, reuse=False, env=None, binary=None):
        """(snapshot id, stderr, bytes read) of `backup .` run in source."""
        args = ["backup", "--json", *(["--parent", parent] if parent else ["--force"])]
        if reuse:
            args.append(FLAG)
        out, err, read = self.rustic(repo, *args, ".", cwd=source, env=env, binary=binary)
        return json.loads(out)["id"], err, read

    def restored(self, repo, snapshot, name) -> dict:
        target = self.path(f"restore-{name}")
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        self.rustic(repo, "restore", "--numeric-id", snapshot, target)
        got = manifest(target)
        shutil.rmtree(target, ignore_errors=True)
        return got


# ---------------------------------------------------------------- fixtures

def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def reflink(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    btrfs.run("cp", "--reflink=always", src, dst)


def overwrite(path: Path, offset: int, data: bytes, keep_mtime=False) -> None:
    st = os.stat(path)
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)
    if keep_mtime:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def fresh_subvolume(ctx, name) -> Path:
    path = ctx.path(name)
    btrfs.subvolume_create(path)
    return path


def frozen(ctx, subvol: Path, name: str) -> Path:
    """A kernel-created read-only snapshot of subvol: what B3 backs up."""
    btrfs.sync()
    snap = ctx.path(name)
    btrfs.snapshot_ro(subvol, snap)
    return snap


# --------------------------------------------------------------- the oracle

def differential(ctx, name, runs, checks=None) -> list[str]:
    """Back up each source in runs (the later ones with the earlier as parent),
    without and with the option, restore every run and compare with the
    source.  checks(reads) gets {(mode, run index): bytes read}."""
    if not ctx.has_flag:
        return []
    problems, reads = [], {}
    for mode, reuse in (("plain", False), ("reuse", True)):
        repo = ctx.repo(f"{name}-{mode}")
        parent = None
        for i, source in enumerate(runs):
            want = manifest(source)
            snap, _, read = ctx.backup(repo, source, parent=parent, reuse=reuse)
            reads[(mode, i)] = read
            problems += [f"{mode} run {i}: {p}" for p in compare(want, ctx.restored(repo, snap, f"{name}-{mode}-{i}"))]
            parent = snap
    if checks and not problems:
        problems += checks(reads)
    return problems


def s1_many_reflinks(ctx):
    sv = fresh_subvolume(ctx, "s1")
    write(sv / "orig", os.urandom(4 * MiB))
    btrfs.sync()
    for i in range(20):
        reflink(sv / "orig", sv / f"d{i % 4}" / f"copy{i}")
    snap = frozen(ctx, sv, "s1-ro")
    want = btrfs.signature(snap / "orig")
    fixture = [f"copy{i} signature differs" for i in range(20)
               if btrfs.signature(snap / f"d{i % 4}" / f"copy{i}") != want]

    def reads(r):
        # 21 paths of one 4 MiB content: reuse saves (almost) the 20 copies
        saved = r[("plain", 0)] - r[("reuse", 0)]
        return [] if saved >= 0.8 * 20 * 4 * MiB else [f"reuse saved only {saved} bytes: {r}"]
    return fixture, differential(ctx, "s1", [snap], reads)


def s2_inplace_edit(ctx):
    sv = fresh_subvolume(ctx, "s2")
    write(sv / "a", os.urandom(2 * MiB))
    btrfs.sync()
    reflink(sv / "a", sv / "b")
    snap1 = frozen(ctx, sv, "s2-ro1")
    before = btrfs.signature(snap1 / "a")
    overwrite(sv / "a", MiB, b"in place, same size", keep_mtime=True)
    snap2 = frozen(ctx, sv, "s2-ro2")
    fixture = []
    if btrfs.signature(snap2 / "a") == before:
        fixture.append("an in-place edit kept a's signature")
    if btrfs.signature(snap2 / "b") != before:
        fixture.append("b's signature changed")
    return fixture, differential(ctx, "s2", [snap1, snap2])


def s3_modified_tail(ctx):
    sv = fresh_subvolume(ctx, "s3")
    write(sv / "a", os.urandom(2 * MiB))
    btrfs.sync()
    reflink(sv / "a", sv / "b")
    overwrite(sv / "b", 2 * MiB - 64 * 1024, os.urandom(64 * 1024))
    snap = frozen(ctx, sv, "s3-ro")
    fixture = []
    if btrfs.signature(snap / "a") == btrfs.signature(snap / "b"):
        fixture.append("a modified tail kept the signature")
    if btrfs.bytenrs(snap / "a")[0] != btrfs.bytenrs(snap / "b")[0]:
        fixture.append("a and b do not share the prefix extent")
    return fixture, differential(ctx, "s3", [snap])


def s4_same_bytes_separately(ctx):
    sv = fresh_subvolume(ctx, "s4")
    data = os.urandom(2 * MiB)
    write(sv / "a", data)
    write(sv / "b", data)
    snap = frozen(ctx, sv, "s4-ro")
    fixture = []
    if set(btrfs.bytenrs(snap / "a")) & set(btrfs.bytenrs(snap / "b")):
        fixture.append("separately written copies share extents")
    return fixture, differential(ctx, "s4", [snap])


def s5_compressed_reflinks(ctx):
    if not ctx.compressed:
        return None, None
    sv = fresh_subvolume(ctx, "s5")
    write(sv / "z", b"compressible line for the oracle\n" * 65536)
    btrfs.sync()
    reflink(sv / "z", sv / "z2")
    snap = frozen(ctx, sv, "s5-ro")
    fixture = []
    if btrfs.signature(snap / "z") != btrfs.signature(snap / "z2"):
        fixture.append("compressed reflinks differ")
    if not any(e.compression for e in btrfs.extents(snap / "z")):
        fixture.append("no compressed extent on a compress mount")

    def reads(r):
        saved = r[("plain", 0)] - r[("reuse", 0)]
        size = os.path.getsize(snap / "z")
        return [] if saved >= 0.8 * size else [f"reuse saved only {saved} bytes: {r}"]
    return fixture, differential(ctx, "s5", [snap], reads)


def s6_nocow(ctx):
    sv = fresh_subvolume(ctx, "s6")
    (sv / "nocow").mkdir()
    btrfs.run("chattr", "+C", sv / "nocow")
    write(sv / "nocow" / "a", os.urandom(MiB))
    btrfs.sync()
    before = btrfs.signature(sv / "nocow" / "a")
    overwrite(sv / "nocow" / "a", 4096, os.urandom(4096))
    btrfs.sync()
    fixture = []
    if btrfs.signature(sv / "nocow" / "a")[1] != before[1]:
        fixture.append("NOCOW in-place overwrite changed the extent items (expected unchanged)")
    reflink(sv / "nocow" / "a", sv / "nocow" / "b")
    snap = frozen(ctx, sv, "s6-ro")

    def reads(r):
        # NOCOW inodes are excluded: with reuse rustic reads (about) as much as without
        saved = r[("plain", 0)] - r[("reuse", 0)]
        return [] if saved < 0.5 * MiB else [f"NOCOW reflinks were reused ({saved} bytes saved): {r}"]
    return fixture, differential(ctx, "s6", [snap], reads)


def s6a_freed_extent_reuse(ctx):
    """A is backed up, deleted and its extent freed; a new file B gets A's old
    disk_bytenr and is renamed to A's path.  The item generation must differ
    (the permanent conformance guard), and B's content must be backed up,
    not A's."""
    sv = fresh_subvolume(ctx, "s6a")
    write(sv / "A", os.urandom(MiB))
    snap1 = frozen(ctx, sv, "s6a-ro1")
    old = btrfs.extents(sv / "A")[0]
    first = {}
    if ctx.has_flag:
        for mode, reuse in (("plain", False), ("reuse", True)):
            repo = ctx.repo(f"s6a-{mode}")
            first[mode] = (repo, ctx.backup(repo, snap1, reuse=reuse)[0])
    (sv / "A").unlink()
    btrfs.run("btrfs", "subvolume", "delete", snap1)  # drop the last reference to A's extent
    btrfs.sync()
    btrfs.sync()
    found = None
    for i in range(300):
        write(sv / f"B{i}", os.urandom(MiB))
        btrfs.sync()
        if btrfs.extents(sv / f"B{i}")[0].disk_bytenr == old.disk_bytenr:
            found = sv / f"B{i}"
            break
    if not found:
        return ["no new file reused A's disk_bytenr within 300 files"], []
    if btrfs.extents(found)[0].generation == old.generation:
        return [f"the reused bytenr kept the item generation {old.generation}"], []
    for other in sv.glob("B*"):
        if other != found:
            other.unlink()
    found.rename(sv / "A")  # B now has A's path and A's old bytenr
    snap2 = frozen(ctx, sv, "s6a-ro2")
    problems = []
    want = manifest(snap2)
    for mode, reuse in (("plain", False), ("reuse", True)):
        if mode not in first:
            continue
        repo, parent = first[mode]
        snap = ctx.backup(repo, snap2, parent=parent, reuse=reuse)[0]
        problems += [f"{mode}: {p}" for p in compare(want, ctx.restored(repo, snap, f"s6a-{mode}"))]
    return [], problems


def s7_writable_source(ctx):
    sv = fresh_subvolume(ctx, "s7")
    write(sv / "a", os.urandom(MiB))
    btrfs.sync()
    reflink(sv / "a", sv / "b")
    problems = differential(ctx, "s7", [sv])
    if ctx.has_flag and not problems:
        _, err, _ = ctx.backup(ctx.repo("s7-log"), sv, reuse=True)
        if not noop_log("source not read-only").search(err):
            problems.append("no 'reuse-extents: no-op (source not read-only)' warning for a writable source")
    return [], problems


def _dirty_flipped(ctx, name):
    """c and its reflink d; c overwritten (buffered, not flushed); the
    subvolume then flipped read-only.  Returns (subvol, its manifest): the
    manifest reads through the page cache, so it holds c's new bytes."""
    sv = fresh_subvolume(ctx, name)
    write(sv / "c", os.urandom(MiB))
    btrfs.sync()
    reflink(sv / "c", sv / "d")
    btrfs.sync()
    overwrite(sv / "c", 10 * 4096, os.urandom(4096))
    btrfs.set_readonly(sv, True)
    return sv, manifest(sv)


def s7d_barrier(ctx):
    """Three runs, each on a fresh dirty-then-flipped-RO fixture:
    - barrier: the normal binary restores c correctly;
    - ignored: the normal binary with the skip switch set still restores c
      correctly and does not log the switch (release builds ignore it);
    - control: a test-hooks binary with the switch restores c STALE, which
      proves the scenario detects a missing barrier."""
    fixture, problems = [], []
    cases = [("barrier", None, None), ("ignored", ctx.binary, {BARRIER_OFF_ENV: "1"})]
    if ctx.hooks_binary:
        cases.append(("control", ctx.hooks_binary, {BARRIER_OFF_ENV: "1"}))
    for case, binary, env in cases:
        sv, want = _dirty_flipped(ctx, f"s7d-{case}")
        if btrfs.extents(sv / "c") != btrfs.extents(sv / "d"):
            fixture.append(f"{case}: the stale item was not observed (incidental writeback?), "
                           "so this case proves nothing")
            continue
        if not ctx.has_flag:
            continue
        repo = ctx.repo(f"s7d-{case}")
        snap, err, _ = ctx.backup(repo, sv, reuse=True, env=env, binary=binary)
        # the whole fixture, both directions: whichever of c and d owned the
        # read, a missing barrier corrupts one of them
        mismatches = compare(want, ctx.restored(repo, snap, f"s7d-{case}"))
        stale = bool(mismatches)
        switched = bool(BARRIER_OFF_LOG.search(err))
        if case == "control":
            if not switched:
                problems.append("control: the test-hooks binary did not log that the barrier is off")
            elif not stale:
                problems.append("control: without the barrier the whole fixture was still correct, "
                                "so the scenario does not detect a missing barrier")
        else:
            if stale:
                problems += [f"{case}: {m}" for m in mismatches]
            if switched:
                problems.append(f"{case}: a release binary honored {BARRIER_OFF_ENV}")
    return fixture, problems


def s9_new_worktree(ctx):
    sv = fresh_subvolume(ctx, "s9")
    for i in range(16):
        write(sv / "store" / f"pkg{i}", os.urandom(256 * 1024))
    btrfs.sync()
    for i in range(16):
        reflink(sv / "store" / f"pkg{i}", sv / "wt1" / "node_modules" / f"pkg{i}")
    snap1 = frozen(ctx, sv, "s9-ro1")
    for i in range(16):
        reflink(sv / "store" / f"pkg{i}", sv / "wt2" / "node_modules" / f"pkg{i}")
    snap2 = frozen(ctx, sv, "s9-ro2")
    fixture = [f"wt2 pkg{i} signature differs from the store" for i in range(16)
               if btrfs.signature(snap2 / "wt2" / "node_modules" / f"pkg{i}")
               != btrfs.signature(snap2 / "store" / f"pkg{i}")]
    wt = 16 * 256 * 1024

    def reads(r):
        # the second backup reads wt2 without reuse, and (almost) none of it with reuse
        saved = r[("plain", 1)] - r[("reuse", 1)]
        return [] if saved >= 0.8 * wt else [f"reuse saved only {saved} bytes of the new worktree: {r}"]
    return fixture, differential(ctx, "s9", [snap1, snap2], reads)


def s10_fallback(ctx):
    if ctx.btrfs:
        return None, None
    src = ctx.path("s10")
    write(src / "a", os.urandom(MiB))
    shutil.copy(src / "a", src / "b")
    problems = differential(ctx, "s10", [src])
    if ctx.has_flag and not problems:
        _, err, _ = ctx.backup(ctx.repo("s10-log"), src, reuse=True)
        if not noop_log("not btrfs").search(err):
            problems.append("no 'reuse-extents: no-op (not btrfs)' warning on a non-btrfs source")
    return [], problems


BTRFS_SCENARIOS = [s1_many_reflinks, s2_inplace_edit, s3_modified_tail, s4_same_bytes_separately,
                   s5_compressed_reflinks, s6_nocow, s6a_freed_extent_reuse, s7_writable_source,
                   s7d_barrier, s9_new_worktree]
# need hooks in the implementation (mid-run RO flip, balance during the run, prune between runs)
PENDING = {"s7a_ro_flip_mid_run": "needs RUSTIC_TEST_PAUSE_AFTER_SCANS (test-hooks)",
           "s7b_balance_during_backup": "needs RUSTIC_TEST_PAUSE_AFTER_SCANS (test-hooks)",
           "s8_prune_between_runs": "with the implementation"}
