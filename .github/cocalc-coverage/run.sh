#!/usr/bin/env bash
# One coverage run of the Rust tests, as defined in SPEC.md next to this file.
# Usage: run.sh RUN OUTDIR   (RUN: A | A-hooks | B)
# It never stops early: every result (test rc, reports, platform record) is written
# to OUTDIR, and finish.py decides CANDIDATE or DIAGNOSTIC from them afterwards.
set -uo pipefail
run="${1:?run}"; out="${2:?outdir}"
case "$run" in
  A) args=(--workspace) ;;
  A-hooks) args=(--workspace --features rustic_core/test-hooks) ;;
  B) args=() ;;
  *) echo "unknown run: $run" >&2; exit 2 ;;
esac
mkdir -p "$out"
rec() { printf '%s=%s\n' "$1" "$2" >> "$out/record.env"; }
: > "$out/record.env"
rec run "$run"
. "$(dirname "$0")/record_platform.sh"
rec test_command "cargo llvm-cov ${args[*]} --locked --no-fail-fast --json --output-path $run.json"

# the measured run: its exit status is recorded, not acted on here
cargo llvm-cov "${args[@]}" --locked --no-fail-fast --json --output-path "$out/$run.json" \
  > "$out/test-output.txt" 2>&1
rec test_rc "$?"
# a failed test run may not have written the export: rebuild it from the same profile data
# json_rc: 0 when the measured command wrote a non-empty export, else the report command's rc
if [ -s "$out/$run.json" ]; then
  rec json_source measured
  rec json_rc 0
else
  rec json_source report
  cargo llvm-cov report "${args[@]}" --json --output-path "$out/$run.json" >> "$out/test-output.txt" 2>&1
  rec json_rc "$?"
fi
cargo llvm-cov report "${args[@]}" --lcov --output-path "$out/$run.lcov" > "$out/report-lcov.log" 2>&1
rec lcov_rc "$?"
cargo llvm-cov report "${args[@]}" --summary-only > "$out/summary.txt" 2> "$out/report-summary.log"
rec summary_rc "$?"
rec export_sha256 "$(sha256sum "$out/$run.json" 2>/dev/null | cut -d' ' -f1)"
finish_record
exit 0
