# CoCalc mutant proofs

This directory proves that a test detects the bug it is meant to detect. A
mutant is a small patch that breaks the code under test. It counts as KILLED
only if the clean build passes the named test and the mutant build fails it
with the expected oracle message. A patch that does not apply, a build failure,
a timeout, a signal, an unexpected exit code or a failure without the oracle
message is INVALID, never a kill.

`run_mutants.py` and `test_run_mutants.py` are byte-identical in
sagemathinc/rustic and sagemathinc/rustic_core. The workflow passes the mode
(`cli` or `core`) as a literal, and the script checks it against the tree
before doing anything else.

## Files

- `MUTANTS.tsv`: the real mutants. The header is
  `id target patch test expect kind`, separated by tabs. `kind` is always
  `real`.
- `patches/`: one patch per mutant, plus the control patches.
- `fixtures/`: verbatim copies of real files for the self-tests. Their source
  revisions are in `fixtures/SOURCES.txt`.
- `../workflows/cocalc-mutants.yml`: the workflow.

## Rules

- **Controls.** Four controls run in every run, and `MUTANTS.tsv` cannot list
  or change them. `ctl-killed` must be KILLED, `ctl-noop` must SURVIVE, and
  `ctl-wrong-oracle` and `ctl-no-apply` must be INVALID.
- **Tests** come from a fixed table in `run_mutants.py`. Each one declares its
  failure exit codes, which never include 0 or anything at or above 124. They
  are never shell strings.
  - In the CLI repo: `selftest:version`, `rt:<scenario>` and
    `rt-snap:<scenario>`. The last two run `tests/cocalc/roundtrip.py` and
    `tests/cocalc/rt_snapshot.py` respectively, from the clean tree, as root,
    on a btrfs loopback.
  - In the core repo: `cargo-test:<path>`, which runs the rustic_core library
    test binary with `--exact`.
- **The clean run comes first.** Each distinct test runs once on the clean
  build and must pass before any mutant uses it.
- **Oracles** are literal pieces separated by `*`, matched in order within one
  line. At least one piece must be 4 or more characters long.
- **Isolation.** Each tree has its own worktree and its own `CARGO_TARGET_DIR`.
  A mutant whose binary is identical to the clean one is INVALID, except for
  `ctl-noop`.
- **Patches** must be in-place edits of existing regular files inside the
  tree, applied with `git apply --whitespace=error`. They may not touch
  `.github/`, `Cargo.toml`, `Cargo.lock` or `rust-toolchain(.toml)`. Mode,
  rename, copy and binary changes are refused.
- **Cargo.lock.**
  - Core mode: the lock must not change at all.
  - CLI mode: the core repo is checked out at the rev pinned in `Cargo.lock`,
    and every package whose `source` is that pinned git rev is overridden with
    a `[patch]` path to the workspace member with the same name and version.
    The only lock change allowed is the removal of those packages' `source`
    (and `checksum`). The observed change is recorded in `lock-transform.json`.
- **Bounds.** At most 8 real mutants (12 with the controls) and 4 distinct
  tests. Builds are limited to 10 minutes and test runs to 8. Output is capped
  at 4 MiB per run, and the artifact is capped in total. The workflow's
  timeouts are derived from these numbers, and a self-test checks them.

## Known gap: no scheduled or manual runs

The forks' default branch is `main`, and it holds no CoCalc workflow. GitHub
only runs `schedule` and `workflow_dispatch` for workflows on the default
branch, so these never fire here. That includes the daily cron in
`cocalc-ci.yml`, which has never run. This workflow therefore runs on push
only: to `cocalc/ci-mutants` (its own pull request branch) and to
`cocalc/mutants/**` proof branches. A proof branch merges a test pull
request's head with this workflow; the proof record names both commits.
Whether to change the default branch is a repository-settings question for the
maintainers, and this pull request does not change it.

## Deviation from the reviewed design (v2.3)

The design listed three overridden packages (rustic_core, rustic_backend and
rustic_testing). The real lock has four with the pinned source, because
rustic_extents is a workspace sibling that rustic_core depends on. The override
set is therefore derived from `Cargo.lock`, as described above. Review approved
this change before implementation.

## Running the self-tests

```sh
python3 -I -B -m unittest discover -s .github/cocalc-mutants -p 'test_*.py'
```

They need no cargo, git or root.
