#!/usr/bin/env python3
"""Tests for finish.py's checks, each on its own.

The summary fixture follows llvm-cov's documented report columns; it is NOT captured
real output (none exists yet).  The reader is confirmed on the first real summary.txt
at promotion (SPEC.md)."""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("fin", os.path.join(HERE, "finish.py"))
fin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fin)

SHA40 = "a" * 40
H = "b" * 64


def good_record(**over):
    rec = {"run": "A", "head_sha": SHA40, "start_utc": "2026-11-23T21:00:00Z", "end_utc": "2026-11-23T21:30:00Z",
           "nproc": "4", "uname": "Linux x 6.8.0 x86_64", "df_before": "/dev/root 1 2 3 4% /", "df_after": "/dev/root 1 2 3 4% /",
           "rustc_vv": "rustc 1.95.0 (59807616e 2026-04-14);binary: rustc;host: x86_64-unknown-linux-gnu;release: 1.95.0;LLVM version: 22;", "cargo_v": "cargo 1.95.0",
           "llvm_cov_version": "cargo-llvm-cov 0.9.2", "cargo_path": "/c", "rustc_path": "/r",
           "cargo_llvm_cov_path": "/l", "llvm-profdata_path": "/p", "llvm-cov_path": "/v", "export_sha256": H,
           "cargo_lock_sha_before": H, "cargo_lock_sha_after": H, "cargo_sha256": H, "rustc_sha256": H,
           "cargo_llvm_cov_sha256": H, "llvm-profdata_sha256": H, "llvm-cov_sha256": H,
           "cargo_lock_git_diff_rc": "0", "profraw_clean_rc": "0", "lcov_rc": "0", "summary_rc": "0", "json_rc": "0",
           "test_rc": "0"}
    rec.update(over)
    return rec


SUMMARY = """Filename                      Regions    Missed Regions     Cover   Functions  Missed Functions  Executed       Lines      Missed Lines     Cover    Branches   Missed Branches     Cover
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
src/lib.rs                         10                 4    60.00%           2                 1    50.00%          20                 5    75.00%           0                 0         -
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
TOTAL                              10                 4    60.00%           2                 1    50.00%          20                 5    75.00%           0                 0         -
"""


class Version(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(fin.version_ok("cargo-llvm-cov 0.9.2"))
        self.assertTrue(fin.version_ok("cargo-llvm-cov 0.9.2\n"))

    def test_rejects(self):
        for v in ("cargo-llvm-cov 0.9.20", "cargo-llvm-cov 0.9.2-rc1", "cargo-llvm-cov 0.9.2 (abc)", "cargo-llvm-cov 0.9.1",
                  "cargo-llvm-cov 0x9.2", "", "error: no such command: `llvm-cov`"):
            self.assertFalse(fin.version_ok(v), v)


class Record(unittest.TestCase):
    def test_good(self):
        self.assertEqual(fin.check_record(good_record(), SHA40, "1.95.0"), [])

    def test_each_required_field(self):
        for k in fin.NONEMPTY + fin.SHA256_FIELDS:
            for bad in (None, ""):
                rec = good_record()
                if bad is None:
                    rec.pop(k)
                else:
                    rec[k] = bad
                self.assertTrue(fin.check_record(rec, SHA40, "1.95.0"), k)

    def test_each_rc(self):
        for k in fin.ZERO_RC:
            for bad in ("1", "no-flag", "", None):
                rec = good_record(**{k: bad}) if bad is not None else {x: v for x, v in good_record().items() if x != k}
                self.assertTrue(fin.check_record(rec, SHA40, "1.95.0"), (k, bad))

    def test_lock_changed(self):
        self.assertIn("Cargo.lock changed", fin.check_record(good_record(cargo_lock_sha_after="c" * 64), SHA40, "1.95.0"))

    def test_head(self):
        self.assertTrue(fin.check_record(good_record(), "c" * 40, "1.95.0"))
        self.assertTrue(fin.check_record(good_record(), "", "1.95.0"))
        self.assertTrue(fin.check_record(good_record(head_sha=SHA40), SHA40[:39], "1.95.0"))

    def test_version_in_record(self):
        self.assertTrue(fin.check_record(good_record(llvm_cov_version="cargo-llvm-cov 0.9.20"), SHA40, "1.95.0"))

    def test_times(self):
        self.assertTrue(fin.check_record(good_record(end_utc="2026-11-23T20:00:00Z"), SHA40, "1.95.0"))
        self.assertTrue(fin.check_record(good_record(start_utc="yesterday"), SHA40, "1.95.0"))


class Toolchain(unittest.TestCase):
    # real rustc -vV lines recorded by CI runs 45273226152 (core, 1.95.0) and 45273259059 (CLI, 1.94.0), ';'-joined
    REAL_195 = "rustc 1.95.0 (59807616e 2026-04-14);binary: rustc;commit-hash: 59807616e1fa2540724bfbac14d7976d7e4a3860;commit-date: 2026-04-14;host: x86_64-unknown-linux-gnu;release: 1.95.0;LLVM version: 22"
    REAL_194 = "rustc 1.94.0 (4a4ef493e 2026-03-02);binary: rustc;commit-hash: 4a4ef493e3a1488c6e321570238084b38948f6db;commit-date: 2026-03-02;host: x86_64-unknown-linux-gnu;release: 1.94.0;LLVM version: 21"

    def test_declared_release(self):
        self.assertTrue(fin.rustc_ok(self.REAL_195, "1.95.0"))
        self.assertTrue(fin.rustc_ok(self.REAL_194, "1.94.0"))
        self.assertEqual(fin.check_record(good_record(rustc_vv=self.REAL_194), SHA40, "1.94.0"), [])

    def test_other_release_rejected(self):
        self.assertFalse(fin.rustc_ok(self.REAL_194, "1.95.0"))
        self.assertFalse(fin.rustc_ok(self.REAL_195, "1.94.0"))
        for v in (self.REAL_195.replace("release: 1.95.0", "release: 1.95.01"),
                  self.REAL_195.replace("release: 1.95.0", "release: 1.95.0-nightly"), "rustc 1.95.0", ""):
            self.assertFalse(fin.rustc_ok(v, "1.95.0"), v)
        self.assertTrue(fin.check_record(good_record(rustc_vv=self.REAL_194), SHA40, "1.95.0"))

    def test_declared_value_must_be_a_release(self):
        """A missing or malformed declaration never passes, even if rustc_vv happens to contain it."""
        for bad in ("", "1.95", "1.95.0 ", "stable", "1.95.0;release: 1.95.0"):
            self.assertFalse(fin.rustc_ok(self.REAL_195, bad), bad)
            self.assertTrue(fin.check_record(good_record(rustc_vv=self.REAL_195), SHA40, bad), bad)
        # a malformed declaration that rustc -vV happens to contain must still fail
        for bad in ("1.95", "stable", ""):
            crafted = self.REAL_195 + f";release: {bad}"
            self.assertFalse(fin.rustc_ok(crafted, bad), bad)

    def test_release_line_exactly_once(self):
        self.assertFalse(fin.rustc_ok(self.REAL_195 + ";release: 1.95.0", "1.95.0"))


class Setup(unittest.TestCase):
    def test_setup(self):
        ok = {"install_rc": "0", "parser_selftest_rc": "0", "changed_paths_rc": "0",
              "core_lock_entry_rc": "0", "btrfs_progs_rc": "0"}
        for run in ("A", "A-hooks", "B", "C"):
            self.assertEqual(fin.check_setup(ok, True, run), [], run)
        self.assertEqual(fin.check_setup({"install_rc": "0", "parser_selftest_rc": "0"}, False, "A"), [])
        need = {"A": ["install_rc", "parser_selftest_rc", "changed_paths_rc"],
                "B": ["install_rc", "parser_selftest_rc", "changed_paths_rc", "core_lock_entry_rc"],
                "C": ["install_rc", "parser_selftest_rc", "changed_paths_rc", "core_lock_entry_rc", "btrfs_progs_rc"]}
        for run, keys in need.items():
            for k in keys:
                bad = dict(ok); bad[k] = "1"
                self.assertTrue(fin.check_setup(bad, True, run), (run, k))
                bad.pop(k)
                self.assertTrue(fin.check_setup(bad, True, run), (run, k))
        self.assertEqual(fin.check_setup({"install_rc": "0", "parser_selftest_rc": "0", "changed_paths_rc": "0"}, True, "A"), [])
        self.assertTrue(fin.check_setup({}, False, "A"))


class Tests(unittest.TestCase):
    OK = "running 3 tests\ntest a ... ok\ntest result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"

    def test_ok(self):
        p, info = fin.check_tests(self.OK, {"test_rc": "0"})
        self.assertEqual((p, info["passed"], info["ignored"]), ([], 3, 1))

    def test_failures(self):
        bad = "test x::y ... FAILED\ntest result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"
        p, info = fin.check_tests(bad, {"test_rc": "101"})
        self.assertEqual(info["failed_names"], ["x::y"])
        self.assertEqual(len(p), 2)
        self.assertTrue(fin.check_tests("", {"test_rc": "0"})[0])
        self.assertTrue(fin.check_tests(self.OK, {"test_rc": "1"})[0])


class Suites(unittest.TestCase):
    def full(self):
        rec = {k: "0" for k in fin.C_ZERO_RC}
        for fs in fin.C_FILESYSTEMS:
            for step in fin.C_FS_STEPS:
                rec[f"{step}_{fs}_rc"] = "0"
            for s in fin.C_SUITES:
                rec[f"suite_{fs}_{s}_rc"] = "0"; rec[f"suite_{fs}_{s}_new_profraw"] = "2"
        return rec

    def test_ok(self):
        self.assertEqual(fin.check_suites(self.full())[0], [])

    def test_named_gates_present(self):
        """The provenance and lifecycle gates review asked for are in the lists, by name."""
        self.assertIn("json_rc", fin.ZERO_RC)
        for k in ("clean_workspace_rc", "chown_rc", "build_rc", "build_hooks_rc"):
            self.assertIn(k, fin.C_ZERO_RC)
        self.assertEqual(set(fin.C_FS_STEPS), {"mkfs", "mount", "umount", "cleanup"})

    def test_each_gate(self):
        for k in list(self.full()):
            rec = self.full()
            rec[k] = "0" if k.endswith("_new_profraw") else "1"
            self.assertTrue(fin.check_suites(rec)[0], k)
            rec.pop(k)
            self.assertTrue(fin.check_suites(rec)[0], k)


class Summary(unittest.TestCase):
    def test_documented_shape(self):
        self.assertEqual(fin.summary_total(SUMMARY), {"regions": (6, 10), "functions": (1, 2), "lines": (15, 20)})

    def test_counted_column_twice(self):
        """All columns present, plus a second Regions column: must be refused, not read."""
        dup = "\n".join(l + ("     Regions" if l.lstrip().startswith("Filename") else
                              "          99" if l.split()[:1] in (["TOTAL"], ["src/lib.rs"]) else "")
                         for l in SUMMARY.splitlines()) + "\n"
        with self.assertRaises(ValueError):
            fin.summary_total(dup)

    def test_fails_closed(self):
        for text in ("", SUMMARY.replace("TOTAL", "TOTALS"), SUMMARY + "TOTAL 1 2 3\n",
                     SUMMARY.replace("Missed Lines", "Missed_Lines"), SUMMARY.replace("    Cover   Functions", "    Cover   Functionz"),
                     SUMMARY.replace("TOTAL                              10                 4", "TOTAL                              4                 10"),
                     SUMMARY.replace("TOTAL                              10", "TOTAL                              x0"),
                     SUMMARY.replace("Executed       Lines", "Executed     Regions")):  # a counted column twice
            with self.assertRaises(ValueError):
                fin.summary_total(text)


if __name__ == "__main__":
    unittest.main()
