#!/usr/bin/env python3
"""Mutant proofs for the CoCalc forks of rustic and rustic_core.

A mutant counts as KILLED only if the clean build passes the named test and the
mutant build fails it with the expected oracle message.  A patch that does not
apply, a build failure, a timeout, a signal, an unexpected exit code or a
failure without the oracle message is INVALID, never a kill.

Design: PR-0b design v2.3 (sha256 81a792b4...), reviewed, plus deviation 1 (the
path-override set is derived from Cargo.lock).  See SPEC.md next to this file.
This file is byte-identical in sagemathinc/rustic and sagemathinc/rustic_core.

Usage:
  run_mutants.py run --mode cli|core --repo DIR --out DIR --work-root DIR
                     --intended-sha SHA --expect-rustc X.Y.Z
  run_mutants.py gate-artifact DIR
  run_mutants.py selftest-version BINARY VERSION
Exit codes: 0 PASS, 1 FAIL, 2 STOP (no verdicts).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib

# ---- bounds (design v2.3, V2-R2): every derived value is computed here ----
MAX_REAL_MUTANTS = 8
N_CONTROLS = 4
N_MUT = MAX_REAL_MUTANTS + N_CONTROLS
MAX_DISTINCT_TESTS = 4
BUILD_MAX_MIN = 10
TEST_MAX_MIN = 8
SETUP_MAX_MIN = 20
TEARDOWN_MAX_MIN = 10
MIN_FREE_BYTES = 8 * 1024**3

KIB = 1024
OUTPUT_CAP = 4 * 1024 * KIB
LINE_CAP = 4096
MATCH_KEEP = 512
LOG_CAP = 64 * KIB
FILE_CAPS = {"results.json": 256 * KIB, "results.md": 64 * KIB, "lock-transform.json": 64 * KIB}
ARTIFACT_MAX = sum(FILE_CAPS.values()) + N_MUT * LOG_CAP
PATCH_MAX = 64 * KIB


def time_model() -> dict:
    builds = 1 + N_MUT
    tests = MAX_DISTINCT_TESTS + N_MUT
    worst = builds * BUILD_MAX_MIN + tests * TEST_MAX_MIN
    step = worst + 2
    return {
        "builds": builds,
        "tests": tests,
        "worst": worst,
        "step": step,
        "job": SETUP_MAX_MIN + step + TEARDOWN_MAX_MIN,
    }


class Stop(Exception):
    """The run cannot give verdicts (exit 2)."""


class Invalid(Exception):
    """This mutant's result is INVALID, with the reason."""


# ---- the mutant manifest ----
HEADER = ["id", "target", "patch", "test", "expect", "kind"]
ID_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
PATCH_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\.patch")
SCENARIO_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
CARGO_TEST_RE = re.compile(r"[a-z_][a-z0-9_]*(::[a-z_][a-z0-9_]*){0,7}")
EXPECTED_VERDICT = {"real": "KILLED", "ctl-kill": "KILLED", "ctl-survive": "SURVIVED", "ctl-invalid": "INVALID"}
TARGETS = {"cli": {"cli", "core"}, "core": {"core"}}

CORE_TEST = "cargo-test:crypto::hasher::tests::hash_reader_is_identical_to_hash"
CONTROLS = {
    "cli": [
        ("ctl-killed", "cli", "ctl-killed.patch", "selftest:version", "version mismatch*0.0.0-cocalc-mutant-control", "ctl-kill"),
        ("ctl-noop", "cli", "ctl-noop.patch", "selftest:version", "version mismatch", "ctl-survive"),
        ("ctl-wrong-oracle", "cli", "ctl-killed.patch", "selftest:version", "this text is never printed", "ctl-invalid"),
        ("ctl-no-apply", "cli", "ctl-no-apply.patch", "selftest:version", "version mismatch", "ctl-invalid"),
    ],
    "core": [
        ("ctl-killed", "core", "ctl-killed.patch", CORE_TEST, "hash_reader_is_identical_to_hash ... FAILED", "ctl-kill"),
        ("ctl-noop", "core", "ctl-noop.patch", CORE_TEST, "hash_reader_is_identical_to_hash ... FAILED", "ctl-survive"),
        ("ctl-wrong-oracle", "core", "ctl-killed.patch", CORE_TEST, "this text is never printed", "ctl-invalid"),
        ("ctl-no-apply", "core", "ctl-no-apply.patch", CORE_TEST, "hash_reader_is_identical_to_hash ... FAILED", "ctl-invalid"),
    ],
}
CONTROL_IDS = {"ctl-killed", "ctl-noop", "ctl-wrong-oracle", "ctl-no-apply"}


def parse_expect(expect: str) -> list[str]:
    """Literal pieces separated by '*'; at least one piece of 4+ characters."""
    if not 1 <= len(expect) <= 200 or not all(" " <= c <= "~" for c in expect):
        raise Stop(f"expect must be 1-200 printable ASCII characters: {expect!r}")
    pieces = expect.split("*")
    if any(p == "" for p in pieces[1:-1]):
        raise Stop(f"expect has an empty inner piece: {expect!r}")
    if not any(len(p) >= 4 for p in pieces):
        raise Stop(f"expect needs a literal piece of at least 4 characters: {expect!r}")
    return [p for p in pieces if p]


def oracle_line(pieces: list[str], text: str) -> str | None:
    """The first line containing every piece in order (linear time), cut to MATCH_KEEP."""
    for line in text.split("\n"):
        line = line[:LINE_CAP]
        pos = 0
        for piece in pieces:
            found = line.find(piece, pos)
            if found < 0:
                break
            pos = found + len(piece)
        else:
            return line[:MATCH_KEEP]
    return None


def test_spec(mode: str, test: str) -> dict:
    """The fixed TESTS table: a named test -> its kind, fail exit codes and timeout."""
    if mode == "cli" and test == "selftest:version":
        spec = {"kind": "selftest-version", "fail": {1}, "timeout_min": 2, "clean_must": "version ok"}
    elif mode == "cli" and test.startswith("rt:") and SCENARIO_RE.fullmatch(test[3:]):
        spec = {"kind": "roundtrip", "script": "roundtrip.py", "scenario": test[3:], "fail": {1}, "timeout_min": 8, "clean_must": None}
    elif mode == "cli" and test.startswith("rt-snap:") and SCENARIO_RE.fullmatch(test[8:]):
        spec = {"kind": "roundtrip", "script": "rt_snapshot.py", "scenario": test[8:], "fail": {1}, "timeout_min": 8, "clean_must": None}
    elif mode == "core" and test.startswith("cargo-test:") and CARGO_TEST_RE.fullmatch(test[11:]):
        spec = {
            "kind": "cargo-test",
            "name": test[11:],
            "fail": {101},
            "timeout_min": 8,
            "clean_must": "test result: ok. 1 passed",
        }
    else:
        raise Stop(f"unknown test for mode {mode}: {test!r}")
    assert spec["timeout_min"] <= TEST_MAX_MIN, test
    assert all(0 < rc < 124 for rc in spec["fail"]), test
    return spec


CLEAN_LABEL = "_clean"  # ID_RE cannot produce it, so no mutant id collides with the clean run


def work_dir_name(label: str, spec: dict) -> str:
    """A fresh work dir name per (run label, test): distinct for rt:x and rt-snap:x."""
    if label != CLEAN_LABEL and not ID_RE.fullmatch(label):
        raise Stop(f"bad run label {label!r}")
    script = {"roundtrip.py": "rt", "rt_snapshot.py": "rt-snap"}[spec["script"]]
    if not SCENARIO_RE.fullmatch(spec["scenario"]):
        raise Stop(f"bad scenario {spec['scenario']!r}")
    return f"{label}--{script}--{spec['scenario']}"


def check_patch_file(patches: Path, name: str) -> bytes:
    """A patch: a basename, a regular non-symlink file inside patches/, <= 64 KiB, UTF-8, no NUL."""
    if not PATCH_NAME_RE.fullmatch(name):
        raise Stop(f"bad patch name {name!r}")
    path = patches / name
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise Stop(f"missing patch {name}") from None
    if not stat.S_ISREG(info.st_mode):
        raise Stop(f"patch {name} is not a regular file")
    if os.path.realpath(path) != os.path.join(os.path.realpath(patches), name):
        raise Stop(f"patch {name} is not inside {patches}")
    if info.st_size > PATCH_MAX:
        raise Stop(f"patch {name} is over {PATCH_MAX} bytes")
    data = path.read_bytes()
    if b"\0" in data:
        raise Stop(f"patch {name} contains NUL")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Stop(f"patch {name} is not UTF-8") from None
    return data


def load_manifest(text: str, mode: str, patches: Path) -> list[dict]:
    """MUTANTS.tsv plus the 4 forced controls; any malformed row STOPs the run."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0].split("\t") != HEADER:
        raise Stop(f"MUTANTS.tsv header must be {HEADER}")
    rows = []
    for n, line in enumerate(lines[1:], start=2):
        fields = line.split("\t")
        if len(fields) != len(HEADER) or any(f == "" or f != f.strip() for f in fields):
            raise Stop(f"MUTANTS.tsv line {n}: need {len(HEADER)} non-empty, unpadded fields")
        row = dict(zip(HEADER, fields))
        if row["kind"] != "real":
            raise Stop(f"MUTANTS.tsv line {n}: only kind 'real' may be listed (controls are fixed)")
        if row["id"] in CONTROL_IDS:
            raise Stop(f"MUTANTS.tsv line {n}: {row['id']} is a control id")
        rows.append(row)
    if len(rows) > MAX_REAL_MUTANTS:
        raise Stop(f"{len(rows)} real mutants; at most {MAX_REAL_MUTANTS}")
    rows += [dict(zip(HEADER, c)) for c in CONTROLS[mode]]
    seen = set()
    for row in rows:
        if not ID_RE.fullmatch(row["id"]) or row["id"] in seen:
            raise Stop(f"bad or duplicate id {row['id']!r}")
        seen.add(row["id"])
        if row["target"] not in TARGETS[mode]:
            raise Stop(f"{row['id']}: target {row['target']!r} not allowed in mode {mode}")
        row["spec"] = test_spec(mode, row["test"])
        row["pieces"] = parse_expect(row["expect"])
        row["patch_bytes"] = check_patch_file(patches, row["patch"])
    if len({row["test"] for row in rows}) > MAX_DISTINCT_TESTS:
        raise Stop(f"more than {MAX_DISTINCT_TESTS} distinct tests")
    return rows


# ---- patches: header screen, numstat parse, path gate ----
FORBIDDEN_HEADERS = (
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "similarity index",
    "GIT binary patch",
    "Binary files",
)
PROTECTED_NAMES = {"Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml"}
COUNT_RE = re.compile(rb"[0-9]+")


def screen_patch_text(data: bytes) -> None:
    for line in data.decode("utf-8").split("\n"):
        if line.startswith(FORBIDDEN_HEADERS):
            raise Invalid(f"patch header not allowed: {line[:80]!r}")


def parse_numstat_z(data: bytes) -> list[tuple[str, list[bytes]]]:
    """git apply --numstat -z records: ('ordinary'|'rename'|'binary'|'binary-rename', paths)."""
    out = []
    pos = 0
    while pos < len(data):
        tab1 = data.find(b"\t", pos)
        tab2 = data.find(b"\t", tab1 + 1) if tab1 >= 0 else -1
        if tab1 < 0 or tab2 < 0:
            raise Stop("numstat: malformed record")
        added, deleted = data[pos:tab1], data[tab1 + 1 : tab2]
        binary = added == b"-" and deleted == b"-"
        if not binary and not (COUNT_RE.fullmatch(added) and COUNT_RE.fullmatch(deleted)):
            raise Stop("numstat: counts must be decimal or '-'")
        rest = tab2 + 1
        if data[rest : rest + 1] == b"\0":  # rename/copy: \0old\0new\0
            old_end = data.find(b"\0", rest + 1)
            new_end = data.find(b"\0", old_end + 1) if old_end >= 0 else -1
            if old_end < 0 or new_end < 0:
                raise Stop("numstat: malformed rename record")
            paths = [data[rest + 1 : old_end], data[old_end + 1 : new_end]]
            kind = "binary-rename" if binary else "rename"
            pos = new_end + 1
        else:
            end = data.find(b"\0", rest)
            if end < 0:
                raise Stop("numstat: record without NUL")
            paths = [data[rest:end]]
            kind = "binary" if binary else "ordinary"
            pos = end + 1
        if any(p == b"" for p in paths):
            raise Stop("numstat: empty path")
        out.append((kind, paths))
    if not out:
        raise Stop("numstat: no records")
    return out


def gate_patch_path(tree: Path, raw: bytes) -> None:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Invalid(f"patch path is not UTF-8: {raw!r}") from None
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or "\0" in path or any(p in ("", ".", "..") for p in parts):
        raise Invalid(f"patch path not allowed: {path!r}")
    if parts[0] == ".github" or parts[-1] in PROTECTED_NAMES:
        raise Invalid(f"patch path protected: {path!r}")
    full = tree / path
    try:
        info = os.lstat(full)
    except FileNotFoundError:
        raise Invalid(f"patch path does not exist: {path!r}") from None
    if not stat.S_ISREG(info.st_mode):
        raise Invalid(f"patch path is not a regular file: {path!r}")
    if not os.path.realpath(full).startswith(os.path.realpath(tree) + os.sep):
        raise Invalid(f"patch path escapes the tree: {path!r}")


def check_numstat(tree: Path, records) -> None:
    for kind, paths in records:
        for raw in paths:  # every old and new path, even for refused kinds
            gate_patch_path(tree, raw)
        if kind != "ordinary":
            raise Invalid(f"{kind} patches are not allowed")


# ---- Cargo.lock and the two modes ----
def read_toml(path: Path) -> dict:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise Stop(f"{path} is not a regular file")
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        raise Stop(f"{path}: {err}") from None


def lock_packages(lock: dict) -> list[dict]:
    pkgs = lock.get("package")
    if not isinstance(lock.get("version"), int) or not isinstance(pkgs, list):
        raise Stop("Cargo.lock: missing version or package list")
    keys = set()
    for p in pkgs:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not isinstance(p.get("version"), str):
            raise Stop("Cargo.lock: malformed package entry")
        key = (p["name"], p["version"], p.get("source"))
        if key in keys:
            raise Stop(f"Cargo.lock: duplicate package {key}")
        keys.add(key)
    return pkgs


CORE_GIT = "git+https://github.com/sagemathinc/rustic_core.git"
CORE_SOURCE_RE = re.compile(re.escape(CORE_GIT) + r"\?rev=([0-9a-f]{40})#([0-9a-f]{40})")


def override_set(l0: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """(source, rev, [(name, version)]) of every L0 package from the pinned rustic_core git rev."""
    sources = {p.get("source") for p in lock_packages(l0) if str(p.get("source", "")).startswith(CORE_GIT)}
    if len(sources) != 1:
        raise Stop(f"Cargo.lock: need exactly one rustic_core git source, found {sorted(map(str, sources))}")
    source = sources.pop()
    m = CORE_SOURCE_RE.fullmatch(source)
    if not m or m.group(1) != m.group(2):
        raise Stop(f"Cargo.lock: rustic_core source is not a pinned rev: {source!r}")
    pkgs = [(p["name"], p["version"]) for p in l0["package"] if p.get("source") == source]
    return source, m.group(1), pkgs


def compare_locks_cli(l0: dict, lc: dict, source: str, overridden: list[tuple[str, str]]) -> dict:
    """The structured L0 -> Lc rule; returns the observed transformation.

    Every package of the derived override set must have lost its source (so the
    override took effect for each one) and changed nothing else but an optional
    checksum; every other package and every other top-level value is unchanged.
    """
    for k in sorted(set(l0) | set(lc)):
        if k != "package" and (k not in l0 or k not in lc or l0[k] != lc[k]):
            raise Stop(f"Cargo.lock: top-level {k!r} changed")
    a, b = lock_packages(l0), lock_packages(lc)
    if [(p["name"], p["version"]) for p in a] != [(p["name"], p["version"]) for p in b]:
        raise Stop("Cargo.lock: the package list changed")
    changes, changed_keys = [], []
    for pa, pb in zip(a, b):
        key = (pa["name"], pa["version"])
        if key not in overridden:
            if pa != pb:
                raise Stop(f"Cargo.lock: {key} changed but is not overridden")
            continue
        if pa.get("source") != source:
            raise Stop(f"Cargo.lock: {key} is in the override set but L0 has another source")
        if "source" in pb:
            raise Stop(f"Cargo.lock: {key} kept a source: the override did not take effect")
        removed = sorted(set(pa) - set(pb))
        if set(pb) - set(pa) or not set(removed) <= {"source", "checksum"}:
            raise Stop(f"Cargo.lock: {key} changed beyond removing source/checksum")
        if any(pa[k] != pb[k] for k in pb):
            raise Stop(f"Cargo.lock: {key} changed a field")
        if pa != pb:
            changed_keys.append(key)
        changes.append({"name": key[0], "version": key[1], "removed": removed})
    if changed_keys != list(overridden):  # also implied by the source check above (defence in depth)
        raise Stop(f"Cargo.lock: changed {changed_keys}, expected exactly {list(overridden)}")
    return {"mode": "cli", "source": source, "overridden": [list(k) for k in overridden], "changes": changes}


def check_core_lock(l0: bytes, lc: bytes) -> None:
    """Core mode: no override, so the lock must not change at all."""
    if l0 != lc:
        raise Stop("mode core: Cargo.lock changed")


def workspace_members(core: Path) -> list[Path]:
    root = read_toml(core / "Cargo.toml")
    members = root.get("workspace", {}).get("members")
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        raise Stop("core workspace: no members list")
    out = []
    for m in members:
        if m.endswith("/*") and "*" not in m[:-2]:
            base = core / m[:-2]
            if not base.is_dir() or base.is_symlink():
                raise Stop(f"core workspace: member directory {m[:-2]!r} is missing")
            out += sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
        elif "*" not in m:
            out.append(core / m)
        else:
            raise Stop(f"core workspace: unsupported member pattern {m!r}")
    return out


def member_paths(core: Path, overridden: list[tuple[str, str]]) -> dict:
    """(name, version) -> the one workspace member dir declaring exactly that [package]."""
    found: dict = {}
    real_core = os.path.realpath(core) + os.sep
    for d in workspace_members(core):
        manifest = d / "Cargo.toml"
        try:
            info = os.lstat(manifest)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.path.realpath(manifest).startswith(real_core):
            raise Stop(f"core workspace: {manifest} is not a contained regular file")
        pkg = read_toml(manifest).get("package", {})
        key = (pkg.get("name"), pkg.get("version"))
        if key in overridden:
            if key in found:
                raise Stop(f"core workspace: duplicate member for {key}")
            found[key] = d
    missing = [k for k in overridden if k not in found]
    if missing:
        raise Stop(f"core workspace: no member with name and version {missing}")
    return found


def patch_section(overridden: list[tuple[str, str]], members: dict) -> str:
    lines = [f'\n[patch."{CORE_GIT[4:]}"]']
    for key in overridden:
        lines.append(f"{key[0]} = {{ path = {json.dumps(str(members[key]))} }}")
    return "\n".join(lines) + "\n"


def check_mode(repo: Path, mode: str) -> None:
    root = read_toml(repo / "Cargo.toml")
    if mode == "cli":
        if root.get("package", {}).get("name") != "rustic-rs":
            raise Stop("mode cli: Cargo.toml [package] name is not rustic-rs")
    elif mode == "core":
        if "package" in root or "workspace" not in root:
            raise Stop("mode core: Cargo.toml is not a workspace without [package]")
        if read_toml(repo / "crates/core/Cargo.toml").get("package", {}).get("name") != "rustic_core":
            raise Stop("mode core: crates/core is not rustic_core")
    else:
        raise Stop(f"unknown mode {mode!r}")


# ---- running commands: capped output, timeouts, signals ----
def decode_output(raw: bytes) -> str:
    return raw.decode("utf-8", "backslashreplace").replace("\0", "\\x00")


def run_capped(argv, cwd, env, timeout_s: float) -> dict:
    """Run with stdout+stderr on one pipe, killing the process group on timeout or over cap.

    One absolute deadline covers both reading the pipe and waiting for the exit, so a
    child that closes its output and then hangs is still a timeout.
    """
    started = time.monotonic()
    deadline = started + timeout_s
    proc = subprocess.Popen(
        [str(a) for a in argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    chunks, size, timed_out, over_cap = [], 0, False, False
    fd = proc.stdout.fileno()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([fd], [], [], min(left, 1.0))
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        size += len(chunk)
        if size > OUTPUT_CAP:
            over_cap = True
            break
        chunks.append(chunk)
    proc.stdout.close()
    if not (timed_out or over_cap):
        try:  # EOF on the pipe: wait for the exit only until the same deadline
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    if timed_out or over_cap:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        rc = proc.wait(timeout=60)  # reap; SIGKILL cannot be ignored
    except subprocess.TimeoutExpired:
        raise Stop(f"process {proc.pid} survived SIGKILL for 60 s") from None
    return {
        "rc": rc,
        "timed_out": timed_out,
        "over_cap": over_cap,
        "output": decode_output(b"".join(chunks)),
        "seconds": round(time.monotonic() - started, 1),
    }


def classify(result: dict, spec: dict, pieces: list[str]) -> tuple[str, str, str | None]:
    """(verdict, reason, matched line) for a mutant's test run."""
    if result["timed_out"]:
        return "INVALID", "timeout", None
    if result["over_cap"]:
        return "INVALID", "output over cap", None
    rc = result["rc"]
    if rc < 0:
        return "INVALID", f"signal {-rc}", None
    if rc == 0:
        return "SURVIVED", "test passed", None
    if rc not in spec["fail"]:
        return "INVALID", f"unexpected rc {rc}", None
    line = oracle_line(pieces, result["output"])
    if line is None:
        return "INVALID", "no oracle", None
    return "KILLED", "oracle matched", line


class Budget:
    """Start nothing whose bound would end past WORST minutes."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.start = clock()

    def allows(self, bound_min: int) -> bool:
        return (self.clock() - self.start) / 60 + bound_min <= time_model()["worst"]


# ---- the workflow file must match the time model ----
STEP_ID_RE = re.compile(r"      - id: ([a-z-]+)\s*$")
JOB_TIMEOUT_RE = re.compile(r"    timeout-minutes: (\d+)\s*$")
STEP_TIMEOUT_RE = re.compile(r"        timeout-minutes: (\d+)\s*$")
SETUP_STEPS = {"checkout", "toolchain", "loopback", "fetch"}
TEARDOWN_STEPS = {"gate", "upload", "verdict"}


def workflow_timeouts(text: str) -> dict:
    """{'job': N, 'steps': {id: N}} from the fixed-layout workflow file."""
    job, steps, current = None, {}, None
    for line in text.split("\n"):
        if m := STEP_ID_RE.fullmatch(line):
            current = m.group(1)
            if current in steps:
                raise ValueError(f"duplicate step id {current}")
            steps[current] = None
        elif m := STEP_TIMEOUT_RE.fullmatch(line):
            if current is None or steps[current] is not None:
                raise ValueError("step timeout outside a step")
            steps[current] = int(m.group(1))
        elif m := JOB_TIMEOUT_RE.fullmatch(line):
            if job is not None:
                raise ValueError("two job timeouts")
            job = int(m.group(1))
    if job is None or any(v is None for v in steps.values()):
        raise ValueError("missing job or step timeout")
    return {"job": job, "steps": steps}


def check_workflow(text: str) -> list[str]:
    """Problems with the workflow's timeouts against time_model(); empty means it matches."""
    t, model, problems = workflow_timeouts(text), time_model(), []
    steps = t["steps"]
    if t["job"] != model["job"]:
        problems.append(f"job timeout {t['job']} != {model['job']}")
    if steps.get("mutants") != model["step"]:
        problems.append(f"mutants step timeout {steps.get('mutants')} != {model['step']}")
    unknown = set(steps) - SETUP_STEPS - TEARDOWN_STEPS - {"mutants"}
    if unknown:
        problems.append(f"steps outside the time model: {sorted(unknown)}")
    if sum(v for k, v in steps.items() if k in SETUP_STEPS) > SETUP_MAX_MIN:
        problems.append("setup steps exceed SETUP_MAX")
    if sum(v for k, v in steps.items() if k in TEARDOWN_STEPS) > TEARDOWN_MAX_MIN:
        problems.append("teardown steps exceed TEARDOWN_MAX")
    return problems


# ---- the artifact: capped writes and the pre-upload gate ----
def write_capped(path: Path, data: bytes, cap: int) -> None:
    if len(data) > cap:
        raise Stop(f"{path.name}: {len(data)} bytes over its cap {cap}")
    path.write_bytes(data)


def expected_names(ids: list[str]) -> list[str]:
    return sorted([*FILE_CAPS, "logs", *(f"logs/{i}.txt" for i in ids)])


def gate_artifact(out: Path) -> list[str]:
    """Problems with the staged artifact; empty means it may be uploaded."""
    try:
        results = json.loads((out / "results.json").read_bytes())
        ids = [m["id"] for m in results["mutants"]]
        if not all(isinstance(i, str) and ID_RE.fullmatch(i) for i in ids):
            return ["results.json: bad ids"]
    except (OSError, ValueError, KeyError, TypeError) as err:
        return [f"results.json unreadable: {err}"]
    problems, names, total = [], [], 0
    for top, dirs, files in os.walk(out):
        for name in dirs + files:
            full = Path(top) / name
            rel = str(full.relative_to(out))
            names.append(rel)
            info = os.lstat(full)
            if rel == "logs":
                if not stat.S_ISDIR(info.st_mode):
                    problems.append("logs is not a directory")
                continue
            if not stat.S_ISREG(info.st_mode):
                problems.append(f"{rel}: not a regular file")
                continue
            cap = FILE_CAPS.get(rel, LOG_CAP)
            if info.st_size > cap:
                problems.append(f"{rel}: {info.st_size} bytes over {cap}")
            total += info.st_size
    if sorted(names) != expected_names(ids):
        problems.append(f"names differ: {sorted(set(names) ^ set(expected_names(ids)))}")
    if total > ARTIFACT_MAX:
        problems.append(f"total {total} over {ARTIFACT_MAX}")
    return problems


def results_md(results: dict) -> str:
    rows = ["| id | kind | test | verdict | reason | oracle line |", "|---|---|---|---|---|---|"]
    for m in results["mutants"]:
        line = (m.get("matched") or "").replace("|", "\\|").replace("`", "'")
        rows.append(f"| {m['id']} | {m['kind']} | `{m['test']}` | {m['verdict']} | {m['reason']} | `{line}` |")
    return f"Verdict: **{results['verdict']}**\n\n" + "\n".join(rows) + "\n"


# ---- the run ----
def git(args, cwd) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "LC_ALL": "C",
    }
    result = subprocess.run(
        ["git", "-c", f"safe.directory={cwd}", *args], cwd=cwd, env=env, capture_output=True, timeout=600
    )
    if result.returncode:
        raise Stop(f"git {' '.join(args)} exited {result.returncode}: {result.stderr.decode(errors='replace')[-500:]}")
    return result.stdout.decode()


def tool_env(target_dir: Path) -> dict:
    env = {"LANG": "C.UTF-8", "CARGO_TERM_COLOR": "never", "CARGO_TARGET_DIR": str(target_dir)}
    for name in ("PATH", "HOME", "CARGO_HOME", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN"):
        value = os.environ.get(name, "")
        if not value:
            raise Stop(f"environment: {name} is not set")
        env[name] = value
    return env  # PROJECT_VERSION is never passed: the CLI's version string comes from Cargo.toml


def rustc_ok(rustc_vv: str, expected: str) -> bool:
    if not re.fullmatch(r"\d+\.\d+\.\d+", expected):
        return False
    releases = [item.strip() for item in rustc_vv.split("\n") if item.strip().startswith("release:")]
    return releases == [f"release: {expected}"]


def sha256_file(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class Run:
    def __init__(self, args):
        self.mode = args.mode
        self.repo = Path(args.repo).resolve(strict=True)
        self.out = Path(args.out)
        self.work_root = Path(args.work_root).resolve(strict=True)
        self.tmp = Path(os.environ.get("RUNNER_TEMP") or "/tmp").resolve(strict=True) / "cocalc-mutants"
        self.intended = args.intended_sha
        self.expect_rustc = args.expect_rustc
        self.budget = Budget()
        self.here = Path(__file__).resolve().parent
        self.clean_results: dict = {}

    # -- trees --
    def worktree(self, repo: Path, path: Path, rev: str) -> Path:
        git(["worktree", "add", "--detach", str(path), rev], repo)
        return path

    def apply(self, tree: Path, data: bytes, name: str) -> None:
        screen_patch_text(data)
        patch = self.tmp / f"apply-{name}"
        patch.write_bytes(data)
        env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        base = ["git", "-c", f"safe.directory={tree}", "apply", "--whitespace=error"]
        numstat = subprocess.run([*base, "--numstat", "-z", str(patch)], cwd=tree, env=env, capture_output=True)
        if numstat.returncode:
            raise Invalid(f"git apply --numstat exited {numstat.returncode}")
        check_numstat(tree, parse_numstat_z(numstat.stdout))
        for extra in (["--check"], []):
            done = subprocess.run([*base, *extra, str(patch)], cwd=tree, env=env, capture_output=True)
            if done.returncode:
                raise Invalid(f"git apply {' '.join(extra)} exited {done.returncode}: {done.stderr.decode(errors='replace')[-300:]}")

    def build(self, tree: Path, target_dir: Path) -> tuple[Path, dict]:
        if self.mode == "cli":
            argv = ["cargo", "build", "--release", "--bin", "rustic"]
        else:
            argv = ["cargo", "test", "--locked", "-p", "rustic_core", "--lib", "--no-run", "--message-format=json-render-diagnostics"]
        result = run_capped(argv, tree, tool_env(target_dir), BUILD_MAX_MIN * 60)
        if result["timed_out"] or result["over_cap"] or result["rc"] != 0:
            raise Invalid(f"build failed (rc {result['rc']}, timeout {result['timed_out']}, over cap {result['over_cap']})")
        if self.mode == "cli":
            return target_dir / "release" / "rustic", result
        exes = []
        for line in result["output"].split("\n"):
            if line.startswith("{"):
                msg = json.loads(line)
                tgt = msg.get("target", {})
                if msg.get("reason") == "compiler-artifact" and tgt.get("name") == "rustic_core" and msg.get("profile", {}).get("test") and msg.get("executable"):
                    exes.append(msg["executable"])
        if len(exes) != 1:
            raise Invalid(f"expected one rustic_core lib test executable, found {len(exes)}")
        return Path(exes[0]), result

    def test(self, row: dict, binary: Path, label: str) -> dict:
        spec = row["spec"]
        if spec["kind"] == "selftest-version":
            want = read_toml(self.repo / "Cargo.toml")["package"]["version"]
            argv = [sys.executable, "-I", "-B", self.here / "run_mutants.py", "selftest-version", binary, want]
            cwd, env = self.tmp, {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
        elif spec["kind"] == "roundtrip":
            work = self.work_root / work_dir_name(label, spec)
            work.mkdir()
            argv = [sys.executable, "-B", self.clean_tree / "tests/cocalc" / spec["script"], binary, work, spec["scenario"]]
            cwd = self.clean_tree / "tests/cocalc"
            env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "ROUNDTRIP_STRICT": "1"}
        else:
            argv = [binary, "--exact", spec["name"], "--test-threads", "1"]
            cwd, env = self.clean_tree / "crates/core", {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
        return run_capped(argv, cwd, env, spec["timeout_min"] * 60)

    def clean_test(self, row: dict) -> None:
        """Once per distinct test: it must pass on the clean build (else STOP)."""
        if row["test"] in self.clean_results:
            return
        if not self.budget.allows(TEST_MAX_MIN):
            raise Stop("budget exhausted before a clean test")
        result = self.test(row, self.clean_binary, CLEAN_LABEL)
        must = row["spec"]["clean_must"]
        if result["rc"] != 0 or result["timed_out"] or result["over_cap"] or (must and must not in result["output"]):
            raise Stop(f"clean run of {row['test']} did not pass (rc {result['rc']}): {result['output'][-1000:]}")
        self.clean_results[row["test"]] = {"rc": 0, "seconds": result["seconds"]}

    def mutant(self, row: dict, overridden, members_of, cleanup: list) -> dict:
        rec = {"id": row["id"], "target": row["target"], "kind": row["kind"], "test": row["test"],
               "patch_sha256": hashlib.sha256(row["patch_bytes"]).hexdigest(), "rc": None, "matched": None}
        log = ""
        try:
            if shutil.disk_usage(self.tmp).free < MIN_FREE_BYTES:
                raise Invalid("disk")
            if not self.budget.allows(BUILD_MAX_MIN + TEST_MAX_MIN):
                raise Invalid("budget")
            tree = self.worktree(self.repo, self.tmp / f"mut-{row['id']}", "HEAD")
            cleanup.append(tree)
            if self.mode == "cli":
                core = self.core_clean
                if row["target"] == "core":
                    core = self.worktree(self.core_src, self.tmp / f"core-mut-{row['id']}", self.core_rev)
                    cleanup.append(core)
                    self.apply(core, row["patch_bytes"], row["id"])
                else:
                    self.apply(tree, row["patch_bytes"], row["id"])
                with open(tree / "Cargo.toml", "a") as f:
                    f.write(patch_section(overridden, member_paths(core, overridden)))
            else:
                self.apply(tree, row["patch_bytes"], row["id"])
            target_dir = self.tmp / f"target-mut-{row['id']}"
            cleanup.append(target_dir)
            binary, build = self.build(tree, target_dir)
            rec["build_s"] = build["seconds"]
            if (tree / "Cargo.lock").read_bytes() != self.lc_bytes:
                raise Stop(f"{row['id']}: the mutant's Cargo.lock differs from the clean one")
            rec["binary_sha256"] = sha256_file(binary)
            if rec["binary_sha256"] == self.clean_sha and row["kind"] != "ctl-survive":
                raise Invalid("patch did not change the binary")
            self.clean_test(row)
            if not self.budget.allows(TEST_MAX_MIN):
                raise Invalid("budget")
            result = self.test(row, binary, row["id"])
            log = result["output"]
            rec["rc"], rec["test_s"] = result["rc"], result["seconds"]
            rec["verdict"], rec["reason"], rec["matched"] = classify(result, row["spec"], row["pieces"])
        except Invalid as why:
            rec["verdict"], rec["reason"] = "INVALID", str(why)
        data = log.encode("utf-8", "backslashreplace")[-LOG_CAP:]
        write_capped(self.out / "logs" / f"{row['id']}.txt", data, LOG_CAP)
        return rec

    def remove(self, paths: list) -> None:
        for path in reversed(paths):
            real = os.path.realpath(path)
            if not real.startswith(str(self.tmp) + os.sep):
                raise Stop(f"refusing to remove {real}")
            if os.path.lexists(real):
                shutil.rmtree(real)
        git(["worktree", "prune"], self.repo)
        if self.mode == "cli":
            git(["worktree", "prune"], self.core_src)
        paths.clear()

    def run(self) -> int:
        check_mode(self.repo, self.mode)
        head = git(["rev-parse", "HEAD"], self.repo).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", self.intended) or head != self.intended:
            raise Stop(f"HEAD {head} is not the intended commit {self.intended}")
        rustc = subprocess.run(["rustc", "-vV"], cwd=self.repo, env=tool_env(self.tmp), capture_output=True, text=True)
        if rustc.returncode or not rustc_ok(rustc.stdout, self.expect_rustc):
            raise Stop(f"rustc is not exactly {self.expect_rustc}")
        here = self.repo / ".github/cocalc-mutants"
        rows = load_manifest((here / "MUTANTS.tsv").read_text("utf-8"), self.mode, here / "patches")
        problems = check_workflow((self.repo / ".github/workflows/cocalc-mutants.yml").read_text("utf-8"))
        if problems:
            raise Stop(f"workflow timeouts: {problems}")
        self.tmp.mkdir()
        (self.out / "logs").mkdir(parents=True)
        l0_bytes = (self.repo / "Cargo.lock").read_bytes()
        self.clean_tree = self.worktree(self.repo, self.tmp / "clean", "HEAD")
        overridden, members = [], {}
        if self.mode == "cli":
            l0 = tomllib.loads(l0_bytes.decode("utf-8"))
            source, self.core_rev, overridden = override_set(l0)
            self.core_src = self.tmp / "core-src"
            subprocess.run(["git", "clone", "--quiet", "--no-checkout", CORE_GIT[4:], str(self.core_src)], check=True,
                           env={"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}, timeout=600)
            self.core_clean = self.worktree(self.core_src, self.tmp / "core-clean", self.core_rev)
            members = member_paths(self.core_clean, overridden)
            with open(self.clean_tree / "Cargo.toml", "a") as f:
                f.write(patch_section(overridden, members))
        self.clean_binary, _ = self.build(self.clean_tree, self.tmp / "target-clean")
        self.lc_bytes = (self.clean_tree / "Cargo.lock").read_bytes()
        if self.mode == "cli":
            transform = compare_locks_cli(l0, tomllib.loads(self.lc_bytes.decode("utf-8")), source, overridden)
            transform["members"] = {f"{k[0]} {k[1]}": str(v.relative_to(self.core_clean)) for k, v in members.items()}
        else:
            check_core_lock(l0_bytes, self.lc_bytes)
            transform = {"mode": "core", "identical": True}
        write_capped(self.out / "lock-transform.json", json.dumps(transform, indent=1).encode(), FILE_CAPS["lock-transform.json"])
        self.clean_sha = sha256_file(self.clean_binary)
        records = []
        for row in sorted(rows, key=lambda r: (r["test"], r["id"])):
            cleanup: list = []
            try:
                records.append(self.mutant(row, overridden, members, cleanup))
            finally:
                self.remove(cleanup)
        offenders = [r["id"] for r in records if r["verdict"] != EXPECTED_VERDICT[r["kind"]]]
        results = {
            "schema": 1,
            "mode": self.mode,
            "intended_sha": self.intended,
            "time_model": time_model(),
            "clean_binary_sha256": self.clean_sha,
            "clean_tests": self.clean_results,
            "mutants": records,
            "offenders": offenders,
            "verdict": "PASS" if not offenders else "FAIL",
        }
        write_capped(self.out / "results.md", results_md(results).encode(), FILE_CAPS["results.md"])
        write_capped(self.out / "results.json", json.dumps(results, indent=1).encode(), FILE_CAPS["results.json"])
        print(results_md(results))
        return 0 if not offenders else 1


def selftest_version(binary: str, want: str) -> int:
    result = run_capped([binary, "--version"], None, {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}, 60)
    got = result["output"].strip()
    if result["rc"] == 0 and got == f"rustic {want}":
        print("version ok")
        return 0
    print(f"version mismatch: rc {result['rc']}, got {got[:200]!r}, want {'rustic ' + want!r}")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", required=True, choices=["cli", "core"])
    for name in ("--repo", "--out", "--work-root", "--intended-sha", "--expect-rustc"):
        run.add_argument(name, required=True)
    gate = sub.add_parser("gate-artifact")
    gate.add_argument("dir")
    version = sub.add_parser("selftest-version")
    version.add_argument("binary")
    version.add_argument("version")
    args = parser.parse_args(argv)
    if args.cmd == "selftest-version":
        return selftest_version(args.binary, args.version)
    if args.cmd == "gate-artifact":
        problems = gate_artifact(Path(args.dir))
        for p in problems:
            print(f"artifact gate: {p}", file=sys.stderr)
        return 1 if problems else 0
    try:
        try:
            return Run(args).run()
        except Stop:
            raise
        except Exception as err:  # anything unplanned is a STOP, never a verdict
            raise Stop(f"unexpected {type(err).__name__}: {err}") from err
    except Stop as why:
        print(f"STOP: {why}", file=sys.stderr)
        out = Path(args.out)
        try:
            (out / "logs").mkdir(parents=True, exist_ok=True)
            for name in ("lock-transform.json", "results.md"):
                if not (out / name).exists():
                    write_capped(out / name, b"{}" if name.endswith("json") else b"Verdict: **STOP**\n", FILE_CAPS[name])
            stop = {"schema": 1, "mode": args.mode, "verdict": "STOP", "reason": str(why)[:2000], "mutants": []}
            for leftover in (out / "logs").iterdir():
                leftover.unlink()
            write_capped(out / "results.json", json.dumps(stop, indent=1).encode(), FILE_CAPS["results.json"])
        except OSError as err:
            print(f"STOP record not written: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
