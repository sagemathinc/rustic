//! The report is a bounded, metadata-only inventory, not a backup success marker.
#![cfg(unix)]

use std::{
    ffi::OsStr,
    fs,
    os::unix::{ffi::OsStrExt, fs::symlink},
    path::Path,
    time::Duration,
};

use assert_cmd::Command;
use serde_json::Value;
use tempfile::{TempDir, tempdir};

fn cli(root: &TempDir) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_rustic"));
    command
        .current_dir(root.path())
        .timeout(Duration::from_secs(15))
        .args(["--no-progress", "--password", "test", "--repository"])
        .arg(root.path().join("repo"));
    command
}

fn report(root: &TempDir, extra: &[&str]) -> std::process::Output {
    cli(root)
        .args([
            "backup-inventory",
            "--exclusion-report",
            "--exclude-larger-than",
            "4",
            "--max-report-bytes",
            "32768",
            "--max-entries",
            "20",
            "--max-file-bytes",
            "4",
            "--max-apparent-bytes",
            "4",
            "--max-chunk-references",
            "1",
            "--max-metadata-bytes",
            "32768",
        ])
        .args(extra)
        .arg("source")
        .output()
        .unwrap()
}

fn records(bytes: &[u8]) -> Vec<Value> {
    std::str::from_utf8(bytes)
        .unwrap()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}

fn path_hex(path: &Path) -> String {
    path.as_os_str()
        .as_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[test]
fn report_matches_size_filter_and_counts_hardlinks_without_reading_holes() {
    let root = tempdir().unwrap();
    cli(&root).arg("init").assert().success();
    let source = root.path().join("source");
    fs::create_dir(&source).unwrap();
    fs::write(source.join("equal"), b"1234").unwrap();
    fs::write(source.join("empty"), b"").unwrap();
    fs::File::create(source.join("huge"))
        .unwrap()
        .set_len(1 << 40)
        .unwrap();
    let weird = source.join(OsStr::from_bytes(b"\xff\n*[?"));
    fs::hard_link(source.join("huge"), &weird).unwrap();
    symlink("huge", source.join("symlink")).unwrap();
    fs::write(source.join("normal-ignore"), b"12345").unwrap();
    let output = report(&root, &["--glob", "!normal-ignore"]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let rows = records(&output.stdout);
    assert_eq!(rows.len(), 4);
    assert_eq!(rows[0]["type"], "header");
    assert!(rows[0]["normal_filters"]["exclude-larger-than"].is_null());
    assert_eq!(rows.last().unwrap()["type"], "complete");
    let inventory = &rows.last().unwrap()["inventory"];
    assert_eq!(inventory["excluded_files"], "2");
    assert_eq!(
        inventory["excluded_apparent_bytes"],
        (2_u64 << 40).to_string()
    );
    assert_eq!(inventory["retained"]["apparent_bytes"], "4");
    assert_eq!(inventory["retained"]["files"], "2");
    assert_eq!(inventory["inspected_entries"], "5");
    assert!(rows.iter().any(|row| {
        row["path"]["value"]
            == path_hex(
                Path::new("source")
                    .join(OsStr::from_bytes(b"\xff\n*[?"))
                    .as_path(),
            )
    }));
    assert_eq!(
        fs::read_dir(root.path().join("repo/snapshots"))
            .unwrap()
            .count(),
        0
    );

    // Use the actual backup filter, not a second hand-written selector.
    cli(&root)
        .args([
            "backup",
            "--strict",
            "--exclude-larger-than",
            "4",
            "--glob",
            "!normal-ignore",
            "--max-apparent-bytes",
            "4",
            "--json",
            "source",
        ])
        .assert()
        .success();
    cli(&root)
        .args(["restore", "--strict", "latest", "restore"])
        .assert()
        .success();
    assert_eq!(
        fs::read(root.path().join("restore/source/equal")).unwrap(),
        b"1234"
    );
    assert!(!root.path().join("restore/source/huge").exists());
    assert!(
        !root
            .path()
            .join("restore/source")
            .join(OsStr::from_bytes(b"\xff\n*[?"))
            .exists()
    );
    assert!(
        fs::symlink_metadata(root.path().join("restore/source/symlink"))
            .unwrap()
            .file_type()
            .is_symlink()
    );
}

#[test]
fn incomplete_and_over_budget_reports_have_no_completion_footer() {
    let root = tempdir().unwrap();
    cli(&root).arg("init").assert().success();
    fs::create_dir(root.path().join("source")).unwrap();
    for name in ["a", "b"] {
        fs::File::create(root.path().join("source").join(name))
            .unwrap()
            .set_len(100)
            .unwrap();
    }
    for flags in [
        vec!["--max-entries", "1"],
        vec!["--max-report-bytes", "1"],
        vec!["--max-metadata-bytes", "1"],
    ] {
        // Invoke directly to avoid duplicate clap options obscuring the reason.
        let output = cli(&root)
            .args([
                "backup-inventory",
                "--exclusion-report",
                "--exclude-larger-than",
                "4",
            ])
            .args(if flags[0] == "--max-report-bytes" {
                vec![]
            } else {
                vec!["--max-report-bytes", "32768"]
            })
            .args(&flags)
            .arg("source")
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(
            !records(&output.stdout)
                .iter()
                .any(|row| row["type"] == "complete")
        );
    }
    let output = report(&root, &[]);
    assert!(output.status.success());
    assert_eq!(
        records(&output.stdout).last().unwrap()["inventory"]["retained"]["files"],
        "0"
    );
    cli(&root)
        .args(["backup-inventory", "--exclusion-report", "source"])
        .assert()
        .failure();
    assert_eq!(
        fs::read_dir(root.path().join("repo/snapshots"))
            .unwrap()
            .count(),
        0
    );
}
