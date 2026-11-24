"""Self-tests of the round-trip oracle: each difference a broken restore causes is reported.

Each test builds a small tree, makes the one change a broken restore would make,
and checks the exact message the mutant proofs match (tests/cocalc/mutants).
Run with: python3 -B -m unittest discover -s tests/cocalc -p 'test_*.py'
No rustic binary and no root are needed.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rt.manifest import compare, manifest  # noqa: E402
from rt.snapshot import excluded_restored, expected, rebase  # noqa: E402


def xattrs_supported(path: Path) -> bool:
    try:
        os.setxattr(path, "user.probe", b"1")
        os.removexattr(path, "user.probe")
        return True
    except OSError:
        return False


class Oracle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "empty-dir").mkdir()
        (self.src / "file").write_bytes(b"content")
        os.utime(self.src / "file", ns=(1_600_000_000_123_456_789, 1_600_000_000_123_456_789))
        (self.src / "a").write_bytes(b"linked")
        os.link(self.src / "a", self.src / "b")
        with open(self.src / "sparse", "wb") as f:
            f.write(b"x")
            f.seek(8 * 1024 * 1024)
            f.write(b"y")
        os.mkdir(os.fsencode(self.src) + b"/name-\xff")

    def tearDown(self):
        self._tmp.cleanup()

    def copy(self) -> Path:
        dst = self.tmp / "dst"
        shutil.copytree(self.src, dst, symlinks=True)
        # copytree breaks hard links; restore the group as the source has it
        os.unlink(dst / "b")
        os.link(dst / "a", dst / "b")
        # and copies dense; make the hole again
        os.unlink(dst / "sparse")
        with open(dst / "sparse", "wb") as f:
            f.write(b"x")
            f.seek(8 * 1024 * 1024)
            f.write(b"y")
        for p in ("file", "sparse", "a"):
            st = os.stat(self.src / p)
            os.utime(dst / p, ns=(st.st_atime_ns, st.st_mtime_ns))
        return dst

    def test_identical_trees_have_no_problems(self):
        self.assertEqual(compare(manifest(self.src), manifest(self.copy()), check_holes=True), [])

    def test_a1_missing_xattr(self):
        if not xattrs_supported(self.src / "file"):
            self.skipTest("no user xattrs on this filesystem")
        os.setxattr(self.src / "file", "user.cocalc", b"v")
        dst = self.copy()
        os.removexattr(dst / "file", "user.cocalc")
        problems = compare(manifest(self.src), manifest(dst))
        self.assertTrue(any(p.startswith("differs: 'file' {") and "'xattrs'" in p for p in problems), problems)

    def test_a2_hole_allocated(self):
        if not manifest(self.src)["sparse"]["_holes"]:
            self.skipTest("this filesystem keeps no holes")
        dst = self.copy()
        os.unlink(dst / "sparse")
        with open(dst / "sparse", "wb") as f:  # dense: zeros written where the hole was
            f.write(b"x" + bytes(8 * 1024 * 1024 - 1) + b"y")
        problems = compare(manifest(self.src), manifest(dst), check_holes=True)
        self.assertTrue(any(p.startswith("hole allocated: 'sparse' bytes ") for p in problems), problems)

    def test_a3_hardlinks_restored_as_copies(self):
        dst = self.copy()
        os.unlink(dst / "b")
        shutil.copy2(dst / "a", dst / "b")
        problems = compare(manifest(self.src), manifest(dst))
        self.assertTrue(any(p.startswith("hardlink groups: [['a', 'b']] != []") for p in problems), problems)

    def test_a4_mtime_at_whole_seconds(self):
        dst = self.copy()
        os.utime(dst / "file", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        problems = compare(manifest(self.src), manifest(dst))
        self.assertTrue(any(p.startswith("differs: 'file' {") and "'mtime_ns'" in p for p in problems), problems)

    def test_a5_lossy_names(self):
        dst = self.copy()
        os.rename(os.fsencode(dst) + b"/name-\xff", dst / "name-�")
        problems = compare(manifest(self.src), manifest(dst))
        self.assertIn("missing: 'name-\\udcff'", problems)
        self.assertIn("extra: 'name-�'", problems)

    def test_a6_empty_dir_skipped(self):
        dst = self.copy()
        os.rmdir(dst / "empty-dir")
        self.assertIn("missing: 'empty-dir'", compare(manifest(self.src), manifest(dst)))


class Excluded(unittest.TestCase):
    M = {".": {"kind": "dir"}, ".snapshots": {}, ".snapshots/x": {}, ".snapshots/y/z": {}, ".snapshotsX": {}, "a": {}}

    def test_filter_drops_snapshots_and_descendants_only(self):
        self.assertEqual(sorted(expected(self.M)), [".", ".snapshotsX", "a"])
        self.assertEqual(excluded_restored(self.M), ["excluded path restored: '.snapshots'",
                                                     "excluded path restored: '.snapshots/x'",
                                                     "excluded path restored: '.snapshots/y/z'"])
        self.assertEqual(excluded_restored(expected(self.M)), [])
        with self.assertRaises(ValueError):  # a nested .snapshots is not modelled, so it is refused
            expected({**self.M, "a/.snapshots": {}})
        self.assertEqual(sorted(rebase({"hl": {"k": 1}, "hl/x": {}, "hlx": {}, "y": {}}, "hl")), [".", "x"])


if __name__ == "__main__":
    unittest.main()
