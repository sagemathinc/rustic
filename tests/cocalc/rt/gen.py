"""Seeded random trees for round trips, and a shrinker for failing ones.

generate(seed) returns a spec: a JSON-able dict whose entries build a tree with
build(spec, root).  The same seed always gives the same spec and the same tree,
so a failing seed is replayed from its printed spec.  shrink() deletes entries
(largest subtree first) while a failure keeps its class, and returns the
smallest spec it found.

Bounds per spec: at most MAX_ENTRIES entries, MAX_BYTES logical file bytes and
MAX_DEPTH directory levels.  No entry is ever named `.snapshots` (CoCalc's
backup excludes that name), and no entry is unreadable or deeper than PATH_MAX,
so a backup never skips anything.
"""
import hashlib
import os
from pathlib import Path
import random
import shutil
import subprocess

MAX_ENTRIES = 1500
MAX_BYTES = 48 * 1024 * 1024
MAX_DEPTH = 12
MIB = 1024 * 1024

SHAPES = (
    "dir",
    "empty-dir",
    "file-dense",
    "file-zeros",
    "file-sparse-mid",
    "file-sparse-tail",
    "file-sparse-lead",
    "file-empty",
    "symlink",
    "hardlink",
    "reflink",
    "xattr",
)
NAME_PARTS = ("a", "data", "Ünïcode-é", "with space", "-dash", "..dots", "n" * 60, "x\udcff\udcfe", "back\\slash")
FILE_MODES = (0o644, 0o600, 0o755, 0o640, 0o4755)
DIR_MODES = (0o755, 0o700, 0o2775, 0o1777)
T0 = 1_500_000_000_000_000_000


def _name(rng, i: int) -> str:
    return f"{rng.choice(NAME_PARTS)}-{i}"


def generate(seed) -> dict:
    rng = random.Random(f"rt-gen-v1:{seed}")
    target = rng.choice((10, 40, 150, 600, MAX_ENTRIES))
    dirs = [("", 0)]  # (path, depth); "" is the root
    files: list[str] = []
    sizes: dict = {}  # path -> logical size, for files and reflinks
    entries: list[dict] = []
    budget = MAX_BYTES
    i = 0
    while len(entries) < target:
        i += 1
        shape = rng.choice(SHAPES)
        parent, depth = rng.choice(dirs)
        path = (parent + "/" if parent else "") + _name(rng, i)
        mtime = T0 + rng.randrange(10**18)
        if shape in ("dir", "empty-dir"):
            if depth + 1 > MAX_DEPTH:
                continue
            entries.append({"path": path, "kind": "dir", "mode": rng.choice(DIR_MODES), "mtime_ns": mtime})
            if shape == "dir":
                dirs.append((path, depth + 1))
        elif shape.startswith("file-"):
            layout = shape[5:]
            size = 0 if layout == "empty" else min(budget, int(2 ** rng.uniform(0, 23)))
            if layout.startswith("sparse"):
                size = rng.choice((4, 16, 32)) * MIB
                if size > budget:  # out of bytes: an empty file instead
                    layout, size = "empty", 0
            budget -= size
            entries.append({"path": path, "kind": "file", "layout": layout, "size": size, "seed": rng.randrange(2**32),
                            "mode": rng.choice(FILE_MODES), "mtime_ns": mtime})
            files.append(path)
            sizes[path] = size
        elif shape == "symlink":
            target_path = rng.choice(files + ["/nonexistent/target", "x" * 300]) if files else "dangling"
            entries.append({"path": path, "kind": "symlink", "target": target_path, "mtime_ns": mtime})
        elif shape in ("hardlink", "reflink") and files:
            source = rng.choice(files)
            if shape == "reflink":
                size = sizes[source]
                if size > budget:
                    continue
                budget -= size
                entries.append({"path": path, "kind": "reflink", "source": source, "size": size, "mtime_ns": mtime})
                files.append(path)
                sizes[path] = size
            else:
                entries.append({"path": path, "kind": "hardlink", "target": source})
        elif shape == "xattr" and files:
            entries.append({"path": rng.choice(files), "kind": "xattr", "name": f"user.gen{i}",
                            "value": rng.randbytes(rng.choice((0, 1, 100, 2000))).hex()})
    return {"version": 1, "seed": str(seed), "entries": entries}


def _content(e: dict) -> bytes:
    """Deterministic bytes for a dense file entry."""
    out, block = bytearray(), hashlib.sha256(str(e["seed"]).encode()).digest()
    while len(out) < e["size"]:
        block = hashlib.sha256(block).digest()
        out += block * 64
    return bytes(out[: e["size"]])


def _write_file(path: Path, e: dict) -> None:
    size, layout = e["size"], e["layout"]
    with open(path, "wb") as f:
        if layout in ("dense", "empty"):
            f.write(_content(e))
        elif layout == "zeros":
            f.write(bytes(size))
        elif layout == "sparse-mid":
            f.write(b"start")
            f.seek(size - 3)
            f.write(b"end")
        elif layout == "sparse-tail":
            f.write(b"head")
            f.truncate(size)
        elif layout == "sparse-lead":
            f.seek(size // 2)
            f.write(b"x" * min(10000, size - size // 2))
            f.truncate(size)


def build(spec: dict, root: Path, reflink: bool = True) -> list[str]:
    """Create the tree under root; return what was built differently (reflink/xattr fallbacks)."""
    fallbacks = []
    dir_meta = []
    for e in spec["entries"]:
        path = root / e["path"]
        kind = e["kind"]
        if kind == "dir":
            path.mkdir()
            dir_meta.append((path, e))
        elif kind == "file":
            _write_file(path, e)
            os.chmod(path, e["mode"])
            os.utime(path, ns=(e["mtime_ns"], e["mtime_ns"]))
        elif kind == "symlink":
            os.symlink(e["target"], path)
            os.utime(path, ns=(e["mtime_ns"], e["mtime_ns"]), follow_symlinks=False)
        elif kind == "hardlink":
            os.link(root / e["target"], path)
        elif kind == "reflink":
            done = reflink and subprocess.run(["cp", "--reflink=always", "--preserve=mode", str(root / e["source"]), str(path)],
                                              capture_output=True).returncode == 0
            if not done:
                shutil.copyfile(root / e["source"], path)
                shutil.copymode(root / e["source"], path)
                fallbacks.append(f"reflink->copy: {e['path']}")
            os.utime(path, ns=(e["mtime_ns"], e["mtime_ns"]))
        elif kind == "xattr":
            try:
                os.setxattr(path, e["name"], bytes.fromhex(e["value"]), follow_symlinks=False)
            except OSError as err:
                fallbacks.append(f"xattr unsupported ({err.strerror}): {e['path']}")
        else:
            raise ValueError(f"unknown entry kind {kind!r}")
    for path, e in sorted(dir_meta, key=lambda pe: -len(pe[0].parts)):  # deepest first, after all content
        os.chmod(path, e["mode"])
        os.utime(path, ns=(e["mtime_ns"], e["mtime_ns"]))
    return fallbacks


def stats(spec: dict) -> dict:
    entries = spec["entries"]
    return {
        "entries": len(entries),
        "bytes": sum(e["size"] for e in entries if e["kind"] in ("file", "reflink")),
        "depth": max((e["path"].count("/") + 1 for e in entries if e["kind"] == "dir"), default=0),
    }


def shapes(spec: dict) -> set:
    out = set()
    for e in spec["entries"]:
        if e["kind"] == "file":
            out.add("file-" + e["layout"])
        elif e["kind"] == "dir":
            has_child = any(f["path"].startswith(e["path"] + "/") for f in spec["entries"])
            out.add("dir" if has_child else "empty-dir")
        else:
            out.add(e["kind"])
    return out


def _dependents(spec: dict) -> dict:
    """path -> the entries that need it: children, hard links, reflinks and xattrs."""
    out: dict = {}
    for e in spec["entries"]:
        needs = []
        if "/" in e["path"]:
            needs.append(e["path"].rsplit("/", 1)[0])
        if e["kind"] == "hardlink":
            needs.append(e["target"])
        elif e["kind"] == "reflink":
            needs.append(e["source"])
        elif e["kind"] == "xattr":
            needs.append(e["path"])
        for n in needs:
            out.setdefault(n, []).append(e)
    return out


def _removal(spec: dict, start: dict, deps: dict | None = None) -> set:
    """The ids of the entry plus everything that depends on it, transitively."""
    deps = _dependents(spec) if deps is None else deps
    gone, todo = {id(start)}, [start]
    while todo:
        e = todo.pop()
        if e["kind"] == "xattr":
            continue  # nothing depends on an xattr
        for d in deps.get(e["path"], ()):
            if id(d) not in gone:
                gone.add(id(d))
                todo.append(d)
    return gone


def problem_class(problems: list[str]):
    """The kind of the first problem ("differs", "missing", "hole allocated", ...), or None."""
    if not problems:
        return None
    return problems[0].split(":", 1)[0]


def shrink(spec: dict, fails, max_runs: int = 60) -> tuple[dict, int]:
    """Delete entries while fails(spec) keeps returning the same class; (smallest spec, runs used)."""
    want = fails(spec)
    if want is None:
        raise ValueError("shrink() needs a failing spec")
    runs = 1
    improved = True
    while improved and runs < max_runs:
        improved = False
        deps = _dependents(spec)
        removals = [(e, _removal(spec, e, deps)) for e in spec["entries"]]
        removals.sort(key=lambda eg: -len(eg[1]))  # largest subtree first; stable for ties
        for e, gone in removals:
            if runs >= max_runs:
                break
            smaller = {**spec, "entries": [f for f in spec["entries"] if id(f) not in gone]}
            runs += 1
            if fails(smaller) == want:
                spec, improved = smaller, True
                break
    return spec, runs
