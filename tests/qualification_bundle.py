#!/usr/bin/env python3
"""Package a tested candidate, never publish a release or change a fleet pin."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib


def digest(path):
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=120, **kwargs).strip()


def validate_report(report, binary_sha256):
    if (type(report.get("schema_version")) is not int or report["schema_version"] != 1
            or report.get("ok") is not True or report.get("binary_sha256") != binary_sha256
            or report.get("immutable_snapshots") is not True
            or report.get("quota_bytes") != 4 * 1024**3
            or report.get("dense_probe_errno") != "EDQUOT"
            or report.get("byte_verification") != "union-of-extents"):
        raise ValueError("missing, failed or mismatched Btrfs qualification")
    allocation = report.get("allocated", {})
    for name, maximum in [("huge", 1024**2), ("mixed", 2 * 1024**2), ("linked", 2 * 1024**2)]:
        value = allocation.get(name)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("sparse allocation was not qualified")
    summaries = report.get("summaries", [])
    if len(summaries) != 3:
        raise ValueError("incremental qualification is missing")
    unchanged, changed = summaries[1:]
    count = unchanged.get("total_files_processed")
    if (type(count) is not int or count <= 0
            or unchanged.get("files_unmodified") != count
            or unchanged.get("data_added_files") != 0
            or changed.get("files_changed") != 1
            or changed.get("files_unmodified") != count - 1):
        raise ValueError("incremental parent reuse was not qualified")


def write_json(path, value):
    with path.open("x", encoding="utf8") as file:
        json.dump(value, file, indent=2, sort_keys=True)
        file.write("\n")


def archive_tree(source, output, epoch):
    with tarfile.open(output, "x:xz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(source.iterdir()):
            if not path.is_file() or path.is_symlink():
                raise ValueError("unexpected bundle member")
            info = archive.gettarinfo(path, arcname=path.name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = epoch
            info.mode = 0o755 if path.name == "rustic" else 0o644
            with path.open("rb") as file:
                archive.addfile(info, file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("qualification", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    command(["git", "diff", "--exit-code", "HEAD", "--"])
    cli_commit = command(["git", "rev-parse", "HEAD"])
    if os.environ.get("GITHUB_SHA", cli_commit) != cli_commit:
        raise ValueError("checkout does not match the workflow commit")
    epoch = int(command(["git", "show", "-s", "--format=%ct", "HEAD"]))
    binary = args.binary.resolve(strict=True)
    if not 0 < binary.stat().st_size <= 256 * 1024**2:
        raise ValueError("unexpected binary size")
    binary_sha256 = digest(binary)
    if args.qualification.stat().st_size > 1024**2:
        raise ValueError("qualification report exceeds limit")
    report = json.loads(args.qualification.read_text())
    validate_report(report, binary_sha256)
    toolchain = command(["rustc", "--version", "--verbose"])
    if not toolchain.startswith("rustc 1.94.0 "):
        raise ValueError("unqualified Rust toolchain")
    target = next(line[6:] for line in toolchain.splitlines() if line.startswith("host: "))
    if target not in {"x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"}:
        raise ValueError("unqualified build target")
    config = tomllib.loads((root / "Cargo.toml").read_text())
    core_commit = config["dependencies"]["rustic_core"]["rev"]
    metadata = json.loads(command(["cargo", "metadata", "--locked", "--format-version", "1",
                                   "--filter-platform", target]))
    core = [package for package in metadata["packages"] if package["name"] == "rustic_core"]
    if len(core) != 1 or core[0]["source"] != "git+https://github.com/sagemathinc/rustic_core.git?rev=" + core_commit + "#" + core_commit:
        raise ValueError("locked core does not match the reviewed pin")
    with tempfile.TemporaryDirectory(prefix="rustic-candidate-") as tmp:
        stage = Path(tmp)
        capabilities = json.loads(command([str(binary), "version", "--json"], env={
            "HOME": tmp, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        }))
        cap = capabilities.get("capabilities", {})
        if (capabilities.get("schema_version") != 1
                or any(cap.get(name) is not True for name in
                       ["strict_backup", "strict_restore", "sparse_required_restore", "hole_aware_backup"])
                or any(type(cap.get(name)) is not int or cap[name] != 1 for name in
                       ["backup_inventory", "backup_admission"])):
            raise ValueError("native capabilities are missing")
        versions = re.findall(r"GLIBC_([0-9]+(?:\.[0-9]+)+)", command(["readelf", "--version-info", "--wide", str(binary)]))
        if not versions:
            raise ValueError("GNU binary has no identified glibc requirement")
        dependencies = [{key: package.get(key) for key in ["name", "version", "source", "license", "checksum"]}
                        for package in metadata["packages"]]
        # This is an honest dependency inventory, not an asserted SPDX/SBOM
        # certification. Cargo.lock carries registry checksums and git revisions.
        write_json(stage / "dependency-inventory.json", {
            "schema_version": 1, "target": target, "packages": dependencies,
            "resolved_graph": metadata["resolve"],
        })
        shutil.copyfile(binary, stage / "rustic")
        if digest(stage / "rustic") != binary_sha256:
            raise ValueError("binary changed after qualification")
        for name in ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "LICENSE-MIT", "LICENSE-APACHE"]:
            shutil.copyfile(root / name, stage / name)
        shutil.copyfile(root / "tests" / "RECOVERY.md", stage / "RECOVERY.md")
        write_json(stage / "qualification.json", report)
        write_json(stage / "manifest.json", {
            "schema_version": 1, "candidate_only": True,
            "cli_repository": "https://github.com/sagemathinc/rustic", "cli_commit": cli_commit,
            "core_repository": "https://github.com/sagemathinc/rustic_core", "core_commit": core_commit,
            "binary_sha256": binary_sha256, "capabilities": capabilities,
            "target": target, "toolchain": toolchain, "profile": config["profile"]["release"],
            "glibc_symbol_minimum": max(versions, key=lambda value: tuple(map(int, value.split(".")))),
            "cargo_lock_sha256": digest(root / "Cargo.lock"),
            "qualified_os": "Ubuntu 24.04; Linux/Btrfs", "bit_reproducibility": "not yet established",
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
            "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        })
        (stage / "SHA256SUMS").write_text("".join(
            f"{digest(path)}  {path.name}\n" for path in sorted(stage.iterdir())))
        args.output.mkdir(parents=True, exist_ok=False)
        name = f"rustic-sparse-{cli_commit[:12]}-{target}.tar.xz"
        output = args.output / name
        archive_tree(stage, output, epoch)
        (args.output / (name + ".sha256")).write_text(f"{digest(output)}  {name}\n")
        print(output)


if __name__ == "__main__":
    main()
