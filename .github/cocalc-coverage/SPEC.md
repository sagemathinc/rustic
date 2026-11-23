# Coverage measurement (cocalc fork, CLI)

This file defines how `cocalc-coverage.yml` measures coverage, so that
measurements before and after a change are comparable. Any change to the
definition below starts a new series; numbers from different versions of this
file are not compared.

## What is measured

- LLVM **line, region and function** coverage of **this crate only** (the rustic
  CLI). Regions are not branches; there is no branch-coverage claim.
- `rustic_core` and `rustic_backend` are git dependencies and are **not**
  measured here; their coverage is measured in the rustic_core fork.
- Run **B**: the measured command is

  ```
  cargo llvm-cov --locked --no-fail-fast --json --output-path B.json
  ```

  with default features. No test filter, no `--ignored`, no `--doctests`, no
  `--release`.
- Run **C**: the CLI code that the end-to-end suites in `tests/cocalc` reach, at
  the same commit as B.
  - The binary is built in the debug profile with the instrumentation that
    `cargo llvm-cov show-env` sets.
  - roundtrip, b3_oracle, crash, corrupt and b6_adversary run as root on
    loopback btrfs, btrfs-zstd and ext4 in turn, with `LLVM_PROFILE_FILE` passed
    explicitly through `sudo env`.
  - Every suite on every filesystem must exit 0 and leave at least one new
    non-empty `.profraw` file.
  - The profile reset, the hand-back of root-written profiles, and each
    filesystem's creation, mount, unmount and cleanup are recorded and must
    succeed.
- The repository's committed profiles are used as is, with `CARGO_INCREMENTAL=0`
  and `RUST_BACKTRACE=0`.
- From the same profile data, each run writes the JSON export, `--lcov` and
  `--summary-only` reports. The report commands take no build options.

## Tools

- Runner `ubuntu-24.04` (x86_64).
- The toolchain this repository builds and releases with: its
  `rust-toolchain.toml` pins `1.94.0`. The job installs it with
  `llvm-tools-preview` and does not override the pin. The run is valid only if
  `rustc -vV` reports exactly `release: 1.94.0`, so a change of the pin shows up
  as a failed gate until this file and the workflow follow it.
- `cargo install cargo-llvm-cov --version 0.9.2 --locked`, built in the job; the
  job checks that the version line is exactly `cargo-llvm-cov 0.9.2`.
- Actions are pinned by commit sha in the workflow. There is no build cache:
  every run builds from scratch.

## The measured commit

`INTENDED_SHA` is the pull request's head commit (not GitHub's merge ref), or
the pushed/dispatched commit for other events. The job checks it out, records
`git rev-parse HEAD`, and the run is DIAGNOSTIC unless the two are equal. The
rustic_core revision comes from `Cargo.lock` and is recorded.

## Verdict

A run is a **CANDIDATE** when all of these hold:

- every setup step after checkout succeeded: the tool install, the self-tests of
  these helpers, the changed-path listing, the rustic_core lock entry and, for
  C, btrfs-progs. No such step aborts the job; each records its exit status;
- HEAD is the intended commit;
- rustc is the declared release, and the cargo-llvm-cov version line is exact;
- every selected test passed (B), or the build succeeded and every suite passed
  with live profiles (C);
- Cargo.lock is unchanged;
- the raw profiles were cleaned;
- the reports were written;
- the totals check out: (a) per kind, the files inside and outside the
  repository add up to the export's totals; (b) the export's totals equal the
  summary's TOTAL row; (c) the export is this run's own.

Otherwise the run is **DIAGNOSTIC**, and the job fails after uploading every
output.

A CANDIDATE becomes a reference measurement only after a separate review of its
artifacts; the job never claims that by itself.

## Recorded (`meta.json`, `record.env`)

- uname; host triple; resolved paths and sha256 of cargo, rustc, cargo-llvm-cov,
  llvm-profdata and llvm-cov;
- tool versions, the declared rustc release; this file's sha256;
- the intended and checked-out commits, the PR base, the changed paths, and the
  rustic_core revision;
- Cargo.lock sha256 before and after;
- start/end time, nproc, disk before and after;
- test counts and failing test names (B); per-suite exit codes and new profile
  counts (C);
- the problems that made a run DIAGNOSTIC.

## Network

The run uses GitHub Actions' own traffic (checkout, toolchain setup, artifact
upload), crates.io and the git dependencies' repositories for the locked
dependencies, crates.io for building cargo-llvm-cov, the rustup component
download, and the Ubuntu package mirror for btrfs-progs (C). This is what the
measurement permits; it is not enforced.
