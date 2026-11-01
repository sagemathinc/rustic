#!/usr/bin/env python3
"""Print btrfs EXTENT_DATA items (file tree) and the matching EXTENT_ITEMs
(extent tree) for files, via BTRFS_IOC_TREE_SEARCH_V2.  Needs CAP_SYS_ADMIN.
Read-only.

Usage: btrfs_items.py FILE [FILE...]
"""
import fcntl
import os
import struct
import sys

BTRFS_IOC_TREE_SEARCH_V2 = 0xC0709411
BTRFS_IOC_INO_LOOKUP = 0xD0009412
KEY = struct.Struct("=QQQQQQQQQIIIIIQQQQ")  # btrfs_ioctl_search_key: 104 bytes
HDR = struct.Struct("=QQQII")               # transid, objectid, offset, type, len
EXTENT_DATA_KEY, EXTENT_ITEM_KEY, METADATA_ITEM_KEY = 108, 168, 169
EXTENT_TREE = 2
TYPES = {0: "inline", 1: "regular", 2: "prealloc"}


def search(fd, tree, min_obj, max_obj, min_type, max_type, min_off=0, max_off=2**64 - 1):
    out = []
    while True:
        bufsize = 64 * 1024
        key = KEY.pack(tree, min_obj, max_obj, min_off, max_off, 0, 2**64 - 1,
                       min_type, max_type, 4096, 0, 0, 0, 0, 0, 0, 0, 0)
        arg = bytearray(key + struct.pack("=Q", bufsize) + bytes(bufsize))
        fcntl.ioctl(fd, BTRFS_IOC_TREE_SEARCH_V2, arg, True)
        nr = struct.unpack_from("=I", arg, 64)[0]
        pos = KEY.size + 8
        last = None
        for _ in range(nr):
            transid, objectid, offset, typ, ln = HDR.unpack_from(arg, pos)
            pos += HDR.size
            out.append((transid, objectid, offset, typ, bytes(arg[pos:pos + ln])))
            pos += ln
            last = (objectid, typ, offset)
        if nr == 0 or last is None:
            return out
        # continue after the last key
        obj, typ, off = last
        if off < 2**64 - 1:
            min_obj, min_type, min_off = obj, typ, off + 1
        elif typ < 255:
            min_obj, min_type, min_off = obj, typ + 1, 0
        else:
            min_obj, min_type, min_off = obj + 1, 0, 0
        if (min_obj, min_type) > (max_obj, max_type):
            return out


def tree_id(fd, inode):
    # INO_LOOKUP with treeid 0 and objectid BTRFS_FIRST_FREE_OBJECTID returns the root id of fd
    arg = bytearray(struct.pack("=QQ", 0, 256) + bytes(4080))
    fcntl.ioctl(fd, BTRFS_IOC_INO_LOOKUP, arg, True)
    return struct.unpack_from("=Q", arg, 0)[0]


def describe(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        root = tree_id(fd, st.st_ino)
        items = search(fd, root, st.st_ino, st.st_ino, EXTENT_DATA_KEY, EXTENT_DATA_KEY)
        print(f"{path}: root {root} inode {st.st_ino} size {st.st_size}")
        for transid, _, off, _, data in items:
            gen, ram, comp, enc, oenc, typ = struct.unpack_from("=QQBBHB", data, 0)
            line = f"  EXTENT_DATA file_off={off} item_transid={transid} gen={gen} type={TYPES.get(typ, typ)} comp={comp} ram={ram}"
            if typ != 0:
                bytenr, num, eoff, nbytes = struct.unpack_from("=QQQQ", data, 21)
                line += f" disk_bytenr={bytenr} disk_num={num} offset={eoff} num_bytes={nbytes}"
                if bytenr:
                    ex = search(fd, EXTENT_TREE, bytenr, bytenr, EXTENT_ITEM_KEY, EXTENT_ITEM_KEY)
                    for etrans, eobj, eoff2, _, edata in ex:
                        if eoff2 == num:
                            refs, egen, flags = struct.unpack_from("=QQQ", edata, 0)
                            line += f" | EXTENT_ITEM refs={refs} gen={egen} flags={flags}"
            print(line)
    finally:
        os.close(fd)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        describe(p)
