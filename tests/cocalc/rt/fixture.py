"""The source tree: one function per kind of entry a restore must reproduce.

Each case builds its entries under the given root.  A case that can't run here
raises Skip with the reason; the runner reports it, so nothing passes silently.
"""
import os
from pathlib import Path
import socket
import stat
import struct


class Skip(Exception):
    pass


CASES = []


def case(root_only=False):
    def register(fn):
        CASES.append((fn.__name__, root_only, fn))
        return fn

    return register


def _set_xattr(path, name, value, follow_symlinks=True):
    try:
        os.setxattr(path, name, value, follow_symlinks=follow_symlinks)
    except OSError as err:
        raise Skip(f"{name}: {err.strerror}")


def posix_acl(entries):
    """A system.posix_acl_* value: version 2, then (tag, perm, id) entries."""
    undefined = 0xFFFFFFFF
    out = struct.pack("<I", 2)
    for tag, perm, qualifier in entries:
        out += struct.pack("<HHI", tag, perm, undefined if qualifier is None else qualifier)
    return out


USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20


@case()
def basics(root: Path):
    (root / "dir" / "nested" / "deep").mkdir(parents=True)
    (root / "empty-dir").mkdir()
    (root / "file").write_bytes(b"hello\n")
    (root / "empty-file").write_bytes(b"")
    (root / "dir" / "random").write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    (root / "dir" / "nested" / "deep" / "zeros").write_bytes(bytes(1024 * 1024))


@case()
def sparse(root: Path):
    with open(root / "sparse", "wb") as f:  # a hole between two data regions
        f.write(b"start")
        f.seek(64 * 1024 * 1024)
        f.write(b"end")
    with open(root / "sparse-tail", "wb") as f:  # a hole running to the end
        f.write(b"head")
        f.truncate(16 * 1024 * 1024)
    with open(root / "sparse-lead", "wb") as f:  # a hole at the start
        f.seek(8 * 1024 * 1024)
        f.write(b"x" * 10000)
    with open(root / "all-hole", "wb") as f:
        f.truncate(32 * 1024 * 1024)


@case()
def names(root: Path):
    base = os.fsencode(root) + b"/"
    os.mkdir(base + b"non-utf8-\xff\xfe")
    (root / "non-utf8-\udcff\udcfe" / "inside").write_bytes(b"in")
    for name in (b"lone-continuation-\x80", b"bad-pair-\xc3\x28", b"\n", b"-leading-dash", b"..dots", b"back\\slash"):
        with open(base + name, "wb") as f:
            f.write(name)
    (root / ("long-" + "x" * 240)).write_bytes(b"long")
    (root / ("n" * 255)).write_bytes(b"255 bytes")
    (root / "with space\nand newline").write_bytes(b"odd")
    (root / "uni-é中\U0001f600").write_bytes(b"unicode")


@case()
def links(root: Path):
    (root / "link-dir-target").mkdir()
    (root / "link-dir-target" / "x").write_bytes(b"x")
    os.symlink("link-dir-target", root / "link-dir")
    os.symlink("plain", root / "link-rel")
    (root / "plain").write_bytes(b"plain")
    os.symlink("/nonexistent/target", root / "link-dangling-abs")
    os.symlink(b"target-\xff", os.fsencode(root / "link-non-utf8"))
    os.symlink("x" * 1000, root / "link-long-target")
    os.utime(root / "link-rel", ns=(1_500_000_000_000_000_001, 1_500_000_000_999_999_999), follow_symlinks=False)


@case()
def hardlinks(root: Path):
    (root / "hl").mkdir()
    (root / "hl" / "a" / "b" / "c").mkdir(parents=True)
    (root / "hl" / "three").write_bytes(b"three links\n")
    os.link(root / "hl" / "three", root / "hl" / "a" / "three-2")
    os.link(root / "hl" / "three", root / "hl" / "a" / "b" / "c" / "three-3")
    (root / "hl" / "empty").write_bytes(b"")
    os.link(root / "hl" / "empty", root / "hl" / "empty-2")
    with open(root / "hl" / "sparse", "wb") as f:
        f.seek(4 * 1024 * 1024)
        f.write(b"tail")
    os.link(root / "hl" / "sparse", root / "hl" / "a" / "sparse-2")
    os.utime(root / "hl" / "three", ns=(1_600_000_000_123_456_789, 1_600_000_000_123_456_789))


@case()
def modes(root: Path):
    (root / "exec").write_bytes(b"#!/bin/sh\n")
    os.chmod(root / "exec", 0o755)
    (root / "private").write_bytes(b"secret")
    os.chmod(root / "private", 0o600)
    (root / "setgid-dir").mkdir()
    os.chmod(root / "setgid-dir", 0o2775)
    (root / "sticky-dir").mkdir()
    os.chmod(root / "sticky-dir", 0o1777)
    (root / "read-only-dir").mkdir()
    (root / "read-only-dir" / "kept").write_bytes(b"kept")
    os.chmod(root / "read-only-dir", 0o555)


@case(root_only=True)
def unreadable(root: Path):
    """Mode 000 entries: only root can back them up."""
    (root / "mode-000").write_bytes(b"locked")
    os.chmod(root / "mode-000", 0)
    (root / "dir-000").mkdir()
    (root / "dir-000" / "inside").write_bytes(b"inside a mode-000 dir")
    os.chmod(root / "dir-000", 0)


@case()
def times(root: Path):
    for name, ns in (
        ("before-1970", -86_400 * 365 * 10 * 10**9 + 7),
        ("after-2038", 2_200_000_000 * 10**9 + 999_999_999),
        ("zero-ns", 1_700_000_000 * 10**9),
        ("max-ns", 1_700_000_000 * 10**9 + 999_999_999),
    ):
        (root / name).write_bytes(name.encode())
        os.utime(root / name, ns=(ns, ns))


@case()
def special(root: Path):
    os.mkfifo(root / "fifo")
    here = os.getcwd()
    os.chdir(root)  # socket paths are limited to about 108 bytes
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind("socket")
        sock.close()
    finally:
        os.chdir(here)


@case(root_only=True)
def devices(root: Path):
    os.mknod(root / "chardev", stat.S_IFCHR | 0o600, os.makedev(1, 3))
    os.mknod(root / "blockdev", stat.S_IFBLK | 0o600, os.makedev(7, 200))
    os.mknod(root / "whiteout", stat.S_IFCHR | 0o000, os.makedev(0, 0))


@case(root_only=True)
def owners(root: Path):
    (root / "owned").write_bytes(b"owned")
    os.chown(root / "owned", 1234, 5678)
    os.symlink("owned", root / "owned-link")
    os.lchown(root / "owned-link", 4321, 8765)
    (root / "group-only").write_bytes(b"group")
    os.chown(root / "group-only", 0, 4242)
    (root / "owned-dir").mkdir()
    (root / "owned-dir" / "child").write_bytes(b"child")
    os.chown(root / "owned-dir", 1001, 1001)
    # chown clears setuid and setgid on files, so a restore must chown first
    (root / "setuid").write_bytes(b"#!/bin/sh\n")
    os.chown(root / "setuid", 1234, 1234)
    os.chmod(root / "setuid", 0o6755)


@case(root_only=True)
def big_ids(root: Path):
    (root / "big-ids").write_bytes(b"big")
    try:
        os.chown(root / "big-ids", 2**31 - 2, 70_000)
    except OSError as err:  # ids outside a user namespace's mapping
        os.unlink(root / "big-ids")
        raise Skip(f"chown to large ids: {err.strerror}")


@case()
def user_xattrs(root: Path):
    (root / "xattr").write_bytes(b"x")
    _set_xattr(root / "xattr", "user.cocalc", b"value")
    _set_xattr(root / "xattr", "user.empty", b"")
    _set_xattr(root / "xattr", "user.binary", bytes(range(256)))
    _set_xattr(root / "xattr", "user.large", os.urandom(3000))
    (root / "xattr-dir").mkdir()
    _set_xattr(root / "xattr-dir", "user.on-dir", b"dir")


@case()
def acls(root: Path):
    (root / "acl").write_bytes(b"acl")
    _set_xattr(
        root / "acl",
        "system.posix_acl_access",
        posix_acl([(USER_OBJ, 6, None), (USER, 4, 4242), (GROUP_OBJ, 4, None), (MASK, 4, None), (OTHER, 0, None)]),
    )
    (root / "acl-dir").mkdir()
    _set_xattr(
        root / "acl-dir",
        "system.posix_acl_default",
        posix_acl([(USER_OBJ, 7, None), (GROUP_OBJ, 5, None), (GROUP, 5, 4343), (MASK, 5, None), (OTHER, 0, None)]),
    )


@case(root_only=True)
def root_xattrs(root: Path):
    (root / "capability").write_bytes(b"#!/bin/sh\n")
    os.chmod(root / "capability", 0o755)
    # vfs_cap_data v2, effective, permitted = CAP_NET_BIND_SERVICE (bit 10)
    _set_xattr(root / "capability", "security.capability", struct.pack("<IIIII", 0x02000001, 1 << 10, 0, 0, 0))
    _set_xattr(root / "capability", "trusted.cocalc", b"trusted")


@case()
def huge_directory(root: Path):
    big = root / "huge"
    big.mkdir()
    for i in range(20_000):
        (big / f"f{i:05d}").write_bytes(b"" if i % 2 else str(i).encode())


@case()
def directory_times(root: Path):
    """Set last, so creating the other entries can't touch them."""
    # lstat only: following the 1000-byte symlink target raises ENAMETOOLONG
    # on Python 3.12
    dirs = sorted(
        Path(top) / name
        for top, names, _ in os.walk(root)
        for name in names
        if stat.S_ISDIR(os.lstat(Path(top) / name).st_mode)
    )
    for i, path in enumerate([*dirs, root]):
        if os.access(path, os.W_OK) or os.geteuid() == 0:
            ns = 1_400_000_000_000_000_000 + i * 1_000_000_007
            os.utime(path, ns=(ns, ns), follow_symlinks=False)


def build(root: Path, as_root: bool) -> list[str]:
    """Build every case; return the skips, each with its reason."""
    skipped = []
    for name, root_only, fn in CASES:
        if root_only and not as_root:
            skipped.append(f"{name}: needs root")
            continue
        try:
            fn(root)
        except Skip as why:
            skipped.append(f"{name}: {why}")
        except PermissionError as err:  # e.g. root inside a container without CAP_MKNOD
            if not root_only:
                raise
            skipped.append(f"{name}: {err.strerror} ({err.filename})")
    return skipped
