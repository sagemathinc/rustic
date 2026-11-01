#!/usr/bin/env python3
"""Crash safety: SIGKILL backup and prune at random moments, and run a backup
during a prune (consistent again after the next prune; see phase 3).  After every kill:
  - the next backup completes;
  - `check --read-data` is clean;
  - every snapshot that should exist restores byte-for-byte (manifest equal).

Usage: crash.py RUSTIC WORK_DIR [--iterations N] [--seed S]

Kill points are random but reproducible from the printed seed.  Exit 1 on any
problem.  No root needed; CI runs it on its loopbacks.
"""
import argparse
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rt.manifest import compare, manifest  # noqa: E402

MiB = 1024 * 1024


class Repo:
    def __init__(self, binary: str, path: Path):
        self.binary = binary
        self.path = path
        self.env = {**os.environ, "RUSTIC_PASSWORD": "fixture-only", "LC_ALL": "C.UTF-8"}

    def argv(self, *args):
        return [self.binary, "--repository", str(self.path), "--no-progress", *map(str, args)]

    def run(self, *args, cwd=None, check=True):
        result = subprocess.run(self.argv(*args), cwd=cwd, env=self.env, capture_output=True, timeout=1800)
        if check and result.returncode:
            raise RuntimeError(f"rustic {' '.join(map(str, args))} exited {result.returncode}: "
                               f"{result.stderr.decode(errors='replace')[-1500:]}")
        return result

    def killed(self, *args, after: float, cwd=None):
        """Start the command, SIGKILL it after `after` seconds (if still running).
        Returns True if it was killed, False if it had already finished."""
        proc = subprocess.Popen(self.argv(*args), cwd=cwd, env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=after)
            return False
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            return True

    def backup(self, source: Path) -> str:
        out = self.run("backup", "--json", "--force", ".", cwd=source).stdout
        return json.loads(out)["id"]

    def snapshot_ids(self) -> list[str]:
        out = self.run("snapshots", "--json").stdout

        def ids(node):
            if isinstance(node, dict):
                if "tree" in node and "id" in node:
                    return [node["id"]]
                return [i for v in node.values() for i in ids(v)]
            if isinstance(node, list):
                return [i for v in node for i in ids(v)]
            return []
        return ids(json.loads(out or "[]"))


def grow(source: Path, rng: random.Random, files: int, size: int) -> None:
    """Add files (and change a few existing ones), so each backup has new data."""
    source.mkdir(parents=True, exist_ok=True)
    existing = sorted(source.rglob("f*"))
    for path in rng.sample(existing, min(3, len(existing))):
        with open(path, "r+b") as f:
            f.write(os.urandom(4096))
    start = len(existing)
    for i in range(start, start + files):
        d = source / f"d{i % 7}"
        d.mkdir(exist_ok=True)
        (d / f"f{i:05d}").write_bytes(os.urandom(rng.randrange(1, size)))


class Check:
    def __init__(self, repo: Repo, work: Path):
        self.repo = repo
        self.work = work
        self.expected: dict[str, dict] = {}  # snapshot id -> manifest at backup time
        self.problems: list[str] = []

    def record(self, snapshot: str, source: Path):
        self.expected[snapshot] = manifest(source)

    def verify(self, when: str, snapshots=None):
        result = self.repo.run("check", "--read-data", check=False)
        if result.returncode:
            self.problems.append(f"{when}: check --read-data failed: {result.stderr.decode(errors='replace')[-800:]}")
        present = set(self.repo.snapshot_ids())
        for snap in snapshots if snapshots is not None else list(self.expected):
            if snap not in present:
                self.problems.append(f"{when}: snapshot {snap[:8]} is gone")
                continue
            target = self.work / "restore"
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir()
            restored = self.repo.run("restore", "--numeric-id", snap, target, check=False)
            if restored.returncode:
                err = restored.stderr.decode(errors="replace")
                reason = next((l.strip() for l in err.splitlines() if "not found" in l or "error" in l.lower()), err[-300:])
                self.problems.append(f"{when}: snapshot {snap[:8]} does not restore: {reason}")
                shutil.rmtree(target, ignore_errors=True)
                continue
            diff = compare(self.expected[snap], manifest(target))
            self.problems += [f"{when}: snapshot {snap[:8]}: {p}" for p in diff[:5]]
            shutil.rmtree(target, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("work")
    ap.add_argument("--iterations", type=int, default=int(os.environ.get("CRASH_ITERATIONS", "6")))
    ap.add_argument("--seed", type=int, default=int(os.environ.get("CRASH_SEED", str(time.time_ns() % 2**32))))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    work = Path(args.work).resolve(strict=True)
    print(f"crash: seed {args.seed}, {args.iterations} iterations", flush=True)

    repo = Repo(str(Path(args.binary).resolve(strict=True)), work / "repo")
    repo.run("init")
    source = work / "source"
    check = Check(repo, work)
    grow(source, rng, 200, 512 * 1024)
    t0 = time.monotonic()
    first = repo.backup(source)
    full = time.monotonic() - t0  # one full backup's duration, to aim the kills
    check.record(first, source)
    log = {"seed": args.seed, "backup_seconds": round(full, 2), "backup_kills": 0, "prune_kills": 0}

    # 1. SIGKILL backups of new data at random moments
    for i in range(args.iterations):
        grow(source, rng, 60, 1024 * 1024)
        if repo.killed("backup", "--force", ".", cwd=source, after=rng.uniform(0.05, max(0.2, full))):
            log["backup_kills"] += 1
        snap = repo.backup(source)  # the next backup completes
        check.record(snap, source)
        check.verify(f"after backup kill {i}", snapshots=[first, snap])

    # 2. SIGKILL prune at random moments, with snapshots forgotten so it has work
    for i in range(args.iterations):
        keep = sorted(check.expected)
        drop = rng.sample(keep, k=max(1, len(keep) // 3))
        for snap in drop:
            repo.run("forget", snap)
            del check.expected[snap]
        # prune is much faster than a backup: aim inside its first fraction of a second
        if repo.killed("prune", "--max-unused", "0", "--instant-delete", after=rng.uniform(0.01, 0.3)):
            log["prune_kills"] += 1
        check.verify(f"after prune kill {i}")
        grow(source, rng, 40, 512 * 1024)
        snap = repo.backup(source)  # backing up after a killed prune works
        check.record(snap, source)
        repo.run("prune", "--max-unused", "0")  # and a full prune afterwards too
        check.verify(f"after prune {i} completed")

    # 3. A backup while a prune runs (both complete or are rejected cleanly)
    grow(source, rng, 80, 1024 * 1024)
    for snap in rng.sample(sorted(check.expected), k=max(1, len(check.expected) // 2)):
        repo.run("forget", snap)
        del check.expected[snap]
    prune = subprocess.Popen(repo.argv("prune", "--max-unused", "0"), env=repo.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    backup = repo.run("backup", "--json", "--force", ".", cwd=source, check=False)
    _, prune_err = prune.communicate(timeout=1800)
    if backup.returncode == 0:
        check.record(json.loads(backup.stdout)["id"], source)
    # Upstream contract (two-phase prune): right after, the new snapshot may
    # reference blobs the prune dropped from the index, so check can fail; the
    # NEXT prune recovers the still-existing packs.  (With --instant-delete the
    # loss is permanent: never run that concurrently.)  Record the window,
    # require full consistency after the follow-up prune.
    window = repo.run("check", "--read-data", check=False)
    log["concurrent"] = {"backup_rc": backup.returncode, "prune_rc": prune.returncode,
                         "check_right_after": window.returncode,
                         "new_snapshot": json.loads(backup.stdout)["id"][:8] if backup.returncode == 0 else None}
    repo.run("prune")
    check.verify("after a backup during a prune, then a follow-up prune")

    print(json.dumps({**log, "snapshots_checked": len(check.expected), "problems": check.problems}, indent=2))
    return 1 if check.problems else 0


if __name__ == "__main__":
    sys.exit(main())
