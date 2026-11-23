#!/usr/bin/env bash
# Run C (SPEC.md): an instrumented rustic binary, driven by the Python suites in
# tests/cocalc, as root, on loopback btrfs, btrfs-zstd and ext4 in turn.  One profile
# data set covers all three.  Usage: run_c.sh OUTDIR SEED
# It never stops early: suite results, profile liveness and reports go to OUTDIR,
# and finish.py decides CANDIDATE or DIAGNOSTIC.
set -uo pipefail
out="${1:?outdir}"; seed="${2:?seed}"
mkdir -p "$out"
rec() { printf '%s=%s\n' "$1" "$2" >> "$out/record.env"; }
: > "$out/record.env"
rec run C
rec seed "$seed"
. "$(dirname "$0")/record_platform.sh"

# the test-hooks binary is only the extent-reuse oracle's negative control: not instrumented
cargo build --locked --release --features test-hooks --target-dir target-hooks > "$out/build-hooks.log" 2>&1
rec build_hooks_rc "$?"
hooks="$PWD/target-hooks/release/rustic"

# shellcheck disable=SC1090
source <(cargo llvm-cov show-env --export-prefix)
rec llvm_profile_file "${LLVM_PROFILE_FILE:-}"
cargo llvm-cov clean --workspace > "$out/clean-workspace.log" 2>&1
rec clean_workspace_rc "$?"   # the empty starting set of raw profiles
cargo build --locked > "$out/build.log" 2>&1
rec build_rc "$?"
bin="$PWD/target/debug/rustic"
profdir="$(dirname "${LLVM_PROFILE_FILE:-/nonexistent/x}")"
mkdir -p "$profdir"
live() { find "$profdir" -name '*.profraw' -size +0c 2>/dev/null | wc -l; }

suite() {  # suite FS NAME ARGS...: run as root with the profile variable passed explicitly
  local fs="$1" name="$2"; shift 2
  local before after
  before=$(live)
  sudo env "LLVM_PROFILE_FILE=$LLVM_PROFILE_FILE" "$@" > "$out/suite-$fs-$name.log" 2>&1
  rec "suite_${fs}_${name}_rc" "$?"
  after=$(live)
  rec "suite_${fs}_${name}_new_profraw" "$((after - before))"
}

for fs in btrfs btrfs-zstd ext4; do
  disk="$(mktemp /tmp/cocalc-cov.XXXXXX.img)"
  mount="$(mktemp -d /tmp/cocalc-cov.XXXXXX)"
  case "$fs" in
    btrfs) mkfs=(sudo mkfs.btrfs -q "$disk"); options=loop ;;
    btrfs-zstd) mkfs=(sudo mkfs.btrfs -q "$disk"); options=loop,compress=zstd ;;
    ext4) mkfs=(sudo mkfs.ext4 -q -F "$disk"); options=loop ;;
  esac
  truncate -s 4G "$disk" && "${mkfs[@]}" > "$out/mkfs-$fs.log" 2>&1
  rec "mkfs_${fs}_rc" "$?"
  sudo mount -o "$options" "$disk" "$mount"
  rec "mount_${fs}_rc" "$?"
  for d in work b3 crash corrupt b6; do sudo mkdir "$mount/$d"; done
  suite "$fs" roundtrip env ROUNDTRIP_STRICT=1 python3 -B tests/cocalc/roundtrip.py "$bin" "$mount/work"
  suite "$fs" b3_oracle env B3_HOOKS_BINARY="$hooks" B3_ORACLE_REQUIRE_FLAG=1 \
    python3 -B tests/cocalc/b3_oracle.py "$bin" "$mount/b3"
  suite "$fs" crash env CRASH_ITERATIONS=4 CRASH_SEED="$seed" python3 -B tests/cocalc/crash.py "$bin" "$mount/crash"
  suite "$fs" corrupt env CORRUPT_SEED="$seed" python3 -B tests/cocalc/corrupt.py "$bin" "$mount/corrupt"
  suite "$fs" b6_adversary python3 -B tests/cocalc/b6_adversary.py "$bin" "$mount/b6"
  sudo umount "$mount"
  rec "umount_${fs}_rc" "$?"
  rmdir "$mount" && rm -f "$disk"
  rec "cleanup_${fs}_rc" "$?"
done

# profiles written by root: hand them back before the report
sudo chown -R "$(id -u):$(id -g)" "$profdir"
rec chown_rc "$?"
cargo llvm-cov report --json --output-path "$out/C.json" > "$out/report-json.log" 2>&1
rec json_rc "$?"
cargo llvm-cov report --lcov --output-path "$out/C.lcov" > "$out/report-lcov.log" 2>&1
rec lcov_rc "$?"
cargo llvm-cov report --summary-only > "$out/summary.txt" 2> "$out/report-summary.log"
rec summary_rc "$?"
rec export_sha256 "$(sha256sum "$out/C.json" 2>/dev/null | cut -d' ' -f1)"
finish_record
exit 0
