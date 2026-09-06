//! Explicit local-source metadata admission. Opens repository configuration only,
//! not its index. Use the same source/filter flags and immutable tree for backup.
use std::{
    io::{self, Write},
    num::NonZeroU64,
    path::{Path, PathBuf},
};

use abscissa_core::{Command, Runnable, Shutdown};
use anyhow::{Context, Result};
use rustic_core::{
    BackupAdmissionOptions, ErrorKind, Excludes, LocalSource, LocalSourceFilterOptions,
    LocalSourceSaveOptions, RusticError, backup_inventory, backup_selection_inventory,
};
use serde::Serialize;
use serde_json::{Value, json};

use crate::{Application, RUSTIC_APP, status_err};

#[derive(clap::Parser, Command, Debug)]
pub(crate) struct BackupInventoryCmd {
    /// Local paths to inspect. Does not read stdin or launch source commands.
    #[clap(required = true, value_name = "SOURCE", value_hint = clap::ValueHint::AnyPath)]
    sources: Vec<PathBuf>,
    #[clap(flatten)]
    limits: BackupAdmissionOptions,
    #[clap(flatten)]
    excludes: Excludes,
    #[clap(flatten)]
    filters: LocalSourceFilterOptions,
    #[clap(flatten)]
    save: LocalSourceSaveOptions,
    /// Stream size-exclusion NDJSON, including omitted paths and a completion footer.
    /// Requires an explicit positive --exclude-larger-than and --max-report-bytes.
    #[clap(long, requires_all = ["exclude_larger_than", "max_report_bytes"])]
    exclusion_report: bool,
    /// Maximum report output bytes. Exceeding this fails, never truncates evidence.
    #[clap(long, requires = "exclusion_report")]
    max_report_bytes: Option<NonZeroU64>,
}

struct BoundedReport<W> {
    inner: W,
    remaining: u64,
}

impl<W: Write> Write for BoundedReport<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let length = u64::try_from(bytes.len()).map_err(io::Error::other)?;
        if length > self.remaining {
            return Err(io::Error::other(
                "exclusion report exceeds max-report-bytes",
            ));
        }
        let written = self.inner.write(bytes)?;
        self.remaining -= u64::try_from(written).map_err(io::Error::other)?;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

fn write_record(out: &mut impl Write, value: &impl Serialize) -> Result<()> {
    serde_json::to_writer(&mut *out, value)?;
    writeln!(out)?;
    Ok(())
}

// Reports must survive JavaScript consumers without rounding a huge sparse
// size/inode/timestamp. Numeric inventory values use canonical decimal strings.
fn decimal_numbers(value: Value) -> Value {
    match value {
        Value::Number(number) => Value::String(number.to_string()),
        Value::Array(values) => Value::Array(values.into_iter().map(decimal_numbers).collect()),
        Value::Object(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, decimal_numbers(value)))
                .collect(),
        ),
        other => other,
    }
}

fn encoded_path(path: &Path) -> Result<Value> {
    #[cfg(unix)]
    let (encoding, bytes) = {
        use std::os::unix::ffi::OsStrExt;
        ("unix-bytes-hex", path.as_os_str().as_bytes().to_vec())
    };
    #[cfg(windows)]
    let (encoding, bytes) = {
        use std::os::windows::ffi::OsStrExt;
        (
            "windows-utf16le-hex",
            path.as_os_str()
                .encode_wide()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        )
    };
    #[cfg(not(any(unix, windows)))]
    let (encoding, bytes) = (
        "utf8-hex",
        path.to_str()
            .context("unencodable source path")?
            .as_bytes()
            .to_vec(),
    );
    let mut value = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        write!(value, "{byte:02x}")?;
    }
    Ok(json!({ "encoding": encoding, "value": value }))
}

impl Runnable for BackupInventoryCmd {
    fn run(&self) {
        if let Err(err) = self.inner_run() {
            status_err!("{}", err);
            RUSTIC_APP.shutdown(Shutdown::Crash);
        }
    }
}

impl BackupInventoryCmd {
    #[allow(clippy::too_many_lines)]
    fn inner_run(&self) -> Result<()> {
        RUSTIC_APP.config().repository.run_open(|repo| {
            if self.exclusion_report {
                let threshold = self.filters.exclude_larger_than
                    .and_then(|size| NonZeroU64::new(size.as_u64()))
                    .context("exclusion reports require a positive explicit size threshold")?;
                let maximum = self.max_report_bytes.context("exclusion reports require max-report-bytes")?;
                let mut filters = self.filters.clone();
                filters.exclude_larger_than = None;
                let source = LocalSource::new(self.save, &self.excludes, &filters, &self.sources)?;
                let mut out = BoundedReport { inner: io::stdout().lock(), remaining: maximum.get() };
                write_record(&mut out, &json!({
                    "schema_version": 1, "type": "header",
                    "exclude_larger_than_bytes": threshold.get().to_string(),
                    "max_report_bytes": maximum.get().to_string(),
                    "sources": self.sources.iter().map(|path| encoded_path(path)).collect::<Result<Vec<_>>>()?,
                    "normal_filters": filters, "excludes": self.excludes, "save_options": self.save,
                    "admission": decimal_numbers(serde_json::to_value(&self.limits)?),
                }))?;
                let selection = backup_selection_inventory(&source, repo.config(), &self.limits,
                    Some(threshold), |path, node| {
                        let record = (|| -> Result<()> {
                            write_record(&mut out, &json!({
                                "schema_version": 1, "type": "excluded", "reason": "apparent_size",
                                "path": encoded_path(path)?, "apparent_bytes": node.meta.size.to_string(),
                                "file_version": {
                                    "inode": node.meta.inode.to_string(),
                                    "mtime_ns": node.meta.mtime.map(|time| time.as_nanosecond().to_string()),
                                    "ctime_ns": node.meta.ctime.map(|time| time.as_nanosecond().to_string()),
                                    "mode": node.meta.mode, "uid": node.meta.uid, "gid": node.meta.gid,
                                },
                            }))
                        })();
                        record.map_err(|error| RusticError::new(ErrorKind::InputOutput,
                            "Exclusion report output failed: {reason}.").attach_context("reason", error.to_string()))
                    })?;
                write_record(&mut out, &json!({
                    "schema_version": 1, "type": "complete",
                    "inventory": decimal_numbers(serde_json::to_value(selection)?),
                }))?;
                out.flush()?;
                return Ok(());
            }
            let source = LocalSource::new(self.save, &self.excludes, &self.filters, &self.sources)?;
            let inventory = backup_inventory(&source, repo.config(), &self.limits)?;
            let mut out = io::stdout().lock();
            serde_json::to_writer(&mut out, &inventory)?;
            writeln!(out)?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn report_budget_allows_equality_and_never_exceeds_limit() {
        let mut out = BoundedReport {
            inner: Vec::new(),
            remaining: 3,
        };
        out.write_all(b"abc").unwrap();
        assert!(out.write_all(b"d").is_err());
        assert_eq!(out.inner, b"abc");
    }

    #[test]
    fn large_inventory_counters_are_exact_decimal_strings() {
        let value = decimal_numbers(json!({"n": u64::MAX, "nested": [9007199254740993_u64]}));
        assert_eq!(
            value,
            json!({"n": "18446744073709551615", "nested": ["9007199254740993"]})
        );
    }

    #[cfg(unix)]
    #[test]
    fn filenames_are_lossless_even_with_invalid_utf8_and_newlines() {
        use std::{ffi::OsStr, os::unix::ffi::OsStrExt};
        assert_eq!(
            encoded_path(Path::new(OsStr::from_bytes(b"a/\xff\n*?"))).unwrap(),
            json!({"encoding": "unix-bytes-hex", "value": "612fff0a2a3f"})
        );
    }
}
