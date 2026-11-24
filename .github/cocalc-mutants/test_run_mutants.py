"""Self-tests of run_mutants.py: ways the harness could report a kill it did not see.

Run with: python3 -I -B -m unittest discover -s .github/cocalc-mutants -p 'test_*.py'
Pure Python: no cargo, no git writes, no root.  Fixtures are verbatim copies of
real files (fixtures/SOURCES.txt).
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_mutants as rm  # noqa: E402

FIX = HERE / "fixtures"
GOOD_PATCH = b"--- a/src/x.rs\n+++ b/src/x.rs\n@@ -1 +1 @@\n-a\n+b\n"


def lock_text(blocks_edit=None) -> str:
    return (FIX / "cli-Cargo.lock-excerpt").read_text()


def lc_from(l0_text: str, names: set) -> str:
    """What the path override does to L0: the overridden packages lose their source line."""
    out, current = [], None
    for line in l0_text.split("\n"):
        if line.startswith("name = "):
            current = line[8:-1]
        if line.startswith("source = ") and current in names and "sagemathinc/rustic_core" in line:
            continue
        out.append(line)
    return "\n".join(out)


class Gate(unittest.TestCase):
    def assertRefused(self, fn, *args, exc=None):
        """fn(*args) must raise exc (default rm.Stop); a crash or no exception is a FAIL."""
        exc = exc or rm.Stop
        try:
            fn(*args)
        except exc:
            return
        except Exception as err:  # noqa: BLE001
            self.fail(f"raised {type(err).__name__}: {err}, not {exc.__name__}")
        self.fail(f"no {exc.__name__}")

    def accepted(self, fn, *args):
        """fn(*args) must succeed; any exception is a FAIL, never an ERROR."""
        try:
            return fn(*args)
        except Exception as err:  # noqa: BLE001
            self.fail(f"raised {type(err).__name__}: {err}")


class Temp(Gate):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def patches(self, **files) -> Path:
        d = self.tmp / "patches"
        d.mkdir(exist_ok=True)
        for name, data in files.items():
            (d / name.replace("_", "-")).write_bytes(data)
        return d


class TimeModel(Gate):
    def test_derived_values(self):
        self.assertEqual(rm.time_model(), {"builds": 13, "tests": 16, "worst": 258, "step": 260, "job": 290})
        self.assertLessEqual(rm.time_model()["job"], 360)

    def test_artifact_max_follows_mutant_bound(self):
        self.assertEqual(rm.N_MUT, 12)
        self.assertEqual(rm.ARTIFACT_MAX, (256 + 64 + 64 + 12 * 64) * 1024)

    def test_every_test_entry_is_within_test_max(self):
        for mode, test in (("cli", "selftest:version"), ("cli", "rt:full_dense"), ("cli", "rt-snap:snapshot_source"), ("core", rm.CORE_TEST)):
            spec = self.accepted(rm.test_spec, mode, test)
            self.assertLessEqual(spec["timeout_min"], rm.TEST_MAX_MIN)
            self.assertTrue(all(0 < rc < 124 for rc in spec["fail"]))

    def test_budget_guard(self):
        now = [0.0]
        budget = rm.Budget(clock=lambda: now[0])
        self.assertTrue(budget.allows(rm.BUILD_MAX_MIN + rm.TEST_MAX_MIN))
        now[0] = (258 - 8) * 60
        self.assertTrue(budget.allows(8))
        self.assertFalse(budget.allows(9))


class Workflow(unittest.TestCase):
    def files(self):
        found = sorted((HERE.parent / "workflows").glob("cocalc-mutants.yml"))
        self.assertEqual(len(found), 1, "the workflow file must exist next to this directory")
        return found[0].read_text()

    def test_the_real_workflow_matches_the_model(self):
        self.assertEqual(rm.check_workflow(self.files()), [])

    def test_off_by_one_timeouts_fail(self):
        text = self.files()
        for bad in ("289", "291"):
            with self.subTest(bad):
                self.assertTrue(rm.check_workflow(text.replace("    timeout-minutes: 290\n", f"    timeout-minutes: {bad}\n")))
        self.assertTrue(rm.check_workflow(text.replace("        timeout-minutes: 260\n", "        timeout-minutes: 261\n")))

    def test_a_step_without_timeout_fails(self):
        text = self.files() + "      - id: extra\n        run: true\n"
        with self.assertRaises(ValueError):
            rm.workflow_timeouts(text)


class Tests(Gate):
    def test_each_kind_names_its_own_fixed_script(self):
        self.assertEqual(self.accepted(rm.test_spec, "cli", "rt:full_dense")["script"], "roundtrip.py")
        snap = self.accepted(rm.test_spec, "cli", "rt-snap:snapshot_source")
        self.assertEqual((snap["script"], snap["scenario"]), ("rt_snapshot.py", "snapshot_source"))
        for mode, bad in (("core", "rt-snap:snapshot_source"), ("cli", "rt-snap:../x"), ("cli", "rt-snap:"),
                          ("cli", "rt-snapshot:x"), ("core", "rt:full_dense"), ("cli", rm.CORE_TEST)):
            with self.subTest(bad):
                self.assertRefused(rm.test_spec, mode, bad)


class WorkDirs(Gate):
    def test_rt_and_rt_snap_never_share_a_work_dir(self):
        names = set()
        for label in (rm.CLEAN_LABEL, "x", "clean", "a1"):
            for test in ("rt:x", "rt-snap:x", "rt:full_dense", "rt-snap:full_dense"):
                names.add(self.accepted(rm.work_dir_name, label, self.accepted(rm.test_spec, "cli", test)))
        self.assertEqual(len(names), 16)

    def test_the_clean_label_cannot_be_a_mutant_id(self):
        self.assertIsNone(rm.ID_RE.fullmatch(rm.CLEAN_LABEL))
        self.assertNotEqual(self.accepted(rm.work_dir_name, rm.CLEAN_LABEL, rm.test_spec("cli", "rt:x")),
                            self.accepted(rm.work_dir_name, "clean", rm.test_spec("cli", "rt:x")))

    def test_bad_labels_are_refused(self):
        for bad in ("../x", "", "A", "x/y"):
            with self.subTest(bad):
                self.assertRefused(rm.work_dir_name, bad, rm.test_spec("cli", "rt:x"))


class Oracle(Gate):
    def test_pieces_match_in_order_within_one_line(self):
        pieces = rm.parse_expect("differs: *'xattrs'")
        self.assertIsNotNone(rm.oracle_line(pieces, "x\ndiffers: 'a' {'xattrs': (1, 2)}\n"))
        self.assertIsNone(rm.oracle_line(pieces, "'xattrs' differs: 'a'"))
        self.assertIsNone(rm.oracle_line(pieces, "differs: 'a'\n'xattrs'"))

    def test_wildcard_only_oracles_are_refused(self):
        for bad in ("*", "**", "*abc*", "a*b*c", ""):
            with self.subTest(bad), self.assertRaises(rm.Stop):
                rm.parse_expect(bad)
        self.assertEqual(rm.parse_expect("*abcd*"), ["abcd"])

    def test_inner_empty_piece_and_non_ascii_are_refused(self):
        for bad in ("abcd**efgh", "abcdé", "abcd\tx", "x" * 201):
            with self.subTest(bad), self.assertRaises(rm.Stop):
                rm.parse_expect(bad)

    def test_long_line_is_cut_before_matching(self):
        pieces = rm.parse_expect("needle")
        self.assertIsNone(rm.oracle_line(pieces, "x" * rm.LINE_CAP + "needle"))
        self.assertEqual(len(rm.oracle_line(pieces, "needle" + "y" * 5000)), rm.MATCH_KEEP)


class Classify(unittest.TestCase):
    spec = {"fail": {1}}
    pieces = ["version mismatch"]

    def res(self, rc, out="version mismatch: got x", timed_out=False, over_cap=False):
        return {"rc": rc, "output": out, "timed_out": timed_out, "over_cap": over_cap}

    def test_killed_needs_fail_rc_and_oracle(self):
        self.assertEqual(rm.classify(self.res(1), self.spec, self.pieces)[0], "KILLED")
        self.assertEqual(rm.classify(self.res(1, "other"), self.spec, self.pieces)[:2], ("INVALID", "no oracle"))

    def test_survived(self):
        self.assertEqual(rm.classify(self.res(0), self.spec, self.pieces)[0], "SURVIVED")

    def test_signals_timeouts_and_odd_rcs_are_invalid(self):
        for result in (self.res(-9), self.res(-15), self.res(137), self.res(139), self.res(124), self.res(2),
                       self.res(1, timed_out=True), self.res(1, over_cap=True)):
            with self.subTest(result):
                self.assertEqual(rm.classify(result, self.spec, self.pieces)[0], "INVALID")

    def test_output_with_nul_and_bad_utf8_is_decoded_not_trusted(self):
        text = rm.decode_output(b"a\0b\xffc")
        self.assertEqual(text, "a\\x00b\\xffc")


class RunCapped(Temp):
    def test_timeout_kills_the_group(self):
        r = rm.run_capped([sys.executable, "-c", "import time; time.sleep(30)"], None, dict(os.environ), 1)
        self.assertTrue(r["timed_out"])
        self.assertEqual(rm.classify(r, {"fail": {1}}, ["abcd"])[0], "INVALID")

    def test_a_child_that_closes_its_output_and_hangs_still_times_out(self):
        code = "import os, time; os.close(1); os.close(2); time.sleep(30)"
        started = rm.time.monotonic()
        r = rm.run_capped([sys.executable, "-c", code], None, dict(os.environ), 2)
        self.assertLess(rm.time.monotonic() - started, 15)
        self.assertTrue(r["timed_out"])
        self.assertEqual(rm.classify(r, {"fail": {1}}, ["abcd"])[:2], ("INVALID", "timeout"))

    def test_signal_is_invalid(self):
        r = rm.run_capped([sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"], None, dict(os.environ), 20)
        self.assertLess(r["rc"], 0)
        self.assertEqual(rm.classify(r, {"fail": {1}}, ["abcd"])[:2], ("INVALID", "signal 9"))

    def test_output_cap(self):
        code = "import sys; sys.stdout.buffer.write(b'x' * 10_000_000)"
        r = rm.run_capped([sys.executable, "-c", code], None, dict(os.environ), 60)
        self.assertTrue(r["over_cap"])
        self.assertLessEqual(len(r["output"]), rm.OUTPUT_CAP)

    def test_stdout_and_stderr_are_merged(self):
        code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(1)"
        r = rm.run_capped([sys.executable, "-c", code], None, dict(os.environ), 20)
        self.assertEqual((r["rc"], sorted(r["output"].split())), (1, ["err", "out"]))


class Manifest(Temp):
    def control_patches(self):
        return self.patches(**{"ctl-killed.patch": GOOD_PATCH, "ctl-noop.patch": GOOD_PATCH, "ctl-no-apply.patch": GOOD_PATCH,
                               "a1.patch": GOOD_PATCH})

    def tsv(self, *rows):
        return "\t".join(rm.HEADER) + "\n" + "".join("\t".join(r) + "\n" for r in rows)

    def test_controls_are_always_added(self):
        rows = rm.load_manifest(self.tsv(), "cli", self.control_patches())
        self.assertEqual([r["id"] for r in rows], ["ctl-killed", "ctl-noop", "ctl-wrong-oracle", "ctl-no-apply"])
        rows = rm.load_manifest(self.tsv(("a1", "core", "a1.patch", "rt:full_dense", "differs: *'xattrs'", "real")), "cli", self.control_patches())
        self.assertEqual(len(rows), 5)

    def test_bad_manifests_stop(self):
        p = self.control_patches()
        good = ("a1", "core", "a1.patch", "rt:full_dense", "differs: *'xattrs'", "real")
        cases = {
            "bad header": "id\ttarget\n",
            "short row": self.tsv(good[:5]),
            "empty field": self.tsv(("a1", "core", "a1.patch", "rt:full_dense", "", "real")),
            "padded field": self.tsv((*good[:4], good[4] + " ", "real")),
            "duplicate id": self.tsv(good, good),
            "control id": self.tsv(("ctl-noop", *good[1:])),
            "control kind": self.tsv((*good[:5], "ctl-kill")),
            "bad id": self.tsv(("A1", *good[1:])),
            "unknown test": self.tsv((good[0], good[1], good[2], "shell:rm -rf /", *good[4:])),
            "bad scenario": self.tsv((good[0], good[1], good[2], "rt:../x", *good[4:])),
            "bad target": self.tsv((good[0], "both", *good[2:])),
            "missing patch": self.tsv((good[0], good[1], "zz.patch", *good[3:])),
            "bad oracle": self.tsv((*good[:4], "*", "real")),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(rm.Stop):
                rm.load_manifest(text, "cli", p)

    def test_core_mode_allows_only_core_targets(self):
        with self.assertRaises(rm.Stop):
            rm.load_manifest(self.tsv(("a1", "cli", "a1.patch", rm.CORE_TEST, "abcd", "real")), "core", self.control_patches())

    def test_row_and_distinct_test_bounds(self):
        p = self.control_patches()
        rows8 = [(f"m{i}", "core", "a1.patch", f"rt:s{i % 3}", "abcd", "real") for i in range(8)]
        self.assertEqual(len(rm.load_manifest(self.tsv(*rows8), "cli", p)), 12)  # 3 rt tests + selftest = 4
        with self.assertRaises(rm.Stop):
            rm.load_manifest(self.tsv(*rows8, ("m8", "core", "a1.patch", "rt:s0", "abcd", "real")), "cli", p)
        with self.assertRaises(rm.Stop):  # a 5th distinct test
            rm.load_manifest(self.tsv(*[(f"m{i}", "core", "a1.patch", f"rt:s{i}", "abcd", "real") for i in range(4)]), "cli", p)

    def test_patch_file_gates(self):
        p = self.patches(**{"ok.patch": GOOD_PATCH, "nul.patch": b"a\0b", "latin.patch": b"\xff\n", "big.patch": b"x" * (rm.PATCH_MAX + 1)})
        os.symlink(p / "ok.patch", p / "link.patch")
        (p / "dir.patch").mkdir()
        (self.tmp / "outside.patch").write_bytes(GOOD_PATCH)
        self.assertEqual(rm.check_patch_file(p, "ok.patch"), GOOD_PATCH)
        for name in ("nul.patch", "latin.patch", "big.patch", "link.patch", "dir.patch", "../outside.patch", "/etc/passwd",
                     "missing.patch", "Up.patch"):
            with self.subTest(name):
                self.assertRefused(rm.check_patch_file, p, name)


class PatchGates(Temp):
    def tree(self):
        t = self.tmp / "tree"
        (t / "src").mkdir(parents=True)
        (t / "src/x.rs").write_text("a\n")
        (t / ".github").mkdir()
        (t / ".github/w.yml").write_text("x")
        (t / "crates/x").mkdir(parents=True)
        (t / "crates/x/Cargo.toml").write_text("x")
        os.symlink("x.rs", t / "src/link.rs")
        (self.tmp / "outside").mkdir()
        (self.tmp / "outside" / "f.rs").write_text("x")
        os.symlink(self.tmp / "outside", t / "lnk")
        return t

    def test_header_screen(self):
        rm.screen_patch_text(GOOD_PATCH)
        for header in ("new file mode 100644", "deleted file mode 100644", "old mode 100644", "new mode 100755",
                       "rename from a", "rename to b", "copy from a", "similarity index 90%", "GIT binary patch",
                       "Binary files a and b differ"):
            with self.subTest(header), self.assertRaises(rm.Invalid):
                rm.screen_patch_text(b"diff --git a/x b/x\n" + header.encode() + b"\n")

    def test_numstat_forms(self):
        self.assertEqual(rm.parse_numstat_z(b"1\t1\tsrc/x.rs\0"), [("ordinary", [b"src/x.rs"])])
        self.assertEqual(rm.parse_numstat_z(b"0\t0\t\0a/../x\0b/y\0"), [("rename", [b"a/../x", b"b/y"])])
        self.assertEqual(rm.parse_numstat_z(b"-\t-\tbin.dat\0"), [("binary", [b"bin.dat"])])
        self.assertEqual(rm.parse_numstat_z(b"-\t-\t\0a\0b\0"), [("binary-rename", [b"a", b"b"])])

    def test_malformed_numstat_stops(self):
        for bad in (b"", b"1\t1\tsrc/x.rs", b"x\t1\tsrc/x.rs\0", b"1\t1\t\0a\0", b"1\tsrc\0", b"1\t1\t\0\0b\0"):
            with self.subTest(bad), self.assertRaises(rm.Stop):
                rm.parse_numstat_z(bad)

    def test_rename_traversal_and_binary_are_refused(self):
        t = self.tree()
        for records in ([("rename", [b"src/../x", b"src/y.rs"])], [("rename", [b"src/x.rs", b".github/w.yml"])],
                        [("binary", [b"src/x.rs"])], [("rename", [b"src/x.rs", b"src/x.rs"])]):
            with self.subTest(records), self.assertRaises(rm.Invalid):
                rm.check_numstat(t, records)
        rm.check_numstat(t, [("ordinary", [b"src/x.rs"])])

    def test_path_gate(self):
        t = self.tree()
        for bad in (b"/etc/passwd", b"../x", b"src//x.rs", b"./src/x.rs", b".github/w.yml", b"crates/x/Cargo.toml",
                    b"Cargo.lock", b"rust-toolchain.toml", b"src/missing.rs", b"src/link.rs", b"src", b"src\\x.rs", b"\xff",
                    b"lnk/f.rs"):
            with self.subTest(bad):
                self.assertRefused(rm.gate_patch_path, t, bad, exc=rm.Invalid)
        rm.gate_patch_path(t, b"src/x.rs")


class Locks(Temp):
    def l0(self):
        return tomllib.loads(lock_text())

    def test_override_set_is_derived_from_the_real_lock(self):
        source, rev, pkgs = rm.override_set(self.l0())
        self.assertEqual(rev, "51115c4755fea8d0a5254e409d99e47a293d68c5")
        self.assertEqual(pkgs, [("rustic_backend", "0.7.0"), ("rustic_core", "0.13.0"), ("rustic_extents", "0.1.0"), ("rustic_testing", "0.5.0")])

    def test_the_override_transformation_is_allowed(self):
        source, _, pkgs = rm.override_set(self.l0())
        lc = tomllib.loads(lc_from(lock_text(), {n for n, _ in pkgs}))
        t = rm.compare_locks_cli(self.l0(), lc, source, pkgs)
        self.assertEqual([c["name"] for c in t["changes"]], [n for n, _ in pkgs])
        self.assertTrue(all(c["removed"] == ["source"] for c in t["changes"]))

    def test_other_lock_changes_stop(self):
        source, _, pkgs = rm.override_set(self.l0())
        names = {n for n, _ in pkgs}
        base = lc_from(lock_text(), names)
        cases = {
            "source changed, not removed": lc_from(lock_text(), names - {"rustic_core"}).replace("rev=51115c4755fea8d0a5254e409d99e47a293d68c5#", "rev=0000000000000000000000000000000000000000#", 1),
            "a neighbour changed": base.replace('checksum = "', 'checksum = "00', 1),
            "version changed": base.replace('version = "0.13.0"', 'version = "0.13.1"', 1),
            "dependency changed": base.replace('dependencies = [\n', 'dependencies = [\n "zzz",\n', 1),
            "extra package": base + '\n[[package]]\nname = "zzz"\nversion = "1.0.0"\n',
            "lock version": base.replace("version = 4", "version = 3", 1),
            "unused patch table": base + '\n[[patch.unused]]\nname = "x"\nversion = "1.0.0"\n',
            "overridden package lost a field": self.drop_deps(base, "rustic_core"),
            "overridden package changed a dependency": self.change_dep(base, "rustic_core"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertNotEqual(text, base)
                self.assertRefused(rm.compare_locks_cli, self.l0(), tomllib.loads(text), source, pkgs)

    @staticmethod
    def block(text, name):
        start = text.index(f'\n[[package]]\nname = "{name}"\n')
        end = text.find("\n[[package]]\n", start + 1)
        return start, (len(text) if end < 0 else end)

    def drop_deps(self, text, name):
        start, end = self.block(text, name)
        blk = text[start:end]
        cut = blk.index("dependencies = [")
        return text[:start] + blk[:cut] + text[end:]

    def change_dep(self, text, name):
        start, end = self.block(text, name)
        blk = text[start:end].replace("dependencies = [\n", 'dependencies = [\n "zzz-not-a-dep",\n', 1)
        return text[:start] + blk + text[end:]

    def test_an_override_that_did_not_take_effect_stops(self):
        source, _, pkgs = rm.override_set(self.l0())
        names = {n for n, _ in pkgs}
        for label, text in (("Lc == L0", lock_text()), ("rustic_extents unchanged", lc_from(lock_text(), names - {"rustic_extents"}))):
            with self.subTest(label):
                self.assertRefused(rm.compare_locks_cli, self.l0(), tomllib.loads(text), source, pkgs)

    def test_source_kept_while_checksum_removed_stops(self):
        # only the "source must be gone" check can refuse this one (synthetic: git
        # packages have no checksum in the real lock)
        source, _, pkgs = rm.override_set(self.l0())
        l0 = self.l0()
        lc = tomllib.loads(lc_from(lock_text(), {n for n, _ in pkgs} - {"rustic_core"}))
        for lock in (l0, lc):
            for p in lock["package"]:
                if p["name"] == "rustic_core":
                    p["checksum"] = "0" * 64
        next(p for p in lc["package"] if p["name"] == "rustic_core").pop("checksum")
        self.assertRefused(rm.compare_locks_cli, l0, lc, source, pkgs)

    def test_override_key_with_another_source_stops(self):
        # a package that is not from the pinned rev may not be treated as overridden,
        # even if its source were removed
        source, _, pkgs = rm.override_set(self.l0())
        l0 = self.l0()
        other = next(p for p in l0["package"] if p.get("source") and p.get("source") != source)
        lc = tomllib.loads(lc_from(lock_text(), {n for n, _ in pkgs}))
        next(p for p in lc["package"] if p["name"] == other["name"] and p["version"] == other["version"]).pop("source")
        keys = sorted([*pkgs, (other["name"], other["version"])], key=[(p["name"], p["version"]) for p in l0["package"]].index)
        self.assertRefused(rm.compare_locks_cli, l0, lc, source, keys)

    def test_top_level_value_change_stops(self):
        # the real lock has no other top-level table; a changed one must still be seen
        source, _, pkgs = rm.override_set(self.l0())
        l0 = self.l0()
        lc = tomllib.loads(lc_from(lock_text(), {n for n, _ in pkgs}))
        l0["metadata"], lc["metadata"] = {"a": "1"}, {"a": "2"}
        self.assertRefused(rm.compare_locks_cli, l0, lc, source, pkgs)
        lc["metadata"] = {"a": "1"}
        rm.compare_locks_cli(l0, lc, source, pkgs)

    def test_a_fourth_overridden_package_cannot_be_dropped_from_the_set(self):
        source, _, pkgs = rm.override_set(self.l0())
        lc = tomllib.loads(lc_from(lock_text(), {n for n, _ in pkgs}))
        with self.assertRaises(rm.Stop):  # the fixed three-name list of design v2.3 would STOP here
            rm.compare_locks_cli(self.l0(), lc, source, [p for p in pkgs if p[0] != "rustic_extents"])

    def test_ambiguous_or_unpinned_sources_stop(self):
        text = lock_text()
        two = text.replace("rev=51115c4755fea8d0a5254e409d99e47a293d68c5#51115c4755fea8d0a5254e409d99e47a293d68c5",
                           "rev=1111111111111111111111111111111111111111#1111111111111111111111111111111111111111", 1)
        unpinned = text.replace("?rev=51115c4755fea8d0a5254e409d99e47a293d68c5#", "?branch=main#")
        mismatch = text.replace("#51115c4755fea8d0a5254e409d99e47a293d68c5", "#2222222222222222222222222222222222222222")
        for name, t in (("two revs", two), ("branch", unpinned), ("rev != commit", mismatch)):
            with self.subTest(name):
                self.assertNotEqual(t, text)
                self.assertRefused(rm.override_set, tomllib.loads(t))

    def test_malformed_lock_stops(self):
        for bad in ({"version": 4}, {"package": []}, {"version": 4, "package": [{"name": "x"}]},
                    {"version": 4, "package": [{"name": "x", "version": "1"}, {"name": "x", "version": "1"}]}):
            with self.subTest(bad):
                self.assertRefused(rm.lock_packages, bad)


class Members(Temp):
    def core_tree(self, skip=(), extra=None):
        core = self.tmp / "core"
        (core / "examples" / "demo").mkdir(parents=True)
        (core / "Cargo.toml").write_bytes((FIX / "core-Cargo.toml").read_bytes())
        for c in ("core", "backend", "extents", "testing", "config"):
            if c in skip:
                continue
            (core / "crates" / c).mkdir(parents=True)
            (core / "crates" / c / "Cargo.toml").write_bytes((FIX / f"core-crates-{c}-Cargo.toml").read_bytes())
        if extra:
            (core / "examples" / "demo" / "Cargo.toml").write_text(extra)
        return core

    def pkgs(self):
        return rm.override_set(tomllib.loads(lock_text()))[2]

    def test_members_match_name_and_version(self):
        found = rm.member_paths(self.core_tree(), self.pkgs())
        self.assertEqual({k: v.name for k, v in found.items()},
                         {("rustic_backend", "0.7.0"): "backend", ("rustic_core", "0.13.0"): "core",
                          ("rustic_extents", "0.1.0"): "extents", ("rustic_testing", "0.5.0"): "testing"})
        section = rm.patch_section(self.pkgs(), found)
        self.assertEqual(sorted(tomllib.loads(section)["patch"]["https://github.com/sagemathinc/rustic_core.git"]),
                         ["rustic_backend", "rustic_core", "rustic_extents", "rustic_testing"])

    def test_missing_duplicate_or_version_mismatch_stops(self):
        self.assertRefused(rm.member_paths, self.core_tree(skip=("extents",)), self.pkgs())
        self._tmp.cleanup()
        self.setUp()
        self.assertRefused(rm.member_paths, self.core_tree(extra='[package]\nname = "rustic_core"\nversion = "0.13.0"\n'), self.pkgs())
        self._tmp.cleanup()
        self.setUp()
        core = self.core_tree()
        text = (core / "crates/extents/Cargo.toml").read_text().replace('version = "0.1.0"', 'version = "0.1.1"', 1)
        (core / "crates/extents/Cargo.toml").write_text(text)
        self.assertRefused(rm.member_paths, core, self.pkgs())

    def test_symlinked_member_manifest_stops(self):
        core = self.core_tree()
        os.unlink(core / "crates/extents/Cargo.toml")
        os.symlink(FIX / "core-crates-extents-Cargo.toml", core / "crates/extents/Cargo.toml")
        self.assertRefused(rm.member_paths, core, self.pkgs())

    def test_member_directory_outside_the_tree_stops(self):
        # a regular manifest reached through a symlinked member directory: only the
        # realpath containment check can refuse it
        core = self.core_tree()
        outside = self.tmp / "outside-extents"
        outside.mkdir()
        os.rename(core / "crates/extents/Cargo.toml", outside / "Cargo.toml")
        os.rmdir(core / "crates/extents")
        os.symlink(outside, core / "crates/extents")
        self.assertRefused(rm.member_paths, core, self.pkgs())


class Modes(Temp):
    def repo(self, files):
        r = self.tmp / "repo"
        for rel, fix in files.items():
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_bytes((FIX / fix).read_bytes())
        return r

    def test_each_mode_accepts_its_own_tree_and_refuses_the_other(self):
        cli = self.repo({"Cargo.toml": "cli-Cargo.toml"})
        rm.check_mode(cli, "cli")
        self.assertRefused(rm.check_mode, cli, "core")
        os.rename(cli, self.tmp / "cli")
        core = self.repo({"Cargo.toml": "core-Cargo.toml", "crates/core/Cargo.toml": "core-crates-core-Cargo.toml"})
        rm.check_mode(core, "core")
        self.assertRefused(rm.check_mode, core, "cli")

    def test_core_mode_never_uses_the_cli_allowance(self):
        # In core mode the rule is bytes: the CLI's allowed transformation is a STOP there.
        l0 = lock_text().encode()
        rm.check_core_lock(l0, l0)
        names = {n for n, _ in rm.override_set(tomllib.loads(lock_text()))[2]}
        with self.assertRaises(rm.Stop):
            rm.check_core_lock(l0, lc_from(lock_text(), names).encode())
        with self.assertRaises(rm.Stop):
            rm.check_core_lock(l0, l0 + b"\n")


class Artifact(Temp):
    def staged(self, ids=("a1", "ctl-noop")):
        out = self.tmp / "out"
        (out / "logs").mkdir(parents=True)
        (out / "results.json").write_text(json.dumps({"mutants": [{"id": i} for i in ids]}))
        (out / "results.md").write_text("x")
        (out / "lock-transform.json").write_text("{}")
        for i in ids:
            (out / "logs" / f"{i}.txt").write_text("log")
        return out

    def test_exact_tree_passes(self):
        self.assertEqual(rm.gate_artifact(self.staged()), [])

    def test_violations_fail(self):
        def extra(o): (o / "extra.txt").write_text("x")
        def missing(o): os.unlink(o / "logs" / "a1.txt")
        def symlink(o): os.unlink(o / "logs" / "a1.txt"); os.symlink("/etc/passwd", o / "logs" / "a1.txt")
        def fifo(o): os.unlink(o / "logs" / "a1.txt"); os.mkfifo(o / "logs" / "a1.txt")
        def big(o): (o / "results.md").write_bytes(b"x" * (rm.FILE_CAPS["results.md"] + 1))
        def biglog(o): (o / "logs" / "a1.txt").write_bytes(b"x" * (rm.LOG_CAP + 1))
        def badjson(o): (o / "results.json").write_text("{")
        for change in (extra, missing, symlink, fifo, big, biglog, badjson):
            with self.subTest(change.__name__):
                self._tmp.cleanup()
                self.setUp()
                out = self.staged()
                change(out)
                self.assertTrue(rm.gate_artifact(out))

    def test_total_over_cap_fails(self):
        ids = [f"m{i}" for i in range(rm.N_MUT + 8)]
        out = self.staged(ids)
        for i in ids:
            (out / "logs" / f"{i}.txt").write_bytes(b"x" * rm.LOG_CAP)
        self.assertTrue(any("total" in p for p in rm.gate_artifact(out)))

    def test_write_capped_refuses(self):
        with self.assertRaises(rm.Stop):
            rm.write_capped(self.tmp / "results.md", b"x" * (rm.FILE_CAPS["results.md"] + 1), rm.FILE_CAPS["results.md"])


class Rustc(Gate):
    # the real `rustc -vV` of the CLI pin on ubuntu-24.04, from the coverage-B record
    # of the 20261123 baseline (record.env stores it with ';' for newlines)
    REAL = ("rustc 1.94.0 (4a4ef493e 2026-03-02);binary: rustc;commit-hash: 4a4ef493e3a1488c6e321570238084b38948f6db;"
            "commit-date: 2026-03-02;host: x86_64-unknown-linux-gnu;release: 1.94.0;LLVM version: 21").replace(";", "\n") + "\n"

    def test_exact_release_only(self):
        self.assertTrue(rm.rustc_ok(self.REAL, "1.94.0"))
        for bad_expected in ("1.94", "1.94.0-beta", "1.95.0"):
            self.assertFalse(rm.rustc_ok(self.REAL, bad_expected))
        self.assertFalse(rm.rustc_ok(self.REAL + "release: 1.95.0\n", "1.94.0"))
        self.assertFalse(rm.rustc_ok(self.REAL.replace("release: 1.94.0", "release: 1.94.0-nightly"), "1.94.0"))


class SelftestVersion(Temp):
    def fake(self, text, rc=0):
        p = self.tmp / "rustic"
        p.write_text(f"#!/bin/sh\necho '{text}'\nexit {rc}\n")
        p.chmod(0o755)
        return str(p)

    def test_match_and_mismatch(self):
        self.assertEqual(rm.selftest_version(self.fake("rustic 0.11.4"), "0.11.4"), 0)
        self.assertEqual(rm.selftest_version(self.fake("rustic 0.0.0-cocalc-mutant-control"), "0.11.4"), 1)
        self.assertEqual(rm.selftest_version(self.fake("rustic 0.11.4", rc=3), "0.11.4"), 1)


if __name__ == "__main__":
    unittest.main()
