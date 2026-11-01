# CoCalc's rustic

This fork is the rustic that [CoCalc](https://cocalc.com) runs for project
backups, on its project hosts and in CoCalc Star. We own it: every change is
tested here and kept here, and we also offer each one upstream to
[rustic-rs](https://github.com/rustic-rs), with no dependence on it being
merged.

## Branches

- **`cocalc/main`**: what CoCalc builds and ships. Changes arrive as small,
  self-contained pull requests, each passing "CoCalc CI" first.
- **`main`**: mirrors upstream; it is not built for CoCalc.
- The core library lives in
  [sagemathinc/rustic_core](https://github.com/sagemathinc/rustic_core), on its
  own `cocalc/main` branch. `Cargo.toml` pins it by exact commit (`rev = ...`),
  so a build never moves without a commit here.

## Upstream

- Upstream is merged into `cocalc/main` (both repositories) about once a month,
  as its own pull request, and on demand for security fixes. Upstream releases
  and advisories (`cargo audit`, the audit workflow) are watched.
- A change of ours is offered upstream as its own pull request when it is
  generally useful. New behavior is behind options, and defaults stay upstream's
  unless a default is a bug.

## Releases

- Tags look like `v<upstream version>-cocalc.<N>`, e.g. `v0.11.4-cocalc.1`.
  `rustic --version` prints the same version without the `v`.
- Pushing a tag runs `release-cd.yml`. It builds only a tag on a commit of
  `cocalc/main` on which "CoCalc CI" succeeded. It builds natively for x86_64
  and arm64 and creates a draft GitHub release with
  `rustic-<tag>-linux-<arch>.tar.gz` and a `.sha256` for each. The draft is
  published by hand after the assets are checked.
- CoCalc pins the release in `src/packages/backend/sandbox/install.ts`
  (`SPEC.rustic`), the single pin for project hosts and CoCalc Star. A new pin
  goes through CoCalc's normal review, and is staged on a few projects first.

## Tests

- **CoCalc CI** (`cocalc-ci.yml`), on every pull request to `cocalc/main`, on
  every push, and nightly:
  - the unit and integration tests;
  - a backup, restore and compare round trip (`tests/cocalc/roundtrip.py`) on
    btrfs, btrfs with `compress=zstd`, and ext4 loopbacks, on x86_64 and arm64.
    The fixture (`tests/cocalc/rt/fixture.py`) covers contents, sparse files,
    modes, owners, times, symlinks, hardlink groups, special files, xattrs,
    ACLs, file capabilities, unusual names and a large directory. The scenarios
    (`tests/cocalc/rt/scenarios.py`) restore it dense, sparse (holes must stay
    holes), as a subtree, over a changed copy with `--delete` and
    `--verify-existing`, and from an incremental backup with a parent. A skipped
    case fails CI unless `CI_ALLOWED_SKIPS` names it with a reason; known
    failures are listed in `EXPECTED_FAILURES`, and one that passes fails CI
    until its entry is removed.
- **Sparse Recovery Qualification** (`sparse-qualification.yml`): sparse files
  and quotas on btrfs, with provenance for the tested binaries.
- More suites will follow: change-detection adversaries, crash safety,
  corruption, fuzzing, scale, and mutation testing as the measure of test
  strength.

## Prune and backup on the same repository

Never run `prune` while a backup into the same repository can run. Serialize
them per repository, and never use `prune --instant-delete` while any backup
could run.

- The exclusion must be keyed by the repository, not by the client or host that
  writes into it. When several clients share one repository, every one of them
  must check the same lock before it starts a backup.
- rustic takes no lock for this. A backup that deduplicates against blobs a
  concurrent prune drops from the index saves a snapshot that references missing
  blobs: `check --read-data` fails, and the snapshot may not restore.
- With the default two-phase prune, the next prune recovers the still-existing
  packs, and the repository is consistent again.
- With `--instant-delete`, the loss is permanent.
- `tests/cocalc/crash.py` asserts this contract.

## Known differences

- A restore leaves the target directory's own mode, owner, times and xattrs
  alone; only the entries inside it are restored.
