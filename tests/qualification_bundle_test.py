#!/usr/bin/env python3
import copy
from pathlib import Path
import tarfile
import tempfile
import unittest

from qualification_bundle import archive_tree, digest, validate_report


class BundleTest(unittest.TestCase):
    def report(self):
        return {"schema_version": 1, "ok": True, "binary_sha256": "a" * 64,
                "immutable_snapshots": True, "quota_bytes": 4 * 1024**3,
                "dense_probe_errno": "EDQUOT", "byte_verification": "union-of-extents",
                "allocated": {"huge": 12288, "mixed": 1024**2, "linked": 1024**2},
                "summaries": [{}, {"total_files_processed": 5, "files_unmodified": 5, "data_added_files": 0},
                              {"total_files_processed": 5, "files_unmodified": 4, "files_changed": 1}]}

    def test_report_must_belong_to_binary_and_cover_quota_and_reuse(self):
        report = self.report()
        validate_report(report, "a" * 64)
        for key, value in [("ok", False), ("schema_version", True), ("binary_sha256", "b" * 64),
                           ("immutable_snapshots", False), ("dense_probe_errno", "ENOSPC"),
                           ("quota_bytes", 8 * 1024**3), ("summaries", [])]:
            with self.assertRaises(ValueError):
                validate_report({**report, key: value}, "a" * 64)
        for key in ["huge", "mixed", "linked"]:
            for value in [None, True, -1, 10 * 1024**3]:
                invalid = copy.deepcopy(report)
                invalid["allocated"][key] = value
                with self.assertRaises(ValueError):
                    validate_report(invalid, "a" * 64)
        invalid = copy.deepcopy(report)
        invalid["summaries"][1]["files_unmodified"] = 0
        with self.assertRaises(ValueError):
            validate_report(invalid, "a" * 64)

    def test_archive_headers_are_stable_and_do_not_include_host_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            stage.mkdir()
            (stage / "rustic").write_bytes(b"binary fixture")
            (stage / "manifest.json").write_text("{}")
            archive_tree(stage, root / "a.tar.xz", 100)
            (stage / "rustic").touch()
            archive_tree(stage, root / "b.tar.xz", 100)
            self.assertEqual(digest(root / "a.tar.xz"), digest(root / "b.tar.xz"))
            with tarfile.open(root / "a.tar.xz") as archive:
                for entry in archive.getmembers():
                    self.assertEqual((entry.uid, entry.gid, entry.mtime), (0, 0, 100))
                    self.assertEqual(entry.mode, 0o755 if entry.name == "rustic" else 0o644)
            (stage / "link").symlink_to("rustic")
            with self.assertRaises(ValueError):
                archive_tree(stage, root / "invalid.tar.xz", 100)


if __name__ == "__main__":
    unittest.main()
