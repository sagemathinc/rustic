#!/usr/bin/env python3
"""Corruption injection: damage one file of a repository in one way, on a
fresh copy of a pristine repository each time, then require:
  - `check --read-data` exits non-zero (the damage is detected), unless the
    kind is listed in NOT_DETECTED with a reason;
  - no `restore` of any snapshot exits 0 with wrong or missing content: each
    restore is either byte-exact or fails loudly (non-zero exit).
Negative controls: the uncorrupted copy, and a "flip the same byte twice"
no-op, must check clean and restore exactly.

Usage: corrupt.py RUSTIC WORK_DIR [--seed S] [--only KIND,...]

Picks (which pack, which offset) are random but reproducible from the seed.
Exit 1 on any problem.  Needs no privileges.
"""
import argparse
import json
import os
from pathlib import Path
import random
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crash import Repo, grow  # noqa: E402
from rt.manifest import compare, manifest  # noqa: E402


class CachedRepo(Repo):
    """A Repo with its own cache directory, passed explicitly with --cache-dir."""
    def __init__(self, binary: str, path: Path, cache: Path):
        super().__init__(binary, path)
        self.cache = cache

    def argv(self, *args):
        return [self.binary, "--repository", str(self.path), "--cache-dir", str(self.cache),
                "--no-progress", *map(str, args)]


def pack_types(repo: Repo) -> dict[str, str]:
    """pack id -> "tree" or "data", from every index file."""
    types = {}
    out = repo.run("list", "index").stdout.decode()
    for ix in (w for line in out.split() for w in [line.strip("IndexId()")] if len(w) == 64):
        raw = repo.run("cat", "index", ix).stdout.decode()
        doc = json.loads(raw[raw.index("{"):])
        for pack in doc.get("packs") or []:
            kinds = {b["type"] for b in pack.get("blobs") or []}
            types[pack["id"]] = "tree" if kinds == {"tree"} else "data"
    return types


def files(repo: Repo, sub: str) -> list[Path]:
    return sorted(p for p in (repo.path / sub).rglob("*") if p.is_file())


def pack_file(repo: Repo, pack_id: str) -> Path:
    return repo.path / "data" / pack_id[:2] / pack_id


def flip(path: Path, offset: int) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0x5A]))


def zero(path: Path, offset: int, n: int) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(bytes(n))


def truncate(path: Path, size: int) -> None:
    with open(path, "r+b") as f:
        f.truncate(size)


def middle(rng, path: Path) -> int:
    size = path.stat().st_size
    return rng.randrange(size // 4, max(size // 4 + 1, 3 * size // 4))


def pick_pack(rng, repo, types, kind):
    candidates = sorted(p for p, t in types.items() if t == kind)
    return pack_file(repo, rng.choice(candidates))


# kind -> function(rng, repo, types) applying the damage; returns a description
KINDS = {
    "none": lambda rng, r, t: "no corruption (negative control)",
    "flip_twice": lambda rng, r, t: _flip_twice(rng, r, t),
    "data_flip": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "data"), "flip"),
    "data_zero_4k": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "data"), "zero"),
    "data_header_flip": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "data"), "tail"),
    "data_truncate": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "data"), "half"),
    "data_delete": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "data"), "delete"),
    "tree_flip": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "tree"), "flip"),
    "tree_delete": lambda rng, r, t: _apply(rng, pick_pack(rng, r, t, "tree"), "delete"),
    "index_flip": lambda rng, r, t: _apply(rng, rng.choice(files(r, "index")), "flip"),
    "index_delete": lambda rng, r, t: _apply(rng, rng.choice(files(r, "index")), "delete"),
    "snapshot_flip": lambda rng, r, t: _apply(rng, rng.choice(files(r, "snapshots")), "flip"),
    "snapshot_truncate": lambda rng, r, t: _apply(rng, rng.choice(files(r, "snapshots")), "empty"),
}


def _apply(rng, path: Path, how: str) -> str:
    size = path.stat().st_size
    if how == "flip":
        off = middle(rng, path)
        flip(path, off)
        return f"flipped byte {off} of {size} in {path.parent.name}/{path.name[:8]}"
    if how == "zero":
        off = max(0, middle(rng, path) - 2048)
        zero(path, off, min(4096, size - off))
        return f"zeroed 4 KiB at {off} of {size} in {path.parent.name}/{path.name[:8]}"
    if how == "tail":
        off = size - rng.randrange(5, min(64, size))
        flip(path, off)
        return f"flipped header byte {off} of {size} in {path.parent.name}/{path.name[:8]}"
    if how == "half":
        truncate(path, size // 2)
        return f"truncated {path.parent.name}/{path.name[:8]} from {size} to {size // 2}"
    if how == "empty":
        truncate(path, 0)
        return f"truncated {path.parent.name}/{path.name[:8]} to 0"
    if how == "delete":
        path.unlink()
        return f"deleted {path.parent.name}/{path.name[:8]} ({size} bytes)"
    raise ValueError(how)


def _flip_twice(rng, repo, types) -> str:
    path = pick_pack(rng, repo, types, "data")
    off = middle(rng, path)
    flip(path, off)
    flip(path, off)
    return f"flipped byte {off} of {path.name[:8]} twice (a no-op: negative control)"


# Kinds whose damage check is allowed not to report, with the reason.  Empty
# until measured: anything listed here must come with a reason.
NOT_DETECTED: dict[str, str] = {}
CLEAN = {"none", "flip_twice"}  # must check clean and restore exactly


def run_kind(kind, cache_mode, binary, pristine: Repo, work: Path, expected: dict, rng) -> dict:
    """cache_mode "cold": an empty cache (a host restoring after a disaster);
    "warm": a copy of the cache the pristine repository's own backups left (the
    backup host itself).  Detection must not depend on it."""
    case = work / f"{kind}-{cache_mode}"
    shutil.rmtree(case, ignore_errors=True)
    shutil.copytree(pristine.path, case / "repo")
    if cache_mode == "warm":
        shutil.copytree(pristine.cache, case / "cache")
    repo = CachedRepo(binary, case / "repo", case / "cache")
    # pick the target with a throwaway cache, so picking leaves the case's cache as it was
    picker = CachedRepo(binary, case / "repo", case / "pick-cache")
    what = KINDS[kind](rng, repo, pack_types(picker) if kind != "none" else {})
    shutil.rmtree(case / "pick-cache", ignore_errors=True)
    result = {"damage": what, "problems": []}
    checked = repo.run("check", "--read-data", check=False)
    result["check_rc"] = checked.returncode
    detected = checked.returncode != 0
    if kind in CLEAN and detected:
        result["problems"].append(f"check failed on an undamaged copy: {checked.stderr.decode(errors='replace')[-400:]}")
    if kind not in CLEAN and not detected and kind not in NOT_DETECTED:
        errors = [l for l in checked.stderr.decode(errors="replace").splitlines() if "ERROR" in l]
        result["problems"].append(f"check --read-data exited 0 ({len(errors)} ERROR lines): {errors[:2]}")
    outcomes = {}
    for snap, want in expected.items():
        target = case / "restore"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        restored = repo.run("restore", "--numeric-id", snap, target, check=False)
        if restored.returncode:
            outcomes[snap[:8]] = "failed"
        else:
            diff = compare(want, manifest(target))
            outcomes[snap[:8]] = "exact" if not diff else "WRONG"
            if diff:
                result["problems"].append(f"restore of {snap[:8]} exited 0 but differs: {diff[:3]}")
        shutil.rmtree(target, ignore_errors=True)
    result["restores"] = outcomes
    if kind in CLEAN and any(v != "exact" for v in outcomes.values()):
        result["problems"].append(f"an undamaged copy did not restore exactly: {outcomes}")
    shutil.rmtree(case, ignore_errors=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("work")
    ap.add_argument("--seed", type=int, default=int(os.environ.get("CORRUPT_SEED", str(time.time_ns() % 2**32))))
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    work = Path(args.work).resolve(strict=True)
    binary = str(Path(args.binary).resolve(strict=True))
    kinds = [k for k in KINDS if not args.only or k in args.only.split(",")]
    print(f"corrupt: seed {args.seed}, kinds {','.join(kinds)}", flush=True)
    t0 = time.monotonic()

    # the pristine repository: three snapshots of a changing tree
    pristine = CachedRepo(binary, work / "pristine", work / "pristine-cache")
    pristine.run("init")
    source = work / "source"
    expected = {}
    for files_, size in ((120, 512 * 1024), (40, 1024 * 1024), (40, 256 * 1024)):
        grow(source, rng, files_, size)
        expected[pristine.backup(source)] = manifest(source)
    pristine.run("check", "--read-data")

    report = {"seed": args.seed, "kinds": {}}
    problems = []
    for kind in kinds:
        for cache_mode in ("cold", "warm"):
            # the same pick in both modes
            result = run_kind(kind, cache_mode, binary, pristine, work, expected,
                              random.Random(f"{args.seed}-{kind}"))
            name = f"{kind}/{cache_mode}"
            report["kinds"][name] = result
            problems += [f"{name}: {p}" for p in result["problems"]]
            print(f"{name:23s} check_rc={result['check_rc']} restores={result['restores']} "
                  f"{'PROBLEM' if result['problems'] else 'ok'}  ({result['damage']})", flush=True)
    report["seconds"] = round(time.monotonic() - t0, 1)
    report["problems"] = problems
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
