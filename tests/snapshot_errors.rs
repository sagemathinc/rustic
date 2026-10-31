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
#[cfg(target_os = "linux")]
fn deep_source(root: &std::path::Path) -> TestResult<()> {
    let mut dir = root.to_path_buf();
    std::fs::create_dir(&dir)?;
    std::env::set_current_dir(&dir)?;
    for i in 0..20 {
        let name = format!("{i:02}-{}", "d".repeat(247));
        std::fs::create_dir(&name)?;
        std::env::set_current_dir(&name)?;
        dir.push(name);
    }
    std::fs::write("bottom", "below PATH_MAX")?;
    Ok(())
}

#[test]
#[cfg(target_os = "linux")]
fn snapshots_show_source_errors() -> TestResult<()> {
    let temp_dir = tempdir()?;
    let source = temp_dir.path().join("source");
    let home = std::env::current_dir()?;
    deep_source(&source)?;
    std::env::set_current_dir(&home)?;
    rustic(&temp_dir).arg("init").assert().success();

    rustic(&temp_dir)
        .args(["backup", "--strict"])
        .arg(&source)
        .assert()
        .failure();
    rustic(&temp_dir)
        .arg("backup")
        .arg(&source)
        .assert()
        .success();
    rustic(&temp_dir)
        .args(["backup", "--json"])
        .arg(&source)
        .assert()
        .success()
        .stdout(predicate::str::contains("\"error_count\""));

    rustic(&temp_dir)
        .arg("snapshots")
        .assert()
        .success()
        .stdout(predicate::str::is_match(r"\(\d+ errors\)")?);
    rustic(&temp_dir)
        .args(["snapshots", "--long"])
        .assert()
        .success()
        .stdout(
            predicate::str::contains("Errors")
                .and(predicate::str::contains("this snapshot is incomplete")),
        );
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
