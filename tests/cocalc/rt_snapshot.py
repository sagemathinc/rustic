#!/usr/bin/env python3
"""Round trips from btrfs read-only snapshots with CoCalc's argv, and generated trees.

Usage: rt_snapshot.py RUSTIC_BINARY WORK_DIR [NAME...]

NAME is one of the snapshot scenarios in rt/snapshot.py (snapshot_source,
snapshot_source_captured, snapshot_subtree, snapshot_parent) or `generated`;
none means all.  WORK_DIR is a fresh directory on the filesystem under test,
run as root.

- On btrfs the scenarios run, and each generated tree is backed up from its
  read-only snapshot.
- On ext4 the scenarios report the skip "rt-snap: needs btrfs" (allowed there
  only), and each generated tree is backed up from a plain directory.

Generated trees: RT_GEN_SEEDS seeds (default 5) named RT_GEN_SEED_BASE-<i>.  A
failing seed is printed with its problems, then shrunk, and the smallest
failing spec is printed so it can be replayed with rt.gen.build().  With
ROUNDTRIP_STRICT=1 (as in CI) any other skip fails the run.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from rt import gen
from rt.manifest import manifest
from rt.snapshot import SCENARIOS, SPARSE, RusticFailed, Snap, btrfs, check, expected, run_all

SNAP_SKIP = "rt-snap: needs btrfs"
SHRINK_RUNS = 60
SHRINK_SECONDS = 600


def fstype(path: Path) -> str:
    r = subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)], capture_output=True, text=True, check=True)
    return r.stdout.strip()


class CleanupFailed(Exception):
    """A generated round trip left something on disk: the generated run must stop."""

    def __init__(self, leftovers: list[str], problems: list[str]):
        super().__init__("; ".join(leftovers))
        self.leftovers = leftovers
        self.problems = problems


class Generated:
    """One round trip of a generated tree with CoCalc's argv.

    Every call (shrink retries included) uses its own repository and fresh
    directories, and removes all of them before returning, after a success and
    after any failure, so disk use never grows with the number of seeds.
    """

    def __init__(self, s: Snap, on_btrfs: bool):
        self.s, self.on_btrfs, self.n = s, on_btrfs, 0

    def path(self, name: str) -> Path:
        self.n += 1
        return self.s.work / f"gen-{self.n}-{name}"

    def fresh(self, name: str, made: list) -> Path:
        path = self.path(name)
        made.append((path, "subvol" if self.on_btrfs else "dir"))  # registered first: a half-made one is removed too
        if self.on_btrfs:
            btrfs("subvolume", "create", path)
        else:
            path.mkdir()
        return path

    def round_trip(self, spec: dict, made: list) -> list[str]:
        repo = self.path("repo")
        made.append((repo, "dir"))
        ctx = Snap(self.s.binary, self.s.work, self.s.as_root, repo=repo)
        init = ctx.run(["init"])
        if init.returncode:
            raise RusticFailed(f"init {repo} exited {init.returncode}: {init.stderr.decode(errors='replace')[-300:]}")
        src = self.fresh("src", made)
        gen.build(spec, src, reflink=self.on_btrfs)
        source = src
        if self.on_btrfs:
            source = self.s.staging / self.path("snap").name
            made.append((source, "subvol"))
            btrfs("subvolume", "snapshot", "-r", src, source)
        want = expected(manifest(source))
        snapshot = ctx.backup(source)
        problems = check(want, ctx.restore(snapshot, self.fresh("dense", made)))
        sparse = ctx.restore(snapshot, self.fresh("sparse", made), *SPARSE)
        return problems + [f"sparse: {p}" for p in check(want, sparse, check_holes=True)]

    def remove(self, path: Path, kind: str) -> None:
        if not os.path.lexists(path):
            return
        if kind == "subvol":
            self.s.delete(path)
        else:
            shutil.rmtree(path)

    def cleanup(self, made: list) -> list[str]:
        """Remove everything this call made, newest first; report what could not be removed."""
        failed = []
        for path, kind in reversed(made):
            try:
                self.remove(path, kind)
            except (OSError, subprocess.SubprocessError) as err:
                failed.append(f"cleanup failed: {path}: {err}")
            if os.path.lexists(path):
                failed.append(f"cleanup failed: {path} still exists")
        return failed

    def problems(self, spec: dict) -> list[str]:
        """The round trip's problems.  If anything it made cannot be removed, CleanupFailed is raised
        instead (chained to an unexpected exception, if one was propagating): leftovers are fatal, so
        no shrink retry or later seed can pile up more of them."""
        made: list[tuple[Path, str]] = []
        try:
            problems = self.round_trip(spec, made)
        except (RusticFailed, OSError, subprocess.SubprocessError, ValueError, KeyError) as err:
            problems = [f"error: {err}"]
        except BaseException as err:  # unexpected: clean up, then re-raise it (or report both)
            leftovers = self.cleanup(made)
            if leftovers:
                raise CleanupFailed(leftovers, [f"unexpected {type(err).__name__}: {err}"]) from err
            raise
        leftovers = self.cleanup(made)
        if leftovers:
            raise CleanupFailed(leftovers, problems)
        return problems


def ensure_staging(s: Snap) -> None:
    """The staging dir for generated snapshots; never a shared repository."""
    s.staging.mkdir(parents=True, exist_ok=True)


def run_generated(s: Snap, on_btrfs: bool) -> dict:
    count = int(os.environ.get("RT_GEN_SEEDS", "5"))
    base = os.environ.get("RT_GEN_SEED_BASE", "local")
    g = Generated(s, on_btrfs)
    out = {"seeds": count, "base": base, "failed": {}, "fatal": None}
    for i in range(count):
        seed = f"{base}-{i}"
        spec = gen.generate(seed)
        try:
            problems = g.problems(spec)
            if not problems:
                continue
            cls = gen.problem_class(problems)
            print(f"generated seed {seed!r} FAILED ({cls}): {problems[:5]}", file=sys.stderr)
            deadline = time.monotonic() + SHRINK_SECONDS

            def fails(candidate):
                if time.monotonic() > deadline:
                    return None  # out of time: keep the current smallest spec
                return gen.problem_class(g.problems(candidate))  # CleanupFailed propagates out of shrink

            small, runs = gen.shrink(spec, fails, max_runs=SHRINK_RUNS)
        except CleanupFailed as err:  # leftovers on disk: no shrink retry, no later seed
            out["fatal"] = {"seed": seed, "leftovers": err.leftovers, "problems": err.problems[:20]}
            print(f"generated run STOPPED at seed {seed!r}: {err.leftovers}", file=sys.stderr)
            break
        out["failed"][seed] = {"class": cls, "problems": problems[:20], "stats": gen.stats(spec),
                               "shrunk_runs": runs, "smallest_spec": small}
        print(f"smallest failing spec for {seed!r} ({len(small['entries'])} entries):\n{json.dumps(small)}", file=sys.stderr)
    return out


def main() -> int:
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    work = Path(sys.argv[2]).resolve(strict=True)
    names = set(sys.argv[3:])
    known = {f.__name__ for f in SCENARIOS} | {"generated"}
    if names - known:
        print(f"unknown names {sorted(names - known)}; known: {sorted(known)}", file=sys.stderr)
        return 2
    fs = fstype(work)
    if fs not in ("btrfs", "ext4"):
        print(f"filesystem {fs!r}: needs btrfs or ext4", file=sys.stderr)
        return 2
    s = Snap(binary, work, as_root=os.geteuid() == 0)
    scenario_names = names - {"generated"} if names else {f.__name__ for f in SCENARIOS}
    skipped, results = [], {}
    if scenario_names and fs == "btrfs":
        results = run_all(s, only=scenario_names if names else None)
        skipped += s.skipped
    elif scenario_names:
        skipped.append(SNAP_SKIP)
    generated = None
    if not names or "generated" in names:
        ensure_staging(s)
        generated = run_generated(s, fs == "btrfs")
    failed = sorted(n for n, p in results.items() if p)
    report = {
        "filesystem": fs,
        "skipped": skipped,
        "passed": sorted(n for n, p in results.items() if not p),
        "failed": failed,
        "problems": {n: results[n] for n in failed},
        "generated": generated,
    }
    print(json.dumps(report, indent=2, default=str))
    if os.environ.get("ROUNDTRIP_STRICT") == "1":
        refused = [x for x in skipped if not (x == SNAP_SKIP and fs == "ext4")]
        if refused:
            print(f"ROUNDTRIP_STRICT: skips not allowed: {refused}", file=sys.stderr)
            return 1
    if not results and not generated:
        print("nothing ran", file=sys.stderr)
        return 1
    return 1 if failed or (generated and (generated["failed"] or generated["fatal"])) else 0


if __name__ == "__main__":
    sys.exit(main())
