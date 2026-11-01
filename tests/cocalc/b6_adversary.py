#!/usr/bin/env python3
"""Adversarial checks for `backup --restored-before TIME --parent ID` (B6).

Oracle: after the backup, restoring the new snapshot must give exactly the
tree that was on disk when the backup ran.  A backup that keeps the parent's
content for a file whose disk content differs is a *divergence*.

Each scenario prepares a restored tree (some within the option's contract,
some deliberately outside it), backs it up with the option, and reports:
  - divergent: paths whose backed-up content differs from the disk;
  - reused: files the option matched without reading (from the summary);
plus a control backup without the option.

Usage: b6_adversary.py RUSTIC WORK_DIR [--only NAME,...]
Exit 1 if an in-contract scenario diverges, or the control does.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rt.manifest import compare, manifest  # noqa: E402

MiB = 1024 * 1024
MARGIN = 2.0  # rustic matches only ctime < TIME - MARGIN (RESTORED_BEFORE_MARGIN)


class R:
    def __init__(self, binary, work: Path):
        self.binary, self.repo, self.cache = binary, work / "repo", work / "cache"
        self.env = {**os.environ, "RUSTIC_PASSWORD": "fixture-only", "LC_ALL": "C.UTF-8"}

    def argv(self, *a):
        return [self.binary, "-r", str(self.repo), "--cache-dir", str(self.cache), "--no-progress", *map(str, a)]

    def run(self, *a, cwd=None, check=True):
        p = subprocess.run(self.argv(*a), cwd=cwd, env=self.env, capture_output=True, timeout=1800)
        if check and p.returncode:
            raise RuntimeError(f"rustic {' '.join(map(str, a))}: {p.stderr.decode(errors='replace')[-1200:]}")
        return p

    def backup(self, src: Path, *extra):
        out = json.loads(self.run("backup", "--json", *extra, ".", cwd=src).stdout)
        s = out.get("summary") or {}
        return out["id"], s

    def restore(self, snap, dest: Path, *extra):
        dest.mkdir(parents=True, exist_ok=True)
        self.run("restore", "--numeric-id", *extra, snap, dest)


def stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="microseconds")


def write(p: Path, data: bytes, mtime=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, ns=(mtime, mtime))


def tree(src: Path, n=40):
    for i in range(n):
        write(src / f"d{i % 4}" / f"f{i:03d}", os.urandom(4096 + 997 * i))


def reused(summary) -> int:
    # files the backup did not read: unmodified files
    return summary.get("files_unmodified", 0)


def check_backup(r: R, src: Path, snap: str, work: Path) -> list[str]:
    out = work / "verify"
    shutil.rmtree(out, ignore_errors=True)
    r.restore(snap, out)
    diff = compare(manifest(src), manifest(out))
    shutil.rmtree(out, ignore_errors=True)
    return diff


def aged():
    """Wait until everything written so far is older than TIME - MARGIN, as
    after a real restore that took longer than the margin."""
    time.sleep(MARGIN + 0.5)


def scenario(name, in_contract, prepare):
    return {"name": name, "in_contract": in_contract, "prepare": prepare}


# Each prepare(r, work) -> (restored_dir, parent_id, TIME as float seconds)
def s_baseline(r, w):
    src = w / "src"; tree(src)
    parent, _ = r.backup(src)
    dest = w / "dest"; r.restore(parent, dest)
    aged()
    return dest, parent, time.time()


def s_write_after_time_no_margin(r, w):
    """Outside: TIME taken right after the restore with no margin, then a
    same-size, same-mtime rewrite immediately after (coarse ctime)."""
    src = w / "src"; tree(src)
    parent, _ = r.backup(src)
    dest = w / "dest"; r.restore(parent, dest)
    aged()
    # size and mtime from the source: stat-ing the target would make its next
    # ctime fine-grained (multigrain) and hide the coarse-clock lag
    st = (src / "d1" / "f001").stat()
    t = time.time()
    write(dest / "d1" / "f001", os.urandom(st.st_size), mtime=st.st_mtime_ns)
    return dest, parent, t


def s_write_after_time_early(r, w):
    """In: the same rewrite, with TIME taken a moment BEFORE the restore
    returned (a conservative caller)."""
    dest, parent, t = s_write_after_time_no_margin(r, w)
    return dest, parent, t - 0.5


def s_stale_nonempty_target(r, w):
    """Outside (target not empty): a pre-existing file with the parent's size
    and mtime but different content; the restore keeps it."""
    src = w / "src"; tree(src)
    parent, _ = r.backup(src)
    dest = w / "dest"
    f = src / "d2" / "f002"
    st = f.stat()
    write(dest / "d2" / "f002", os.urandom(st.st_size), mtime=st.st_mtime_ns)
    time.sleep(MARGIN + 0.5)
    r.restore(parent, dest)
    aged()
    return dest, parent, time.time()


def s_wrong_parent(r, w):
    """Outside (parent is not the restored snapshot): snapshot B has a file
    rewritten with the same size and mtime as in A; restore B, claim A."""
    src = w / "src"; tree(src)
    a, _ = r.backup(src)
    f = src / "d3" / "f003"
    st = f.stat()
    write(f, os.urandom(st.st_size), mtime=st.st_mtime_ns)
    b, _ = r.backup(src)
    dest = w / "dest"; r.restore(b, dest)
    aged()
    return dest, a, time.time()


def s_interrupted_then_rerun(r, w):
    """In: a restore killed midway, then rerun to completion."""
    src = w / "src"
    for i in range(30):
        write(src / f"big{i:02d}", os.urandom(2 * MiB))
    tree(src)
    parent, _ = r.backup(src)
    dest = w / "dest"; dest.mkdir()
    p = subprocess.Popen(r.argv("restore", "--numeric-id", parent, dest), env=r.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    p.send_signal(signal.SIGKILL); p.wait()
    r.restore(parent, dest)
    aged()
    return dest, parent, time.time()


def s_killed_not_rerun(r, w):
    """Outside (restore not complete): killed midway and never rerun."""
    src = w / "src"
    for i in range(30):
        write(src / f"big{i:02d}", os.urandom(2 * MiB))
    tree(src)
    parent, _ = r.backup(src)
    dest = w / "dest"; dest.mkdir()
    p = subprocess.Popen(r.argv("restore", "--numeric-id", parent, dest), env=r.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    p.send_signal(signal.SIGKILL); p.wait()
    aged()
    return dest, parent, time.time()


def s_links_and_specials(r, w):
    """In: hardlinks, symlinks (dangling too), a fifo, empty and sparse files."""
    src = w / "src"; tree(src, 12)
    os.link(src / "d0" / "f000", src / "d0" / "hard")
    os.symlink("f004", src / "d0" / "sym")
    os.symlink("missing", src / "d0" / "dangling")
    os.mkfifo(src / "d0" / "fifo")
    (src / "empty").write_bytes(b"")
    with open(src / "sparse", "wb") as f:
        f.seek(8 * MiB); f.write(b"end")
    parent, _ = r.backup(src)
    dest = w / "dest"; r.restore(parent, dest)
    aged()
    return dest, parent, time.time()


def s_chmod_pass(r, w):
    """In: a permission pass after the restore (bumps every ctime)."""
    dest, parent, _ = s_baseline(r, w)
    t = time.time()
    time.sleep(0.5)
    for p in dest.rglob("*"):
        if p.is_file():
            os.chmod(p, 0o640)
    return dest, parent, t


def s_chmod_then_time(r, w):
    """In: a permission pass after the restore, then TIME (taken after the
    pass, as a careful caller would): reuse, with the new modes backed up."""
    src = w / "src"; tree(src)
    parent, _ = r.backup(src)
    dest = w / "dest"; r.restore(parent, dest)
    for p in dest.rglob("*"):
        if p.is_file():
            os.chmod(p, 0o600)
    aged()
    return dest, parent, time.time()


SCENARIOS = [
    scenario("baseline", True, s_baseline),
    scenario("write_after_time_early", True, s_write_after_time_early),
    scenario("write_right_after_time", True, s_write_after_time_no_margin),
    scenario("interrupted_then_rerun", True, s_interrupted_then_rerun),
    scenario("links_and_specials", True, s_links_and_specials),
    scenario("chmod_pass", True, s_chmod_pass),
    scenario("chmod_then_time", True, s_chmod_then_time),
    scenario("stale_nonempty_target", False, s_stale_nonempty_target),
    scenario("wrong_parent", False, s_wrong_parent),
    scenario("killed_not_rerun", False, s_killed_not_rerun),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("work")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    binary = str(Path(args.binary).resolve(strict=True))
    work = Path(args.work).resolve(strict=True)
    if "--restored-before" not in subprocess.run([binary, "backup", "--help"], capture_output=True, text=True).stdout:
        print("binary has no --restored-before")
        return 2
    report, problems = {}, []
    if not args.only:
        w = work / "future"; shutil.rmtree(w, ignore_errors=True); w.mkdir()
        r = R(binary, w); r.run("init")
        src = w / "src"; tree(src, 4)
        parent, _ = r.backup(src)
        p = r.run("backup", "--parent", parent, "--restored-before", stamp(time.time() + 60), ".",
                  cwd=src, check=False)
        report["future_time"] = {"rc": p.returncode}
        if p.returncode == 0 or b"future" not in p.stderr:
            problems.append(f"a future --restored-before was accepted (rc {p.returncode})")
        print(f"future_time                  rc={p.returncode} ({'rejected' if p.returncode else 'ACCEPTED'})", flush=True)
        shutil.rmtree(w, ignore_errors=True)
    for sc in SCENARIOS:
        if args.only and sc["name"] not in args.only.split(","):
            continue
        w = work / sc["name"]
        shutil.rmtree(w, ignore_errors=True)
        w.mkdir()
        r = R(binary, w)
        r.run("init")
        dest, parent, t = sc["prepare"](r, w)
        res = {"in_contract": sc["in_contract"]}
        snap, summ = r.backup(dest, "--parent", parent, "--restored-before", stamp(t))
        res["reused"] = reused(summ)
        res["divergent"] = check_backup(r, dest, snap, w)[:5]
        csnap, csumm = r.backup(dest, "--parent", parent)
        res["control_reused"] = reused(csumm)
        cdiff = check_backup(r, dest, csnap, w)
        if cdiff:
            problems.append(f"{sc['name']}: the control (no option) diverges: {cdiff[:3]}")
        if sc["in_contract"] and res["divergent"]:
            problems.append(f"{sc['name']}: in-contract divergence: {res['divergent'][:3]}")
        report[sc["name"]] = res
        print(f"{sc['name']:28s} {'in ' if sc['in_contract'] else 'OUT'} reused={res['reused']:3d} "
              f"control_reused={res['control_reused']:3d} divergent={len(res['divergent'])}", flush=True)
        shutil.rmtree(w, ignore_errors=True)
    print(json.dumps({"scenarios": report, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
