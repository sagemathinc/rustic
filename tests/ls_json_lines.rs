//! `rustic ls --json-lines`: one line per entry with its full node metadata.

use std::{collections::BTreeSet, ffi::OsStr, fs, path::Path};

use assert_cmd::Command;
use rustic_core::repofile::{Metadata, Node, NodeType};
use rustic_testing::TestResult;
use serde_json::Value;
use tempfile::{TempDir, tempdir};

fn rustic(temp_dir: &TempDir) -> Command {
    let mut runner = Command::new(env!("CARGO_BIN_EXE_rustic"));
    runner
        .arg("-r")
        .arg(temp_dir.path().join("repo"))
        .arg("--password")
        .arg("test")
        .arg("--no-progress");
    runner
}

fn escaped(name: &OsStr) -> String {
    Node::new_node(name, NodeType::Dir, Metadata::default()).name
}

fn lines(output: &[u8]) -> TestResult<Vec<Value>> {
    Ok(output
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(serde_json::from_slice)
        .collect::<Result<_, _>>()?)
}

#[cfg(unix)]
#[test]
fn json_lines_lists_every_entry_with_its_node() -> TestResult<()> {
    use std::os::unix::{ffi::OsStrExt, fs::symlink};

    let temp_dir = tempdir()?;
    rustic(&temp_dir).arg("init").assert().success();

    let source = tempdir()?;
    let root = source.path();
    fs::create_dir_all(root.join("dir/nested"))?;
    fs::write(root.join("dir/nested/file"), b"content")?;
    fs::write(root.join("line\nbreak"), b"x")?;
    let odd = OsStr::from_bytes(b"not-utf8-\xff\xfe");
    fs::write(root.join(odd), b"y")?;
    fs::hard_link(root.join("dir/nested/file"), root.join("hardlink"))?;
    symlink("dir/nested/file", root.join("link"))?;

    rustic(&temp_dir)
        .current_dir(root)
        .args(["backup", "."])
        .assert()
        .success();

    let output = rustic(&temp_dir)
        .args(["ls", "--json-lines", "latest"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let lines = lines(&output)?;

    // header: the format, and the full ids of the snapshot and its tree
    let header = &lines[0];
    assert_eq!(header["format"], "rustic-ls-json-lines");
    assert_eq!(header["version"], 1);
    for key in ["snapshot", "tree"] {
        let id = header[key].as_str().unwrap();
        assert_eq!(id.len(), 64, "{key} id must be complete: {id}");
        assert!(id.bytes().all(|b| b.is_ascii_hexdigit()));
    }

    // trailer: complete, with the number of entries in between
    let entries = &lines[1..lines.len() - 1];
    assert_eq!(
        lines[lines.len() - 1],
        serde_json::json!({"complete": true, "entries": entries.len()})
    );

    // every path, as escaped components that round-trip any name
    let got: BTreeSet<Vec<String>> = entries
        .iter()
        .map(|entry| {
            entry["path"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c.as_str().unwrap().to_string())
                .collect()
        })
        .collect();
    let want: BTreeSet<Vec<String>> = [
        vec!["dir"],
        vec!["dir", "nested"],
        vec!["dir", "nested", "file"],
        vec!["hardlink"],
        vec!["link"],
    ]
    .into_iter()
    .map(|parts| parts.into_iter().map(|p| escaped(OsStr::new(p))).collect())
    .chain([vec![escaped(OsStr::new("line\nbreak"))], vec![escaped(odd)]])
    .collect();
    assert_eq!(got, want);

    // the node is the full node: its name is the last path component, and
    // files carry their content ids, directories their subtree
    for entry in entries {
        let node = &entry["node"];
        let path = entry["path"].as_array().unwrap();
        assert_eq!(node["name"], path[path.len() - 1]);
        match node["type"].as_str().unwrap() {
            "file" => assert!(node["content"].is_array()),
            "dir" => assert!(node["subtree"].is_string()),
            "symlink" => assert_eq!(node["linktarget"], "dir/nested/file"),
            other => panic!("unexpected node type {other}"),
        }
    }
    Ok(())
}

#[test]
fn json_lines_conflicts_with_other_formats() -> TestResult<()> {
    let temp_dir = tempdir()?;
    for other in ["--json", "--long", "--summary"] {
        rustic(&temp_dir)
            .args(["ls", "--json-lines", other, "latest"])
            .assert()
            .failure();
    }
    // a local path works too (no snapshot or tree in the header)
    let output = rustic(&temp_dir)
        .args(["ls", "--json-lines"])
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/hooks-fixtures"))
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let lines = lines(&output)?;
    assert!(lines[0]["snapshot"].is_null() && lines[0]["tree"].is_null());
    assert_eq!(lines[lines.len() - 1]["complete"], true);
    Ok(())
}
