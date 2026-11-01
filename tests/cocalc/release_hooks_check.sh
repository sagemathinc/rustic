#!/usr/bin/env bash
# Proves that a rustic binary ignores every `test-hooks` switch: the marker
# files a backed-up tree could contain, and every RUSTIC_TEST_* variable.
#
#   release_hooks_check.sh RUSTIC                  # must ignore them all
#   release_hooks_check.sh RUSTIC --expect-hooks   # control: a test-hooks build
#
# The tree holds two files with the same fake signature (`SIG:x`) but different
# bytes, plus the markers that turn on the fake signature source and make its
# end check fail. A binary honoring them would reuse a's content for b (a wrong
# restore), fail the run, log "test-hooks", or create the pause marker.
set -euo pipefail

bin=$(realpath "$1")
mode=${2:-}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
src=$work/src
hooks=$work/hooks
mkdir "$src" "$hooks"
: >"$hooks/release" # never block, even if the pause hook were honored
: >"$src/.rustic-test-fake-extents"
: >"$src/.rustic-test-end-fails"
for f in a b; do
  { printf 'SIG:x\n'; head -c 300000 /dev/urandom; } >"$src/$f"
done
cmp -s "$src/a" "$src/b" && { echo "FAIL: fixture files are equal" >&2; exit 1; }

export RUSTIC_PASSWORD=release-hooks-check
export RUSTIC_TEST_SKIP_EXTENT_BARRIER=1
export RUSTIC_TEST_PAUSE_AFTER_SCANS=1
export RUSTIC_TEST_PAUSE_DIR=$hooks
"$bin" -r "$work/repo" --no-progress init >/dev/null 2>&1

set +e
(cd "$src" && timeout 300 "$bin" -r "$work/repo" --no-progress backup --json --reuse-extents .) \
  >"$work/out" 2>"$work/err"
rc=$?
set -e

if [ "$mode" = --expect-hooks ]; then
  # the control shows that this check sees the hooks when they are built in
  if [ "$rc" -ne 0 ] && [ -e "$hooks/paused" ] && grep -q 'test-hooks' "$work/err"; then
    echo "OK: control: the test-hooks build honors the switches (rc=$rc, paused, logged)"
    exit 0
  fi
  echo "FAIL: control: the test-hooks build was not detected (rc=$rc)" >&2
  cat "$work/err" >&2
  exit 1
fi

fail() {
  echo "FAIL: $*" >&2
  cat "$work/err" >&2
  exit 1
}
[ "$rc" -eq 0 ] || fail "the backup failed (rc=$rc): the end-check marker was honored?"
! grep -q 'test-hooks' "$work/err" || fail "a test-hooks switch was logged"
[ ! -e "$hooks/paused" ] || fail "the pause hook was honored"
id=$(python3 -c 'import json, sys
s = json.load(open(sys.argv[1]))
reused = (s.get("summary") or {}).get("extent_reused_files", 0)
assert reused == 0, f"extent_reused_files = {reused}"
print(s["id"])' "$work/out") || fail "content was reused by extent signature"
"$bin" -r "$work/repo" --no-progress restore "$id" "$work/restore" >/dev/null 2>&1 || fail "restore failed"
for f in a b .rustic-test-fake-extents .rustic-test-end-fails; do
  cmp -s "$src/$f" "$work/restore/$f" || fail "$f restored differently"
done
echo "OK: $(basename "$bin") ignores every test-hooks switch (no reuse, byte-correct restore)"
