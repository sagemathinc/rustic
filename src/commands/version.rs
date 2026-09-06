use abscissa_core::{Command, Runnable};
use clap::Parser;

// `version` command
#[derive(Command, Debug, Parser)]
pub struct VersionCmd {
    /// Machine-readable capabilities; does not load profiles or repository data.
    #[clap(long)]
    json: bool,
}

impl Runnable for VersionCmd {
    // Print the version and exit
    fn run(&self) {
        if self.json {
            println!(
                "{}",
                serde_json::json!({
                    "schema_version": 1,
                    "version": crate::commands::version(),
                    "capabilities": {
                        "strict_backup": true,
                        "strict_local_metadata": 1,
                        "strict_restore": true,
                        "sparse_required_restore": cfg!(target_os = "linux"),
                        "hole_aware_backup": cfg!(target_os = "linux"),
                        "backup_inventory": 1,
                        "backup_exclusion_inventory": 1,
                        "backup_admission": 1,
                    },
                })
            );
            return;
        }
        // Use the existing version helper from the parent module
        println!("rustic {}", crate::commands::version());
    }
}
