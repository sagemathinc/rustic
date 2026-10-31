#!/usr/bin/env python3
"""Back up a tree of every kind of entry, restore it, and compare.

Usage: roundtrip.py RUSTIC_BINARY WORK_DIR

WORK_DIR is a fresh directory on the filesystem under test (CI runs this on
btrfs, btrfs with compress=zstd, and ext4 loopbacks).  As root it also covers
owners, devices and a capability-like security xattr; otherwise those cases
are skipped and reported.  Exits non-zero with the differences listed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


def make_tree(root: Path, as_root: bool) -> list[str]:
    """Create the fixture; return the names of skipped cases."""
    skipped = []
    (root / "dir" / "nested" / "deep").mkdir(parents=True)
    (root / "empty-dir").mkdir()
    (root / "file").write_bytes(b"hello\n")
    (root / "empty-file").write_bytes(b"")
    (root / "dir" / "random").write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    (root / "dir" / "nested" / "deep" / "zeros").write_bytes(bytes(1024 * 1024))
    # sparse: a hole between two data regions
    with open(root / "sparse", "wb") as f:
        f.write(b"start")
        f.seek(64 * 1024 * 1024)
        f.write(b"end")
    # names: non-UTF-8, long, spaces and newlines
    os.mkdir(os.fsencode(root) + b"/non-utf8-\xff\xfe")
    (root / ("long-" + "x" * 240)).write_bytes(b"long")
    (root / "with space\nand newline").write_bytes(b"odd")
    # links
    os.symlink("file", root / "link-rel")
    os.symlink("/nonexistent/target", root / "link-dangling")
    os.link(root / "file", root / "hardlink-a")
    os.link(root / "file", root / "dir" / "hardlink-b")
    # modes and times
    (root / "exec").write_bytes(b"#!/bin/sh\n")
    os.chmod(root / "exec", 0o755)
    (root / "private").write_bytes(b"secret")
    os.chmod(root / "private", 0o600)
    (root / "setgid-dir").mkdir()
    os.chmod(root / "setgid-dir", 0o2775)
    os.utime(root / "file", ns=(1_600_000_000_123_456_789, 1_600_000_000_123_456_789))
    # special files
    os.mkfifo(root / "fifo")
    # xattrs
    try:
        os.setxattr(root / "file", "user.cocalc", b"value")
    except OSError:
        skipped.append("user xattr (filesystem refuses)")
    if as_root:
        os.chown(root / "private", 1234, 5678)
        os.lchown(root / "link-rel", 4321, 8765)
        os.mknod(root / "chardev", stat.S_IFCHR | 0o600, os.makedev(1, 3))
        os.mknod(root / "whiteout", stat.S_IFCHR | 0o000, os.makedev(0, 0))
    else:
        skipped.extend(["owners", "device nodes"])
    return skipped


def manifest(root: Path) -> dict:
    """Path -> everything a restore must reproduce."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(os.fsencode(root)):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, os.fsencode(root)).decode("utf-8", "surrogateescape")
            info = os.lstat(path)
            entry = {
                "mode": oct(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size": info.st_size if stat.S_ISREG(info.st_mode) else None,
            }
            if not stat.S_ISDIR(info.st_mode):
                entry["mtime_ns"] = info.st_mtime_ns
            if stat.S_ISREG(info.st_mode):
                with open(path, "rb") as f:
                    entry["sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
                entry["nlink"] = info.st_nlink
            elif stat.S_ISLNK(info.st_mode):
                entry["target"] = os.readlink(path).decode("utf-8", "surrogateescape")
            elif stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode):
                entry["rdev"] = [os.major(info.st_rdev), os.minor(info.st_rdev)]
            try:
                names = os.listxattr(path, follow_symlinks=False)
            except OSError:
                names = []
            entry["xattrs"] = {
                n: os.getxattr(path, n, follow_symlinks=False).hex() for n in sorted(names) if n.startswith("user.")
            }
            out[rel] = entry
    return out


def hardlink_groups(root: Path) -> list[list[str]]:
    groups = {}
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                groups.setdefault(info.st_ino, []).append(os.path.relpath(path, root))
    return sorted(sorted(g) for g in groups.values())


def main() -> int:
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    work = Path(sys.argv[2]).resolve(strict=True)
    as_root = os.geteuid() == 0
    source, restored, repo = work / "source", work / "restored", work / "repo"
    for path in (source, restored):
        path.mkdir()
    env = {**os.environ, "RUSTIC_PASSWORD": "fixture-only", "LC_ALL": "C.UTF-8"}
    common = [binary, "--repository", str(repo), "--no-progress"]

    def run(*args, cwd=work):
        result = subprocess.run([*common, *args], cwd=cwd, env=env, capture_output=True, timeout=600)
        if result.returncode:
            sys.stderr.write(result.stderr.decode(errors="replace"))
            raise SystemExit(f"rustic {args[0]} failed with {result.returncode}")
        return result.stdout

    skipped = make_tree(source, as_root)
    run("init")
    snapshot = json.loads(run("backup", "--json", ".", cwd=source))["id"]
    run("restore", "--numeric-id", snapshot, str(restored))
    run("check", "--read-data")

    want, got = manifest(source), manifest(restored)
    problems = []
    for rel in sorted(set(want) | set(got)):
        if rel not in got:
            problems.append(f"missing: {rel!r}")
        elif rel not in want:
            problems.append(f"extra: {rel!r}")
        elif want[rel] != got[rel]:
            diff = {k: (want[rel].get(k), got[rel].get(k)) for k in want[rel] if want[rel].get(k) != got[rel].get(k)}
            problems.append(f"differs: {rel!r} {diff}")
    if hardlink_groups(source) != hardlink_groups(restored):
        problems.append(f"hardlink groups: {hardlink_groups(source)} != {hardlink_groups(restored)}")
    fs = subprocess.run(["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "-T", str(work)], capture_output=True, text=True).stdout.strip()
    print(json.dumps({"filesystem": fs, "entries": len(want), "skipped": skipped, "problems": problems}, indent=2))
    shutil.rmtree(work / "restored", ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
