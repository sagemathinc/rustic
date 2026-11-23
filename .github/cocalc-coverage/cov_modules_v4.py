#!/usr/bin/env python3
"""Per-crate and per-module coverage table from a `cargo llvm-cov --json` export.

Usage: cov_modules_v4.py EXPORT.json REPO_ROOT > modules.md

Coverage kinds: lines, regions, functions (LLVM's).  No branch coverage.

Rows are (crate, module).  The crate is found from the path relative to REPO_ROOT:
  crates/<c>/...    -> crate <c>
  examples/<e>/...  -> crate examples/<e>
  anything else     -> crate (root)   (the repository's own top-level package)
Within a crate, the rest of the path R gives the module:
  src/<m>/...  or src/<m>.rs -> <m> (or <m>.rs)    e.g. commands, lib.rs
  src                        -> src                (a file named src, kept visible)
  <d>/<x>/...  (d not src)   -> <d>/<x>            e.g. tests/integration, tests/a.rs
  <f>          (one part)    -> <f>                e.g. build.rs
Every file under REPO_ROOT lands in exactly one row; files outside it are counted
and printed, never placed in a row.

Fails closed: any unexpected input or shape, or totals that don't add up,
prints one line to stderr and exits 2 with NOTHING on stdout.

Self-check: (sum over files inside the root) + (sum over files outside it)
must equal the export's own data[0].totals for every kind.

Paths must be canonical absolute POSIX spellings (REPO_ROOT too): no "." or ".."
components, no repeated or trailing "/", no leading "//".  That makes "inside the
root" and "duplicate file" plain string facts, so a spelling like /repo/../x.rs can
neither be counted inside the root nor hide a duplicate.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

KINDS = ("lines", "regions", "functions")


class Bad(Exception):
    pass


def count(v, what):
    # bool is an int subclass: reject it explicitly
    if type(v) is not int or v < 0:
        raise Bad(f"{what} is not a non-negative integer")
    return v


def summary(obj, what):
    if not isinstance(obj, dict):
        raise Bad(f"{what} is not an object")
    out = {}
    for k in KINDS:
        s = obj.get(k)
        if not isinstance(s, dict):
            raise Bad(f"{what}.{k} missing")
        n, c = count(s.get("count"), f"{what}.{k}.count"), count(s.get("covered"), f"{what}.{k}.covered")
        if c > n:
            raise Bad(f"{what}.{k}: covered > count")
        out[k] = (c, n)
    return out


def canonical(name: str, what: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not path.is_absolute() or name.startswith("//") or str(path) != name
            or any(part in (".", "..") for part in path.parts)):
        raise Bad(f"{what}: not a canonical absolute path")
    return path


def key(rel: PurePosixPath) -> tuple[str, str]:
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "crates":
        crate, rest = parts[1], parts[2:]
    elif len(parts) >= 3 and parts[0] == "examples":
        crate, rest = f"examples/{parts[1]}", parts[2:]
    else:
        crate, rest = "(root)", parts
    if rest[0] == "src" and len(rest) >= 2:
        module = rest[1]
    elif len(rest) == 1:
        module = rest[0]
    else:
        module = f"{rest[0]}/{rest[1]}"
    return crate, module


def build(data, root: PurePosixPath):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list) or len(data["data"]) != 1:
        raise Bad("export must have exactly one data[] entry")
    entry = data["data"][0]
    if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
        raise Bad("data[0].files missing")
    totals = summary(entry.get("totals"), "data[0].totals")
    mods = defaultdict(lambda: {k: [0, 0] for k in KINDS})
    crates = defaultdict(lambda: {k: [0, 0] for k in KINDS})
    seen = {k: [0, 0] for k in KINDS}
    outside, names = 0, set()
    for i, f in enumerate(entry["files"]):
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str) or not f["filename"]:
            raise Bad(f"files[{i}].filename missing")
        name = f["filename"]
        path = canonical(name, f"files[{i}].filename")
        if name in names:
            raise Bad(f"files[{i}]: duplicate filename")
        names.add(name)
        s = summary(f.get("summary"), f"files[{i}].summary")
        for k in KINDS:
            seen[k][0] += s[k][0]; seen[k][1] += s[k][1]
        try:
            rel = path.relative_to(root)
        except ValueError:
            outside += 1
            continue
        if not rel.parts:
            raise Bad(f"files[{i}] is the root itself")
        crate, module = key(rel)
        for k in KINDS:
            for t in (mods[(crate, module)], crates[crate]):
                t[k][0] += s[k][0]; t[k][1] += s[k][1]
    for k in KINDS:
        if tuple(seen[k]) != totals[k]:
            raise Bad(f"{k}: files sum {tuple(seen[k])} != data[0].totals {totals[k]}")
    if not mods:
        raise Bad("no files under the repo root")
    return crates, mods, outside, totals


def render(crates, mods, outside, totals, root) -> str:
    def pct(c, n):
        return f"{100 * c / n:.1f}% ({c}/{n})" if n else "n/a"
    out = [f"export totals (all files): " + ", ".join(f"{k} {pct(*totals[k])}" for k in KINDS),
           f"files outside {root}: {outside} (counted in the totals, not in the tables)", "",
           "| crate | lines | regions | functions |", "|---|---|---|---|"]
    out += [f"| {c} | " + " | ".join(pct(*crates[c][k]) for k in KINDS) + " |" for c in sorted(crates)]
    out += ["", "| crate | module | lines | regions | functions |", "|---|---|---|---|---|"]
    out += [f"| {c} | {m} | " + " | ".join(pct(*mods[(c, m)][k]) for k in KINDS) + " |" for (c, m) in sorted(mods)]
    return "\n".join(out) + "\n"


def main(argv) -> int:
    try:
        if len(argv) != 3:
            raise Bad("usage: cov_modules_v4.py EXPORT.json REPO_ROOT")
        root = canonical(argv[2], "REPO_ROOT")
        try:
            data = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise Bad(f"cannot read export: {type(e).__name__}")
        text = render(*build(data, root), root)
    except Bad as e:
        print(f"cov_modules: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
