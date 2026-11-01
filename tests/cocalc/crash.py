#!/usr/bin/env python3
"""Crash safety: SIGKILL backup and prune at random moments (every kill must
land), and run a backup inside a prune's window by construction (consistent
again after the next prune; an --instant-delete control must lose data).  After every kill:
  - the next backup completes;
  - `check --read-data` is clean;
  - every snapshot that should exist restores byte-for-byte (manifest equal).

Usage: crash.py RUSTIC WORK_DIR [--iterations N] [--seed S]

Kill points are random but reproducible from the printed seed.  Exit 1 on any
problem.  Needs no privileges itself (CI runs it with sudo only because its
loopback mounts are root-owned).
"""
import argparse
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rt.manifest import compare, manifest  # noqa: E402

MiB = 1024 * 1024
READY = "password is correct"  # logged once the repository is open
PLANNED = "- recover:"  # the last line of prune's plan, printed before it changes anything
ATTEMPTS = 8  # per kill: retry, aiming earlier, until one lands


def drain(stream, anchor: str, seen: threading.Event, lines=None):
    for raw in stream:
        line = raw.decode(errors="replace")
        if lines is not None:
            lines.append(line)
        if anchor in line:
            seen.set()


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

    def killed(self, *args, anchor: str, delay: float, cwd=None):
        """Start the command; once its output shows `anchor`, SIGKILL it after
        `delay` seconds.  Returns (killed, seconds it ran past the anchor, exit
        code); when it finished first, the seconds are its full time past the
        anchor, which aims the next kill."""
        proc = subprocess.Popen(self.argv(*args), cwd=cwd, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        seen = threading.Event()
        reader = threading.Thread(target=drain, args=(proc.stdout, anchor, seen), daemon=True)
        reader.start()
        while not seen.wait(0.01):
            if proc.poll() is not None:
                break
        t0 = time.monotonic()
        try:
            if not seen.is_set():  # finished (or failed) before the anchor
                return False, 0.0, proc.wait()
            rc = proc.wait(timeout=delay)
            return False, time.monotonic() - t0, rc
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            return True, delay, -signal.SIGKILL
        finally:
            reader.join()

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


def land_kill(repo: Repo, args, anchor: str, estimate: float, rng: random.Random,
              prepare, check: Check, what: str, cwd=None) -> tuple[float, bool]:
    """SIGKILL `args` at a random moment past `anchor`, retrying (and aiming
    inside the measured time) until a kill lands.  `prepare()` gives the
    command new work after a run that finished.  Returns (new estimate, landed)."""
    for _ in range(ATTEMPTS):
        landed, ran, rc = repo.killed(*args, anchor=anchor, delay=rng.uniform(0, estimate), cwd=cwd)
        if landed:
            return estimate, True
        if rc != 0:  # a run that got past us must still have succeeded
            check.problems.append(f"{what}: rustic {' '.join(args)} exited {rc} without being killed")
            return estimate, False
        estimate = max(0.002, min(estimate, ran) * 0.8)
        prepare()
    check.problems.append(f"{what}: no kill landed in {ATTEMPTS} attempts (aimed down to {estimate:.3f} s)")
    return estimate, False


def race(repo: Repo, check: Check, source: Path, prune_args, rng: random.Random) -> dict:
    """A backup that runs entirely inside a prune's window, by construction:
    data only a forgotten snapshot uses is back in the source; the prune is
    stopped (SIGSTOP) once it has printed its plan to drop that data, and the
    backup, which dedups against the still-indexed blobs, runs to completion
    before the prune continues."""
    extra = source / "race"
    grow(extra, rng, 60, 1024 * 1024)
    keep = source.parent / f"{source.name}-race-copy"
    shutil.rmtree(keep, ignore_errors=True)
    shutil.copytree(extra, keep)
    with_extra = repo.backup(source)
    shutil.rmtree(extra)
    check.record(repo.backup(source), source)
    repo.run("forget", with_extra)  # now only the source will use extra's blobs
    shutil.move(keep, extra)

    out: list[str] = []
    planned = threading.Event()
    prune = subprocess.Popen(repo.argv("prune", *prune_args), env=repo.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    reader = threading.Thread(target=drain, args=(prune.stdout, PLANNED, planned, out), daemon=True)
    reader.start()
    while not planned.wait(0.01) and prune.poll() is None:
        pass
    stopped = planned.is_set() and prune.poll() is None
    try:
        if stopped:
            prune.send_signal(signal.SIGSTOP)
        backup = repo.run("backup", "--json", "--force", ".", cwd=source, check=False)
    except BaseException:
        prune.kill()  # never leave a stopped prune behind
        raise
    finally:
        if stopped:
            prune.send_signal(signal.SIGCONT)
        prune.wait(timeout=1800)
        reader.join()
    removes = next((int(m[1]) for l in out if (m := re.search(r"total prune:\s+(\d+) blobs", l))), 0)
    result = {"prune": " ".join(prune_args), "stopped_at_plan": stopped, "plan_removes_blobs": removes,
              "backup_rc": backup.returncode, "prune_rc": prune.returncode}
    if not stopped or removes == 0:
        check.problems.append(f"race {result['prune']}: no window (prune stopped at plan: {stopped}, "
                              f"removes {removes} blobs): {''.join(out)[-600:]}")
    if backup.returncode or prune.returncode:  # lock-free: both must succeed
        check.problems.append(f"race {result['prune']}: backup exit {backup.returncode}, prune exit "
                              f"{prune.returncode}: {backup.stderr.decode(errors='replace')[-400:]}{''.join(out)[-400:]}")
    else:
        result["snapshot"] = json.loads(backup.stdout)["id"]
        check.record(result["snapshot"], source)
    result["check_right_after"] = repo.run("check", "--read-data", check=False).returncode
    return result


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
    binary = str(Path(args.binary).resolve(strict=True))
    t_start = time.monotonic()

    repo = Repo(binary, work / "repo")
    repo.run("init")
    source = work / "source"
    check = Check(repo, work)
    grow(source, rng, 200, 512 * 1024)
    first = repo.backup(source)
    check.record(first, source)
    log = {"seed": args.seed, "backup_kills": 0, "prune_kills": 0}

    # 1. SIGKILL backups of new data at random moments after the repository opens
    estimate = 2.0
    for i in range(args.iterations):
        grow(source, rng, 60, 1024 * 1024)
        estimate, landed = land_kill(repo, ("backup", "--force", "."), READY, estimate, rng,
                             lambda: grow(source, rng, 60, 1024 * 1024), check, f"backup kill {i}", cwd=source)
        log["backup_kills"] += landed
        snap = repo.backup(source)  # the next backup completes
        check.record(snap, source)
        check.verify(f"after backup kill {i}", snapshots=[first, snap])

    # 2. SIGKILL prune at random moments after its plan (when it starts changing
    #    the repository), with snapshots forgotten so it has work
    def more_prune_work():
        grow(source, rng, 20, 512 * 1024)
        check.record(repo.backup(source), source)
        victim = rng.choice(sorted(set(check.expected) - {first}))
        repo.run("forget", victim)
        del check.expected[victim]

    estimate = 0.3
    for i in range(args.iterations):
        keep = sorted(check.expected)
        for snap in rng.sample(keep, k=max(1, len(keep) // 3)):
            repo.run("forget", snap)
            del check.expected[snap]
        estimate, landed = land_kill(repo, ("prune", "--max-unused", "0", "--instant-delete"), PLANNED, estimate, rng,
                             more_prune_work, check, f"prune kill {i}")
        log["prune_kills"] += landed
        check.verify(f"after prune kill {i}")
        grow(source, rng, 40, 512 * 1024)
        check.record(repo.backup(source), source)  # backing up after a killed prune works
        repo.run("prune", "--max-unused", "0")  # and a full prune afterwards too
        check.verify(f"after prune {i} completed")

    # 3. A backup inside a two-phase prune's window.  Upstream contract: right
    #    after, the new snapshot may reference blobs the prune dropped from the
    #    index (check_right_after != 0); the NEXT prune recovers the still-existing
    #    packs, after which everything must verify.
    log["race"] = race(repo, check, source, ("--max-unused", "0"), rng)
    if log["race"]["check_right_after"] == 0:
        check.problems.append("race: check --read-data passed right after the race; upstream's window "
                              "should be visible there (a pack fallback would flip this expectation)")
    repo.run("prune")
    check.verify("after a backup inside a prune's window, then a follow-up prune")

    # Negative control: the same race with --instant-delete loses the data for
    # good, and verify must say so.  If it doesn't, the race missed the window
    # (or verify is blind), and phase 3 proves nothing.
    ctl_work = work / "control"
    ctl_work.mkdir()
    ctl = Repo(binary, ctl_work / "repo")
    ctl.run("init")
    ctl_check = Check(ctl, ctl_work)
    ctl_source = ctl_work / "source"
    grow(ctl_source, rng, 40, 512 * 1024)
    ctl_check.record(ctl.backup(ctl_source), ctl_source)
    log["control"] = race(ctl, ctl_check, ctl_source, ("--max-unused", "0", "--instant-delete"), rng)
    setup_problems = list(ctl_check.problems)
    follow_up = ctl.run("prune", check=False)  # may itself fail on the missing blobs
    log["control"]["follow_up_prune_rc"] = follow_up.returncode
    ctl_check.verify("control")
    lost = ctl_check.problems[len(setup_problems):]
    log["control"]["loss_detected"] = bool(lost)
    if setup_problems:
        check.problems += [f"control: {p}" for p in setup_problems]
    elif not lost:
        check.problems.append("control: a backup inside an --instant-delete prune's window lost nothing; "
                              "the race missed the window or verify cannot see the loss")

    log["seconds"] = round(time.monotonic() - t_start, 1)
    print(json.dumps({**log, "snapshots_checked": len(check.expected), "problems": check.problems}, indent=2))
    return 1 if check.problems else 0


if __name__ == "__main__":
    sys.exit(main())
