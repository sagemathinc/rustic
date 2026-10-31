"""What a restore must reproduce, path by path, and how two trees differ.

The walker changes into each directory and works with short relative names,
so trees with paths longer than PATH_MAX can be described too.
"""
import hashlib
import os
import stat

BLOCK = 4096
KINDS = {
    stat.S_IFREG: "file",
    stat.S_IFDIR: "dir",
    stat.S_IFLNK: "symlink",
    stat.S_IFIFO: "fifo",
    stat.S_IFSOCK: "socket",
    stat.S_IFCHR: "chardev",
    stat.S_IFBLK: "blockdev",
}


def _decode(name: bytes) -> str:
    return name.decode("utf-8", "surrogateescape")


def holes(fd: int, size: int) -> list[tuple[int, int]]:
    """Hole ranges of an open file, from SEEK_HOLE/SEEK_DATA."""
    found = []
    pos = 0
    while pos < size:
        hole = os.lseek(fd, pos, os.SEEK_HOLE)
        if hole >= size:
            break
        try:
            data = os.lseek(fd, hole, os.SEEK_DATA)
        except OSError:  # ENXIO: the hole runs to the end of the file
            data = size
        found.append((hole, data))
        pos = data
    return found


def hole_blocks(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The whole BLOCK-aligned blocks inside each hole range."""
    out = []
    for start, end in ranges:
        start = -(-start // BLOCK) * BLOCK
        end = end // BLOCK * BLOCK
        if end > start:
            out.append((start, end))
    return out


def covered(inner: tuple[int, int], ranges: list[tuple[int, int]]) -> bool:
    """Whether the sorted, disjoint ranges cover all of inner."""
    start, end = inner
    for s, e in ranges:
        if s <= start < e:
            if end <= e:
                return True
            start = e
    return False


def entry(name: bytes) -> dict:
    """Describe one entry of the current directory."""
    info = os.lstat(name)
    kind = KINDS[stat.S_IFMT(info.st_mode)]
    out = {
        "kind": kind,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mtime_ns": info.st_mtime_ns,
    }
    if kind == "file":
        out["size"] = info.st_size
        out["nlink"] = info.st_nlink
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(os.dup(fd), "rb") as f:
                out["sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
            out["_holes"] = hole_blocks(holes(fd, info.st_size))
        finally:
            os.close(fd)
        if info.st_nlink > 1:
            out["_inode"] = info.st_ino
    elif kind == "symlink":
        out["target"] = _decode(os.readlink(name))
    elif kind in ("chardev", "blockdev"):
        out["rdev"] = [os.major(info.st_rdev), os.minor(info.st_rdev)]
    try:
        names = os.listxattr(name, follow_symlinks=False)
    except OSError:
        names = []
    out["xattrs"] = {n: os.getxattr(name, n, follow_symlinks=False).hex() for n in sorted(names)}
    return out


def manifest(root) -> dict:
    """Relative path ("." for the root itself) -> entry()."""
    out = {}
    home = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    top = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fchdir(top)
        out["."] = entry(b".")
        _walk(b"", top, out)
    finally:
        os.fchdir(home)
        os.close(home)
        os.close(top)
    return out


def _walk(prefix: bytes, here: int, out: dict) -> None:
    for name in sorted(os.listdir(b".")):
        rel = prefix + name
        e = entry(name)
        out[_decode(rel)] = e
        if e["kind"] == "dir":
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchdir(child)
                _walk(rel + b"/", child, out)
            finally:
                os.fchdir(here)
                os.close(child)


def hardlink_groups(m: dict) -> list[list[str]]:
    groups = {}
    for path, e in m.items():
        if "_inode" in e:
            groups.setdefault(e["_inode"], []).append(path)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


def compare(want: dict, got: dict, check_holes: bool = False) -> list[str]:
    """Differences in both directions; keys starting with _ get their own checks.

    check_holes is for sparse restores: every hole of the source must be a hole
    in the restore.  A dense restore writes zeros there, which is allowed.
    """
    problems = []
    for path in sorted(set(want) | set(got)):
        if path not in got:
            problems.append(f"missing: {path!r}")
            continue
        if path not in want:
            problems.append(f"extra: {path!r}")
            continue
        w, g = want[path], got[path]
        keys = ("kind",) if path == "." else sorted(set(w) | set(g))  # the root: see root_metadata
        diff = {k: (w.get(k), g.get(k)) for k in keys if not k.startswith("_") and w.get(k) != g.get(k)}
        if diff:
            problems.append(f"differs: {path!r} {diff}")
        # A restore may punch more holes than the source had (zero chunks),
        # but no hole of the source may come back allocated.
        for block in w.get("_holes", []) if check_holes else []:
            if not covered(block, g.get("_holes", [])):
                problems.append(f"hole allocated: {path!r} bytes {block[0]}..{block[1]}")
                break
    want_groups, got_groups = hardlink_groups(want), hardlink_groups(got)
    if want_groups != got_groups:
        problems.append(f"hardlink groups: {want_groups} != {got_groups}")
    return problems
