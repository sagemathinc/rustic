//! A backup that had to skip source entries says so in `snapshots`.

use assert_cmd::Command;
use predicates::prelude::{PredicateBooleanExt, predicate};
use tempfile::{TempDir, tempdir};

use rustic_testing::TestResult;

fn rustic(temp_dir: &TempDir) -> Command {
    let mut runner = Command::new(env!("CARGO_BIN_EXE_rustic"));
    _ = runner
        .arg("-r")
        .arg(temp_dir.path().join("repo"))
        .arg("--password")
        .arg("test")
        .arg("--no-progress");
    runner
}

/// A tree deeper than PATH_MAX: the source walker can't read below it, even as root.
///
/// No single path used to build it reaches PATH_MAX: the first half is made
/// from the root, the second half by child processes started in the middle,
/// so the test process keeps its working directory.
#[cfg(target_os = "linux")]
fn deep_source(root: &std::path::Path) -> TestResult<()> {
    let levels = |range: std::ops::Range<usize>| {
        range
            .map(|i| format!("{i:02}-{}", "d".repeat(247)))
            .collect::<Vec<_>>()
            .join("/")
    };
    let middle = root.join(levels(0..10));
    std::fs::create_dir_all(&middle)?;
    let rest = levels(10..20);
    for (program, args) in [
        ("mkdir", vec!["-p".to_string(), rest.clone()]),
        (
            "sh",
            vec![
                "-c".into(),
                r#"echo deep > "$1""#.into(),
                "sh".into(),
                format!("{rest}/bottom"),
            ],
        ),
    ] {
        let status = std::process::Command::new(program)
            .args(&args)
            .current_dir(&middle)
            .status()?;
        assert!(
            status.success(),
            "building the deep tree failed: {program}: {status}"
        );
    }
    Ok(())
}

#[test]
#[cfg(target_os = "linux")]
fn snapshots_show_source_errors() -> TestResult<()> {
    let temp_dir = tempdir()?;
    let source = temp_dir.path().join("source");
    deep_source(&source)?;
    rustic(&temp_dir).arg("init").assert().success();

    rustic(&temp_dir)
        .args(["backup", "--strict"])
        .arg(&source)
        .assert()
        .failure();
    // a non-strict backup saves the snapshot and records how many errors it met
    let output = rustic(&temp_dir)
        .args(["backup", "--json"])
        .arg(&source)
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let snapshot: serde_json::Value = serde_json::from_slice(&output)?;
    let errors = snapshot["summary"]["error_count"]
        .as_u64()
        .expect("summary.error_count is a number");
    assert!(
        errors > 0,
        "the backup skipped entries without counting them"
    );

    // both views show exactly that count
    rustic(&temp_dir)
        .arg("snapshots")
        .assert()
        .success()
        .stdout(predicate::str::contains(format!("({errors} errors)")));
    rustic(&temp_dir)
        .args(["snapshots", "--long"])
        .assert()
        .success()
        .stdout(predicate::str::is_match(format!(
            r"Errors\s*\S*\s*{errors} errors while reading the source; this snapshot is incomplete"
        ))?);
    Ok(())
}

#[test]
fn snapshots_without_errors_show_none() -> TestResult<()> {
    let temp_dir = tempdir()?;
    let source = temp_dir.path().join("source");
    std::fs::create_dir(&source)?;
    std::fs::write(source.join("file"), "file")?;
    rustic(&temp_dir).arg("init").assert().success();
    rustic(&temp_dir)
        .arg("backup")
        .arg(&source)
        .assert()
        .success();
    rustic(&temp_dir)
        .args(["snapshots", "--long"])
        .assert()
        .success()
        .stdout(predicate::str::contains("errors").not());
    rustic(&temp_dir)
        .arg("snapshots")
        .assert()
        .success()
        .stdout(predicate::str::contains("errors").not());
    Ok(())
}
