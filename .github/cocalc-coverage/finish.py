#!/usr/bin/env python3
"""Decide whether a coverage run is a CANDIDATE or DIAGNOSTIC (SPEC.md), and write meta.json.

Usage: finish.py OUTDIR REPO_ROOT SPEC_FILE
Exit 0: CANDIDATE.  Exit 1: DIAGNOSTIC (every output is still kept).

A job never labels a run VALID.  A CANDIDATE becomes VALID only through a separately
reviewed promotion of its artifacts (SPEC.md).

Inputs in OUTDIR:
  setup.env   written by the workflow's setup steps (each records its rc, none aborts);
  record.env  written by run.sh / run_c.sh and record_platform.sh;
  the export, lcov, summary and test output named there.
Every check below is a separate function so that each can be tested on its own
(test_finish.py).  Any missing or failed item is a problem; any problem => DIAGNOSTIC.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cov_modules_v4 as cm  # noqa: E402

VERSION_LINE = re.compile(r"cargo-llvm-cov 0\.9\.2")  # used with fullmatch: no 0.9.20, no suffix
RESULT = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;", re.M)
FAILED_TEST = re.compile(r"^test (\S+) \.\.\. FAILED$", re.M)
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
C_FILESYSTEMS = ("btrfs", "btrfs-zstd", "ext4")
C_SUITES = ("roundtrip", "b3_oracle", "crash", "corrupt", "b6_adversary")

# record.env fields that must be present and non-empty for every run
NONEMPTY = ("run", "head_sha", "start_utc", "end_utc", "nproc", "uname", "df_before", "df_after",
            "rustc_vv", "cargo_v", "llvm_cov_version", "cargo_path", "rustc_path", "cargo_llvm_cov_path",
            "llvm-profdata_path", "llvm-cov_path", "export_sha256")
SHA256_FIELDS = ("cargo_lock_sha_before", "cargo_lock_sha_after", "cargo_sha256", "rustc_sha256",
                 "cargo_llvm_cov_sha256", "llvm-profdata_sha256", "llvm-cov_sha256")
# json_rc: the export came from the measured command (0) or from the fallback report, whose rc it is
ZERO_RC = ("cargo_lock_git_diff_rc", "profraw_clean_rc", "lcov_rc", "summary_rc", "json_rc")
# run C only: the empty starting profile set, and the hand-back of root-written profiles
C_ZERO_RC = ("build_rc", "build_hooks_rc", "clean_workspace_rc", "chown_rc")
# run C, per filesystem: the image lifecycle around the suites
C_FS_STEPS = ("mkfs", "mount", "umount", "cleanup")


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                out[k] = v
    return out


def version_ok(line: str) -> bool:
    return VERSION_LINE.fullmatch(line.strip()) is not None


def check_setup(setup: dict, is_pr: bool, run: str) -> list:
    required = ["install_rc", "parser_selftest_rc"]
    if is_pr:
        required.append("changed_paths_rc")
    if run in ("B", "C"):  # the CLI: the rustic_core revision must be recorded from Cargo.lock
        required.append("core_lock_entry_rc")
    if run == "C":
        required.append("btrfs_progs_rc")
    problems = []
    for k in required:
        if setup.get(k) != "0":
            problems.append(f"setup {k}={setup.get(k)!r}")
    return problems


def check_record(rec: dict, intended: str) -> list:
    problems = []
    for k in NONEMPTY:
        if not rec.get(k):
            problems.append(f"record field {k} missing or empty")
    for k in SHA256_FIELDS:
        if not SHA256.fullmatch(rec.get(k, "")):
            problems.append(f"record field {k} is not a sha256: {rec.get(k)!r}")
    for k in ZERO_RC:
        if rec.get(k) != "0":
            problems.append(f"{k}={rec.get(k)!r}")
    if rec.get("cargo_lock_sha_before") != rec.get("cargo_lock_sha_after"):
        problems.append("Cargo.lock changed")
    if not SHA.fullmatch(intended) or rec.get("head_sha") != intended:
        problems.append(f"checked-out HEAD {rec.get('head_sha')!r} is not the intended commit {intended!r}")
    if not version_ok(rec.get("llvm_cov_version", "")):
        problems.append(f"cargo-llvm-cov version line is {rec.get('llvm_cov_version')!r}")
    try:
        t0 = datetime.strptime(rec.get("start_utc", ""), "%Y-%m-%dT%H:%M:%SZ")
        t1 = datetime.strptime(rec.get("end_utc", ""), "%Y-%m-%dT%H:%M:%SZ")
        if t1 < t0:
            problems.append("end before start")
    except ValueError:
        problems.append("start/end time unreadable")
    return problems


def check_tests(text: str, rec: dict) -> tuple[list, dict]:
    counts = [tuple(map(int, m)) for m in RESULT.findall(text)]
    info = {"passed": sum(c[0] for c in counts), "failed": sum(c[1] for c in counts),
            "ignored": sum(c[2] for c in counts), "result_lines": len(counts),
            "failed_names": sorted(set(FAILED_TEST.findall(text)))}
    problems = []
    if rec.get("test_rc") != "0":
        problems.append(f"test command exited {rec.get('test_rc')!r}")
    if not counts:
        problems.append("no 'test result:' lines in the test output")
    if info["failed"] or info["failed_names"]:
        problems.append(f"{info['failed']} failed tests: {info['failed_names'][:20]}")
    return problems, info


def check_suites(rec: dict) -> tuple[list, dict]:
    problems, suites = [], {}
    for k in C_ZERO_RC:
        if rec.get(k) != "0":
            problems.append(f"{k}={rec.get(k)!r}")
    for fs in C_FILESYSTEMS:
        for step in C_FS_STEPS:
            if rec.get(f"{step}_{fs}_rc") != "0":
                problems.append(f"{step} {fs}: {rec.get(f'{step}_{fs}_rc')!r}")
        for suite in C_SUITES:
            name = f"{fs}_{suite}"
            rc, new = rec.get(f"suite_{name}_rc"), rec.get(f"suite_{name}_new_profraw")
            suites[name] = {"rc": rc, "new_profraw": new}
            if rc != "0":
                problems.append(f"suite {name} exited {rc!r}")
            if not (new or "").isdigit() or int(new) < 1:
                problems.append(f"suite {name} left no new non-empty profile ({new!r})")
    return problems, suites


def summary_total(text: str) -> dict:
    """The TOTAL row of `cargo llvm-cov report --summary-only`, as (covered, count) per kind.

    Columns are found by their header names, so a reorder can't be misread.  Written
    against llvm-cov's documented report columns; confirmed on real output only at
    promotion.  Any missing piece raises ValueError.
    """
    lines = text.splitlines()
    headers = [l for l in lines if l.lstrip().startswith("Filename")]
    total = [l for l in lines if l.split()[:1] == ["TOTAL"]]
    if len(headers) != 1 or len(total) != 1:
        raise ValueError("not exactly one header row and one TOTAL row")
    names = re.split(r"\s{2,}", headers[0].strip())[1:]
    cells = total[0].split()[1:]
    if len(names) != len(cells):
        raise ValueError(f"{len(names)} column names but {len(cells)} TOTAL cells")
    for used in ("Regions", "Missed Regions", "Functions", "Missed Functions", "Lines", "Missed Lines"):
        if names.count(used) != 1:  # "Cover" repeats in llvm-cov's header; the counted columns must not
            raise ValueError(f"column {used!r} appears {names.count(used)} times")
    row = dict(zip(names, cells))
    out = {}
    for kind, (n, missed) in {"regions": ("Regions", "Missed Regions"),
                              "functions": ("Functions", "Missed Functions"),
                              "lines": ("Lines", "Missed Lines")}.items():
        if not (row[n].isdigit() and row[missed].isdigit()):
            raise ValueError(f"{n}/{missed} cells are not counts")
        count, miss = int(row[n]), int(row[missed])
        if miss > count:
            raise ValueError(f"{missed} > {n}")
        out[kind] = (count - miss, count)
    return out


def check_export(out: Path, run: str, rec: dict, root: str):
    """R4 (a) (b) (c).  Returns (problems, table or None, totals or None, outside or None)."""
    export = out / f"{run}.json"
    try:
        if hashlib.sha256(export.read_bytes()).hexdigest() != rec.get("export_sha256"):  # (c)
            raise cm.Bad("export sha differs from the one recorded by this run")
        crates, mods, outside, totals = cm.build(json.loads(export.read_text()), cm.canonical(root, "REPO_ROOT"))  # (a)
        summ = summary_total((out / "summary.txt").read_text())  # (b)
        for k in cm.KINDS:
            if summ[k] != totals[k]:
                raise cm.Bad(f"{k}: summary TOTAL {summ[k]} != export totals {totals[k]}")
        return [], cm.render(crates, mods, outside, totals, root), totals, outside
    except (OSError, ValueError, cm.Bad) as e:
        return [f"validation: {type(e).__name__}: {e}"], None, None, None


def main(argv) -> int:
    out, root, spec = Path(argv[1]), argv[2], Path(argv[3])
    rec, setup = read_env(out / "record.env"), read_env(out / "setup.env")
    run = rec.get("run", "?")
    intended = os.environ.get("INTENDED_SHA", "")
    is_pr = bool(os.environ.get("PR_BASE_SHA"))
    meta = {"record": rec, "setup": setup,
            "github": {k: os.environ.get(k) for k in
                       ("GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_EVENT_NAME",
                        "PR_BASE_SHA", "PR_HEAD_SHA", "INTENDED_SHA")}}
    problems = []
    try:
        meta["spec_sha256"] = hashlib.sha256(spec.read_bytes()).hexdigest()
    except OSError as e:
        problems.append(f"spec unreadable: {type(e).__name__}")
    changed = out / "changed-paths.txt"
    meta["changed_paths"] = changed.read_text().split() if changed.exists() else None
    problems += check_setup(setup, is_pr, run)
    problems += check_record(rec, intended)
    if run == "C":
        p, meta["suites"] = check_suites(rec)
    elif run in ("A", "A-hooks", "B"):
        text = (out / "test-output.txt").read_text(errors="replace") if (out / "test-output.txt").exists() else ""
        p, meta["tests"] = check_tests(text, rec)
    else:
        p = [f"unknown run {run!r}"]
    problems += p
    p, table, totals, outside = check_export(out, run, rec, root)
    problems += p
    if totals:
        meta["totals"] = {k: {"covered": totals[k][0], "count": totals[k][1]} for k in cm.KINDS}
        meta["files_outside_root"] = outside
    meta["verdict"] = "CANDIDATE" if not problems else "DIAGNOSTIC"
    meta["problems"] = problems
    (out / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    if meta["verdict"] == "CANDIDATE":
        (out / "modules.md").write_text(table)
    print(f"{run}: {meta['verdict']}")
    for p in problems:
        print(f"  - {p}")
    return 0 if meta["verdict"] == "CANDIDATE" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
