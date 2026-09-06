//! Explicit local-source metadata admission. Opens repository configuration only,
//! not its index. Use the same source/filter flags and immutable tree for backup.
use std::{io::Write, path::PathBuf};

use abscissa_core::{Command, Runnable, Shutdown};
use anyhow::Result;
use rustic_core::{
    BackupAdmissionOptions, Excludes, LocalSource, LocalSourceFilterOptions,
    LocalSourceSaveOptions, backup_inventory,
};

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
    fn inner_run(&self) -> Result<()> {
        RUSTIC_APP.config().repository.run_open(|repo| {
            let source = LocalSource::new(self.save, &self.excludes, &self.filters, &self.sources)?;
            let inventory = backup_inventory(&source, repo.config(), &self.limits)?;
            let mut out = std::io::stdout().lock();
            serde_json::to_writer(&mut out, &inventory)?;
            writeln!(out)?;
            Ok(())
        })
    }
}
