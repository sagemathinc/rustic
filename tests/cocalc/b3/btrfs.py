"""btrfs inspection for the extent-reuse oracle: file extent signatures via
TREE_SEARCH_V2 (needs CAP_SYS_ADMIN), FIEMAP and subvolume helpers.

The signature mirrors the approved design (v1, item generation only): for each
EXTENT_DATA item of the inode, in file-offset order, (file offset, type,
compression, encryption, other_encoding, disk_bytenr, disk_num_bytes, offset,
num_bytes, ram_bytes, generation), plus the file size.  This module only reads
it, so fixtures can prove they produced the state a scenario is about; the
implementation under test computes its own.
"""
import fcntl
import os
import struct
import subprocess
from typing import NamedTuple

BTRFS_SUPER_MAGIC = 0x9123683E
TREE_SEARCH_V2 = 0xC0709411
INO_LOOKUP = 0xD0009412
FS_IOC_FIEMAP = 0xC020660B
SEARCH_KEY = struct.Struct("=QQQQQQQIIIIQQQQ")  # btrfs_ioctl_search_key, 104 bytes
SEARCH_HEADER = struct.Struct("=QQQII")         # transid, objectid, offset, type, len
EXTENT_DATA_KEY = 108
FILE_EXTENT_HEAD = struct.Struct("=QQBBHB")     # generation, ram_bytes, compression, encryption, other_encoding, type
FILE_EXTENT_DISK = struct.Struct("=QQQQ")       # disk_bytenr, disk_num_bytes, offset, num_bytes
INLINE, REGULAR, PREALLOC = 0, 1, 2
assert SEARCH_KEY.size == 104 and FILE_EXTENT_HEAD.size == 21


class Extent(NamedTuple):
    file_offset: int
    type: int
    compression: int
    encryption: int
    other_encoding: int
    disk_bytenr: int
    disk_num_bytes: int
    offset: int
    num_bytes: int
    ram_bytes: int
    generation: int


def _search(fd: int, tree: int, objectid: int, item_type: int) -> list[tuple[int, bytes]]:
    """(key offset, item data) of every item (objectid, item_type, *) in tree."""
    out: list[tuple[int, bytes]] = []
    min_offset = 0
    while True:
        bufsize = 64 * 1024
        key = SEARCH_KEY.pack(tree, objectid, objectid, min_offset, 2**64 - 1, 0, 2**64 - 1,
                              item_type, item_type, 4096, 0, 0, 0, 0, 0)
        arg = bytearray(key + struct.pack("=Q", bufsize) + bytes(bufsize))
        fcntl.ioctl(fd, TREE_SEARCH_V2, arg, True)
        count = struct.unpack_from("=I", arg, 64)[0]
        pos = SEARCH_KEY.size + 8
        last = None
        for _ in range(count):
            _, obj, offset, typ, length = SEARCH_HEADER.unpack_from(arg, pos)
            pos += SEARCH_HEADER.size
            if obj == objectid and typ == item_type:
                out.append((offset, bytes(arg[pos:pos + length])))
                last = offset
            pos += length
        if count == 0 or last is None or last == 2**64 - 1:
            return out
        min_offset = last + 1


def subvolume_id(fd: int) -> int:
    arg = bytearray(struct.pack("=QQ", 0, 256) + bytes(4080))
    fcntl.ioctl(fd, INO_LOOKUP, arg, True)
    return struct.unpack_from("=Q", arg, 0)[0]


def extents(path) -> list[Extent]:
    """The inode's EXTENT_DATA items, in file-offset order (no flush)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        ino = os.fstat(fd).st_ino
        out = []
        for offset, data in _search(fd, subvolume_id(fd), ino, EXTENT_DATA_KEY):
            gen, ram, comp, enc, oenc, typ = FILE_EXTENT_HEAD.unpack_from(data, 0)
            if typ == INLINE:
                disk = (0, 0, 0, len(data) - FILE_EXTENT_HEAD.size)
            else:
                disk = FILE_EXTENT_DISK.unpack_from(data, FILE_EXTENT_HEAD.size)
            out.append(Extent(offset, typ, comp, enc, oenc, *disk, ram, gen))
        return out
    finally:
        os.close(fd)


def signature(path) -> tuple:
    """(size, extents): equal signatures mean the design would reuse content."""
    return (os.stat(path).st_size, tuple(extents(path)))


def bytenrs(path) -> list[int]:
    return [e.disk_bytenr for e in extents(path)]


def is_btrfs(path) -> bool:
    return _fs_magic(path) == BTRFS_SUPER_MAGIC


def _fs_magic(path) -> int:
    out = subprocess.run(["stat", "-f", "-c", "%t", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.strip(), 16)


def compression_enabled(path) -> bool:
    out = subprocess.run(["findmnt", "-n", "-o", "OPTIONS", "-T", str(path)], capture_output=True, text=True)
    return "compress" in out.stdout


def run(*args) -> str:
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True).stdout


def subvolume_create(path) -> None:
    run("btrfs", "subvolume", "create", path)


def snapshot_ro(source, target) -> None:
    run("btrfs", "subvolume", "snapshot", "-r", source, target)


def set_readonly(path, readonly: bool) -> None:
    run("btrfs", "property", "set", "-ts", path, "ro", "true" if readonly else "false")


def sync() -> None:
    os.sync()
