#!/usr/bin/env python3
"""Extent-reuse oracle: fixture checks today, the with/without differential
once the binary has --reuse-extents.

Usage (as root, on a btrfs or ext4 loopback): b3_oracle.py RUSTIC WORK_DIR [SCENARIO...]

B3_HOOKS_BINARY: a rustic built with the test-hooks feature, for s7d's
negative control (pending without it).

Exit 1 on any fixture or differential problem.  With B3_ORACLE_REQUIRE_FLAG=1
a binary without --reuse-extents is a failure too (for after the core lands).
"""
import json
import os
from pathlib import Path
import sys

from b3 import scenarios as sc


def main() -> int:
    hooks = os.environ.get("B3_HOOKS_BINARY")
    ctx = sc.Context(str(Path(sys.argv[1]).resolve(strict=True)), Path(sys.argv[2]).resolve(strict=True),
                     str(Path(hooks).resolve(strict=True)) if hooks else None)
    names = set(sys.argv[3:])
    chosen = (sc.BTRFS_SCENARIOS if ctx.btrfs else []) + [sc.s10_fallback]
    known = {s.__name__ for s in sc.BTRFS_SCENARIOS + [sc.s10_fallback]}
    if names - known:
        print(f"unknown scenarios {sorted(names - known)}; known: {sorted(known)}", file=sys.stderr)
        return 2
    report, failed = {}, False
    for scenario in chosen:
        if names and scenario.__name__ not in names:
            continue
        try:
            fixture, diff = scenario(ctx)
        except Exception as err:  # any error is a problem, never a pass
            fixture, diff = [f"error: {type(err).__name__}: {err}"], []
        if fixture is None:
            report[scenario.__name__] = "not applicable on this filesystem"
            continue
        if diff is None:
            diff_report = "pending: needs B3_HOOKS_BINARY (a test-hooks build)"
        elif not ctx.has_flag:
            diff_report = "pending: binary lacks --reuse-extents"
        else:
            diff_report = diff or "ok"
        report[scenario.__name__] = {"fixture": fixture or "ok", "differential": diff_report}
        failed |= bool(fixture) or bool(diff)
        if diff is None and ctx.has_flag and os.environ.get("B3_ORACLE_REQUIRE_FLAG") == "1":
            failed = True  # with the flag required, every differential must actually run
    report["pending"] = dict(sc.PENDING, **({} if ctx.hooks_binary else
                             {"s7d_control": "needs B3_HOOKS_BINARY (a test-hooks build)"}))
    print(json.dumps({"filesystem": "btrfs" + (" (compressed)" if ctx.compressed else "") if ctx.btrfs else "other",
                      "has_flag": ctx.has_flag, "scenarios": report}, indent=2))
    if os.environ.get("B3_ORACLE_REQUIRE_FLAG") == "1" and not ctx.has_flag:
        print("B3_ORACLE_REQUIRE_FLAG=1 but the binary lacks --reuse-extents", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
