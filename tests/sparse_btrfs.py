#!/usr/bin/env python3
"""Opt-in qualification on a marked, disposable Btrfs mount. Requires root."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    mount = Path(sys.argv[2]).resolve(strict=True)
    marker = mount / ".cocalc-disposable-sparse-qualification"
    assert os.geteuid() == 0, "requires root on an isolated test runner"
    assert marker.is_file(), "refusing an unmarked filesystem"
    assert os.path.ismount(mount), "must be a separate disposable mount"
    kind = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", str(mount)], text=True).strip()
    assert kind == "btrfs", kind
    root = Path(tempfile.mkdtemp(prefix="sparse-", dir=mount))
    subvolumes = []
    env = {
        "HOME": str(root), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
        "RUSTIC_PASSWORD": "disposable-fixture-only", "LC_ALL": "C.UTF-8",
    }

    def run(args, cwd=root):
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError(f"{args}: {result.returncode}\n{result.stderr}")
        return result.stdout

    def subvolume(name, source=None):
        dest = root / name
        if source is None:
            run(["btrfs", "subvolume", "create", str(dest)])
        else:
            run(["btrfs", "subvolume", "snapshot", "-r", str(source), str(dest)])
        subvolumes.append(dest)
        return dest

    repo = root / "repo"
    common = [binary, "--repository", str(repo), "--no-cache", "--no-progress"]
    try:
        run(["btrfs", "quota", "enable", str(mount)])
        source = subvolume("source")
        run(["btrfs", "qgroup", "limit", "4G", str(source)])
        with open(source / "huge", "xb") as output:
            output.truncate(10 * 1024**3)
            for offset in [0, 5 * 1024**3, 10 * 1024**3 - 4096]:
                output.seek(offset)
                output.write(os.urandom(4096))
            output.flush()
            os.fsync(output.fileno())
        with open(source / "mixed", "xb") as output:
            output.truncate(16 * 1024**2)
            for offset in range(0, 16 * 1024**2, 65536):
                output.seek(offset)
                output.write(os.urandom(4096))
            output.flush()
            os.fsync(output.fileno())
        (source / "ordinary").write_bytes(os.urandom(1024**2))
        os.link(source / "mixed", source / "linked")
        os.symlink("mixed", source / "symlink")
        run(common + ["init"])
        summaries = []
        parent = None
        for number in range(3):
            if number == 2:
                ordinary = source / "ordinary"
                before = ordinary.stat()
                with open(ordinary, "r+b") as output:
                    output.write(os.urandom(4096))
                os.utime(ordinary, ns=(before.st_atime_ns, before.st_mtime_ns))
            capture = subvolume(f"capture-{number}", source)
            assert (capture / "huge").stat().st_ino == (source / "huge").stat().st_ino
            args = common + ["backup", "--strict", "--json", "--no-scan", "--host", "btrfs-qualification"]
            if parent:
                args += ["--parent", parent]
            snapshot = json.loads(run(args + ["."], cwd=capture))
            parent = snapshot["id"]
            summary = snapshot["summary"]
            summaries.append(summary)
            if number == 1:
                assert summary["files_unmodified"] == summary["total_files_processed"], summary
                assert summary["data_added_files"] == 0, summary
            if number == 2:
                assert summary["files_changed"] == 1, summary
                assert summary["files_unmodified"] == summary["total_files_processed"] - 1, summary

        stage = subvolume("restore")
        run(["btrfs", "qgroup", "limit", "4G", str(stage)])
        # Demonstrate enforcement, not just successful execution of qgroup limit.
        probe = stage / "quota-probe"
        try:
            with open(probe, "xb") as output:
                try:
                    os.posix_fallocate(output.fileno(), 0, 5 * 1024**3)
                except OSError as error:
                    assert error.errno == errno.EDQUOT, error
                else:
                    raise AssertionError("4 GiB staging quota did not reject 5 GiB allocation")
        finally:
            probe.unlink(missing_ok=True)
        run(["btrfs", "filesystem", "sync", str(mount)])
        run(common + ["restore", "--strict", "--sparse", "by-content-required", parent, str(stage)])
        allocations = {}
        for name in ["huge", "mixed", "linked", "ordinary"]:
            original, restored = source / name, stage / name
            with open(restored, "rb") as output:
                os.fsync(output.fileno())
            assert restored.stat().st_size == original.stat().st_size
            allocated = restored.stat().st_blocks * 512
            assert allocated <= original.stat().st_blocks * 512 + 1024**2, name
            allocations[name] = allocated
            verify_extents(original, restored)
        assert (stage / "mixed").stat().st_ino == (stage / "linked").stat().st_ino
        assert os.readlink(stage / "symlink") == "mixed"
        run(common + ["check", "--read-data"])
        print(json.dumps({"ok": True, "summaries": summaries, "allocated": allocations}))
    finally:
        for subvol in reversed(subvolumes):
            subprocess.run(["btrfs", "subvolume", "delete", str(subvol)], check=True, timeout=60)
        shutil.rmtree(root)


def verify_extents(original, restored):
    """Compare every byte allocated in either file; both-hole regions are zeros."""
    with open(original, "rb") as left, open(restored, "rb") as right:
        size = os.fstat(left.fileno()).st_size
        ranges = []
        for file in [left, right]:
            offset = 0
            while offset < size:
                try:
                    start = os.lseek(file.fileno(), offset, os.SEEK_DATA)
                except OSError as error:
                    if error.errno == errno.ENXIO:
                        break
                    raise
                end = min(size, os.lseek(file.fileno(), start, os.SEEK_HOLE))
                assert offset <= start < end
                ranges.append((start, end))
                assert len(ranges) <= 10000
                offset = end
        budget = 32 * 1024**2
        for start, end in ranges:
            budget -= end - start
            assert budget >= 0, "unbounded extent verification"
            left.seek(start)
            right.seek(start)
            while start < end:
                count = min(end - start, 1024**2)
                assert left.read(count) == right.read(count)
                start += count


if __name__ == "__main__":
    main()
