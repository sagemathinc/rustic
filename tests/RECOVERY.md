# Native Sparse Recovery Candidate

This bundle is a tested candidate, not permission to replace a production
binary or delete a source project. It targets GNU Linux on the named CPU
architecture, qualified on Ubuntu 24.04 and Btrfs. It is not a musl build.

## Establish Trust Before Recovery

Verify the bundle checksum AND its GitHub build-provenance attestation against
`sagemathinc/rustic` and the `sparse-qualification.yml` workflow at the reviewed
CLI commit. A checksum distributed beside an arbitrary download is not an
authenticity check. Preserve the attestation and trusted verification material
when preparing an offline recovery kit. Do not rely on an artifact's filename.

With a GitHub CLI that supports attestation verification, use the independently
reviewed source commit, not one read from an unverified archive:

```sh
gh attestation verify CANDIDATE.tar.xz --bundle attestation.json \
  --repo sagemathinc/rustic \
  --signer-workflow sagemathinc/rustic/.github/workflows/sparse-qualification.yml \
  --source-digest REVIEWED_FULL_CLI_COMMIT \
  --source-ref refs/heads/cocalc/sparse-backups --deny-self-hosted-runners
```

For fully offline verification, also supply `--custom-trusted-root` with trust
material obtained and reviewed beforehand. A bundle supplied with an artifact
is evidence to verify, not itself a trust anchor. Qualification CI verifies both
architecture bundles and confirms that an unrelated source commit is rejected.

Extract into a fresh operator-controlled directory, then run `sha256sum -c
SHA256SUMS`. Compare `manifest.json` with the approved binary hash, CLI/core
commits, lockfile and target. Confirm host compatibility before executing.
`glibc_symbol_minimum` is not a complete shared-library compatibility test.
The dependency inventory records the resolved Cargo graph; it does not assert
vulnerability freedom or bit-for-bit build reproducibility.

## Restore Without Expanding Sparse Files

Supply repository credentials separately in an operator-controlled profile.
Never place credentials in this bundle. For example:

```sh
./rustic version --json
./rustic -P /secure/recovery-profile check --read-data
./rustic -P /secure/recovery-profile restore --strict --sparse by-content-required SNAPSHOT /empty/staging
```

Use a fresh, inaccessible Btrfs staging subvolume with enforced quota and
reserved data/metadata headroom. Supervise the entire job with finite runtime,
memory, CPU and I/O budgets. Confirm byte contents, hardlinks, ownership and
quota accounting before publication. Preserve the repository and any live
source if restoration or verification fails. Do not retry without sparse mode
or without strict mode just to make the command succeed.

`qualification.json` proves the bundled binary passed a bounded disposable
Btrfs test, including rejecting a dense 5 GiB allocation under a 4 GiB quota,
then restoring a 10 GiB sparse file, mixed chunks, hardlinks and incremental
snapshot reuse. It is not a proof about every repository or CoCalc lifecycle.

The repository format is unchanged. Older Rustic readers can still restore
correct bytes while allocating the entire apparent file size. Format
compatibility alone therefore does not make an older reader quota-safe.
Keep this tested reader available for offline recovery and rollback; do not
discard it merely because an older writer can read the repository.

## Bounded Exclusion Inventory

`backup-inventory --exclusion-report --exclude-larger-than SIZE
--max-report-bytes BYTES SOURCE` streams an NDJSON header, zero or more excluded
file records, and one final `complete` record. It uses the ordinary source
filters, but counts oversized files against traversal and metadata budgets
before excluding them from retained-content budgets. Equality with SIZE is
eligible. Supply explicit admission limits and a supervised runtime as well.

Require BOTH a successful process exit and the completion record. Any prefix
from a failed, interrupted, or over-budget scan is incomplete evidence. Reports
must be stored outside user-writable trees and bound to the immutable source,
effective policy, and actual backup result. A completed inventory does not mean
a backup succeeded, is complete, or fits a restore quota.

Paths are lossless hex-encoded OS strings (`unix-bytes-hex` on Linux), not glob
patterns or JSON text filenames. Validate selection boundaries after decoding;
do not turn report paths into shell commands or unescaped exclusion globs.
Inventory counters, sizes, inodes and nanosecond timestamps use decimal strings
so consumers do not round them through JavaScript numbers. File-version fields
are bounded metadata hints, not content hashes or authorization. Missing or
overridden metadata must not hide new warnings.

The inventory itself writes no repository snapshots. Back up the SAME immutable
tree with the SAME normal filters and size threshold, strict completion and
retained-work limits. Upload/report failures must not authorize source deletion.
