//! The `backup` parent options `--detect-moves` and `--restored-before`,
//! through the CLI.
#![cfg(unix)]

use std::{fs, path::Path};

use assert_cmd::Command;
use predicates::prelude::{PredicateBooleanExt, predicate};
use tempfile::{TempDir, tempdir};

use rustic_testing::TestResult;

fn rustic(repo: &TempDir, cwd: &Path) -> Command {
    let mut runner = Command::new(env!("CARGO_BIN_EXE_rustic"));
    _ = runner
        .current_dir(cwd)
        .arg("-r")
        .arg(repo.path().join("repo"))
        .arg("--password")
        .arg("test")
        .arg("--no-progress");
    runner
}

/// A repository and a source tree with `top/{d1,d2,d3}/f`.
fn setup() -> TestResult<(TempDir, TempDir)> {
    let repo = tempdir()?;
    let src = tempdir()?;
    for d in ["d1", "d2", "d3"] {
        let dir = src.path().join("top").join(d);
        fs::create_dir_all(&dir)?;
        fs::write(dir.join("f"), d)?;
    }
    _ = rustic(&repo, src.path()).arg("init").assert().success();
    Ok((repo, src))
}

/// Back up `.` with these extra arguments; return the snapshot JSON and the
/// log output.
fn backup(repo: &TempDir, src: &TempDir, args: &[&str]) -> TestResult<(serde_json::Value, String)> {
    let output = rustic(repo, src.path())
        .args(["backup", "--json"])
        .args(args)
        .arg(".")
        .assert()
        .success()
        .get_output()
        .clone();
    Ok((
        serde_json::from_slice(&output.stdout)?,
        String::from_utf8_lossy(&output.stderr).to_string(),
    ))
}

/// (files read: new or changed, files unmodified) of a snapshot.
fn counts(snap: &serde_json::Value) -> (u64, u64) {
    let summary = &snap["summary"];
    (
        summary["files_new"].as_u64().unwrap() + summary["files_changed"].as_u64().unwrap(),
        summary["files_unmodified"].as_u64().unwrap(),
    )
}

#[test]
fn detect_moves_needs_one_file_system_on_the_cli() -> TestResult<()> {
    let (repo, src) = setup()?;
    let (parent, _) = backup(&repo, &src, &["-x"])?;
    let parent = parent["id"].as_str().unwrap().to_string();
    fs::rename(src.path().join("top"), src.path().join("moved"))?;

    // without -x: ignored with a warning, and every moved file is read
    let (snap, log) = backup(&repo, &src, &["--parent", &parent, "--detect-moves"])?;
    assert!(log.contains("ignoring --detect-moves"), "{log}");
    assert_eq!(counts(&snap), (3, 0));
    assert!(snap.get("single_filesystem").is_none());

    // with -x: the moved files are found
    let (snap, log) = backup(&repo, &src, &["-x", "--parent", &parent, "--detect-moves"])?;
    assert!(!log.contains("ignoring --detect-moves"), "{log}");
    assert_eq!(counts(&snap), (0, 3));
    assert_eq!(snap["single_filesystem"], true);
    Ok(())
}

#[test]
fn detect_moves_needs_a_single_filesystem_parent_on_the_cli() -> TestResult<()> {
    let (repo, src) = setup()?;
    // a parent made without -x
    let (parent, _) = backup(&repo, &src, &[])?;
    let parent = parent["id"].as_str().unwrap().to_string();
    fs::rename(src.path().join("top"), src.path().join("moved"))?;
    let (snap, log) = backup(&repo, &src, &["-x", "--parent", &parent, "--detect-moves"])?;
    assert!(log.contains("ignoring --detect-moves"), "{log}");
    assert_eq!(counts(&snap), (3, 0));
    Ok(())
}

#[test]
fn restored_before_in_the_future_is_an_error_on_the_cli() -> TestResult<()> {
    let (repo, src) = setup()?;
    let (parent, _) = backup(&repo, &src, &["-x"])?;
    let parent = parent["id"].as_str().unwrap().to_string();
    _ = rustic(&repo, src.path())
        .args(["backup", "-x", "--parent", &parent])
        .args(["--restored-before", "2999-01-01T00:00:00Z", "."])
        .assert()
        .failure()
        .stderr(predicate::str::contains("is in the future"))
        .stderr(predicate::str::contains("successfully saved").not());
    Ok(())
}
