//! CLI access to native room imports, exports and multipart uploads.

use std::path::PathBuf;

use anyhow::Result;
use clap::{Args, Subcommand};
use serde_json::json;

use crate::{
    archive::{self, ExportOptions, ImportOptions, ImportSource, S3Options},
    host::Host,
    settings::ClusterConfig,
};

#[derive(Args)]
pub struct Arguments {
    #[arg(long, default_value = "/srv/dst", global = true)]
    root: PathBuf,
    #[arg(long, default_value = "/etc/containers/systemd", global = true)]
    quadlet_dir: PathBuf,
    #[arg(long, default_value = "systemctl", global = true)]
    systemctl: PathBuf,
    #[arg(long, global = true)]
    user: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Args)]
struct Source {
    number: u16,
    #[arg(long, conflicts_with = "client_compatible")]
    room_id: Option<String>,
    /// Adapt exactly one master and one secondary for a client save slot.
    #[arg(long)]
    client_compatible: bool,
    /// Replacement export configuration as JSON, @file, or stdin (-).
    #[arg(long)]
    configuration: Option<String>,
    #[arg(long, default_value_t = archive::DEFAULT_COMPRESSION_LEVEL, value_parser = clap::value_parser!(u32).range(1..=22))]
    compression_level: u32,
}

impl Source {
    fn options(&self) -> Result<ExportOptions> {
        Ok(ExportOptions {
            room_id: self.room_id.clone(),
            client_compatible: self.client_compatible,
            configuration: self
                .configuration
                .as_deref()
                .map(|source| ClusterConfig::from_value(super::input(source)?))
                .transpose()?,
        })
    }
}

#[derive(Subcommand)]
enum Command {
    /// Rebuild one native room from a directory, ZIP or 7z archive.
    Import {
        source: PathBuf,
        /// A new directory for the imported room.
        #[arg(long)]
        output: PathBuf,
        /// Reconstruct server settings from a client's Master/Caves shard indexes.
        #[arg(long)]
        from_client: bool,
    },
    /// Export one room to an explicit local archive path.
    Export {
        #[command(flatten)]
        source: Source,
        #[arg(long)]
        output: PathBuf,
    },
    /// Export and upload one room; explicit S3 values override AWS environment settings.
    Upload {
        #[command(flatten)]
        source: Source,
        #[arg(long)]
        bucket: Option<String>,
        #[arg(long)]
        endpoint: Option<String>,
        #[arg(long)]
        region: Option<String>,
        #[arg(long)]
        access_key_id: Option<String>,
        #[arg(long)]
        secret_access_key: Option<String>,
        #[arg(long)]
        session_token: Option<String>,
        #[arg(long, default_value = "")]
        object_prefix: String,
        #[arg(long)]
        url_prefix: Option<String>,
    },
}

pub async fn run(arguments: Arguments, compact: bool) -> Result<bool> {
    let mut host = Host::new(arguments.root, arguments.quadlet_dir);
    host.systemd.executable = arguments.systemctl;
    host.systemd.user = arguments.user;
    match arguments.command {
        Command::Import {
            source,
            output,
            from_client,
        } => {
            let configuration = archive::import_cluster(
                ImportSource::Path(source),
                output.clone(),
                ImportOptions { from_client },
            )
            .await?;
            super::emit(
                &json!({"path": output, "configuration": configuration}),
                compact,
            )?;
        }
        Command::Export { source, output } => {
            let archive = archive::export_host(
                &host,
                source.number,
                source.options()?,
                source.compression_level,
            )
            .await?;
            let filename = archive.filename.clone();
            archive.save(&output)?;
            super::emit(
                &json!({"number": source.number, "filename": filename, "path": output}),
                compact,
            )?;
        }
        Command::Upload {
            source,
            bucket,
            endpoint,
            region,
            access_key_id,
            secret_access_key,
            session_token,
            object_prefix,
            url_prefix,
        } => {
            let mut interrupt =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
            let mut terminate =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
            let store = S3Options {
                bucket,
                endpoint,
                region,
                access_key_id,
                secret_access_key,
                session_token,
            }
            .build()?;
            let archive = archive::export_host(
                &host,
                source.number,
                source.options()?,
                source.compression_level,
            )
            .await?;
            let task = archive.start_upload(store, &object_prefix, url_prefix.as_deref())?;
            let result = task
                .wait_until_cancelled(async move {
                    tokio::select! {
                        _ = interrupt.recv() => {},
                        _ = terminate.recv() => {},
                    }
                })
                .await?;
            super::emit(&result, compact)?;
        }
    }
    Ok(true)
}
