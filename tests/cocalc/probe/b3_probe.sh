#!/bin/bash
# Empirical questions for extent-identity reuse, on a fresh btrfs loopback.
# Usage (as root): b3_probe.sh MOUNTPOINT ITEMS_PY
# Prints EXTENT_DATA item generations and EXTENT_ITEM generations for:
#   1-2. a file and its reflink clone
#   3.   a new file that reuses the disk_bytenr of a deleted file
#   5.   a buffered overwrite that is still dirty (delalloc) vs after sync
set -u
m="$1"; items="$2"
cd "$m"
show() { python3 -I "$items" "$@"; }
fm() { python3 -I - "$@" <<'EOF'
import fcntl, struct, sys
HDR = struct.Struct("=QQIIII"); EXT = struct.Struct("=QQQQQIIII")
for p in sys.argv[2:]:
    sync = sys.argv[1] == "sync"
    buf = bytearray(HDR.size + 32 * EXT.size)
    HDR.pack_into(buf, 0, 0, 2**64 - 1, 1 if sync else 0, 0, 32, 0)
    with open(p, "rb") as f:
        fcntl.ioctl(f.fileno(), 0xC020660B, buf, True)
    n = HDR.unpack_from(buf, 0)[3]
    exts = [EXT.unpack_from(buf, HDR.size + i * EXT.size) for i in range(n)]
    print(f"  FIEMAP({'SYNC' if sync else 'nosync'}) {p}: " + "; ".join(
        f"log={e[0]} phys={e[1]} len={e[2]} flags={e[5]:#x}" for e in exts))
EOF
}
echo "== kernel $(uname -r); mount: $(findmnt -n -o FSTYPE,OPTIONS -T "$m")"

echo "== 1-2. reflink clone: item gen and extent-item gen of the source and the clone"
head -c 1048576 /dev/urandom > a; sync; sleep 6; sync
cp --reflink=always a b; sync
show a b

echo "== 3. freed-extent reuse"
head -c 1048576 /dev/urandom > victim; sync
old=$(show victim | sed -n 's/.*disk_bytenr=\([0-9]*\).*/\1/p' | head -1)
echo "  victim disk_bytenr=$old"; show victim
rm victim; sync; sleep 2; sync; sync
found=""
for i in $(seq 1 300); do
  head -c 1048576 /dev/urandom > "new$i"; sync
  b=$(show "new$i" | sed -n 's/.*disk_bytenr=\([0-9]*\).*/\1/p' | head -1)
  if [ "$b" = "$old" ]; then found="new$i"; break; fi
done
if [ -n "$found" ]; then echo "  reused by $found after $i files:"; show "$found"; else echo "  not reused within 300 files"; fi

echo "== 5. dirty buffered overwrite of an extent shared with a reflink sibling"
head -c 1048576 /dev/urandom > c; sync; cp --reflink=always c d; sync
echo "  before overwrite:"; show c d
dd if=/dev/urandom of=c bs=4096 count=1 seek=10 conv=notrunc status=none   # buffered, no sync
echo "  dirty (no sync yet):"; show c; fm nosync c
fm sync c
echo "  after FIEMAP_FLAG_SYNC:"; show c
sync; echo "  after sync:"; show c d

echo "== 4. compressible data: clone and freed-extent reuse (compressed extents on compress=zstd)"
yes "compressible line of text for the probe" | head -c 1048576 > z; sync; sleep 6; sync
cp --reflink=always z z2; sync
show z z2
oldz=$(show z | sed -n 's/.*disk_bytenr=\([0-9]*\).*/\1/p' | head -1)
rm z z2; sync; sleep 2; sync; sync
for i in $(seq 1 300); do
  yes "other compressible text $i" | head -c 1048576 > "nz$i"; sync
  b=$(show "nz$i" | sed -n 's/.*disk_bytenr=\([0-9]*\).*/\1/p' | head -1)
  if [ "$b" = "$oldz" ]; then echo "  reused by nz$i:"; show "nz$i"; break; fi
done
