#!/usr/bin/env python3
"""Back up trees of every kind of entry, restore them several ways, and compare.

Usage: roundtrip.py RUSTIC_BINARY WORK_DIR [SCENARIO...]

WORK_DIR is a fresh directory on the filesystem under test (CI runs this on
btrfs, btrfs with compress=zstd, and ext4 loopbacks).  As root it also covers
owners, devices, mode-000 entries and security/trusted xattrs; otherwise those
cases are reported as skipped.  With ROUNDTRIP_STRICT=1 (as in CI) a skipped
case fails the run unless CI_ALLOWED_SKIPS names it with a reason.  The fixture cases are in rt/fixture.py, the restore
scenarios in rt/scenarios.py.  Exits non-zero with the differences listed per
scenario.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from rt.scenarios import EXPECTED_FAILURES, Context, UnknownScenario, run_all, unexplained

# Fixture case -> why CI can't run it.  Under ROUNDTRIP_STRICT=1 every other
# skip fails the run.
CI_ALLOWED_SKIPS: dict[str, str] = {}


def main() -> int:
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    work = Path(sys.argv[2]).resolve(strict=True)
    ctx = Context(binary, work, as_root=os.geteuid() == 0)
    try:
        results = run_all(ctx, only=set(sys.argv[3:]))
    except UnknownScenario as err:
        print(err, file=sys.stderr)
        return 2
    if not results:
        print("no scenario ran", file=sys.stderr)
        return 2
    fs = subprocess.run(["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "-T", str(work)], capture_output=True, text=True)
    failed = sorted(n for n, problems in results.items() if unexplained(n, problems))
    expected = sorted(n for n, problems in results.items() if problems and n in EXPECTED_FAILURES and n not in failed)
    unexpected = sorted(n for n, problems in results.items() if not problems and n in EXPECTED_FAILURES)
    report = {
        "filesystem": fs.stdout.strip(),
        "entries": len(ctx.want),
        "skipped": ctx.skipped,
        "passed": sorted(n for n, problems in results.items() if not problems and n not in EXPECTED_FAILURES),
        "failed": failed,
        "expected_failures": {n: {"reason": EXPECTED_FAILURES[n][0], "problems": results[n]} for n in expected},
        "unexpected_passes": unexpected,
        "problems": {n: unexplained(n, results[n]) for n in failed},
    }
    print(json.dumps(report, indent=2))
    if os.environ.get("ROUNDTRIP_STRICT") == "1":
        refused = [s for s in ctx.skipped if s.split(":", 1)[0] not in CI_ALLOWED_SKIPS]
        if refused:
            print(f"ROUNDTRIP_STRICT: skips not in CI_ALLOWED_SKIPS: {refused}", file=sys.stderr)
            return 1
    return 1 if failed or unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
