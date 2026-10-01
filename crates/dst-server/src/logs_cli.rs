//! CLI adapters for the shared local log readers.

use std::{io::Write, path::PathBuf};

use anyhow::{Context, Result};
use clap::{Args, Subcommand};
use serde_json::json;

use crate::logs::{JournalLogs, JournalQuery, NetdataLogQuery, NetdataLogs};

#[derive(Args)]
pub struct Arguments {
    #[command(subcommand)]
    command: Command,
}

#[derive(Args)]
struct Limits {
    /// Maximum size of an individual native log record.
    #[arg(long, default_value_t = 4 * 1024 * 1024)]
    max_record_bytes: usize,
    /// Maximum total output of a finite query.
    #[arg(long, default_value_t = 64 * 1024 * 1024)]
    max_output_bytes: usize,
    /// Completion timeout in seconds for finite queries.
    #[arg(long, default_value_t = 120.0)]
    timeout: f64,
}

#[derive(Subcommand)]
enum Command {
    /// Query or follow journalctl records, retaining native fields and cursors.
    Journal {
        /// Repeat for additional systemd unit patterns; omitted means all units.
        #[arg(long = "unit")]
        units: Vec<String>,
        /// JournalQuery JSON object, @filename, or - for stdin.
        #[arg(long, default_value = "{}")]
        query: String,
        #[arg(long)]
        follow: bool,
        #[arg(long, default_value = "journalctl")]
        executable: PathBuf,
        #[command(flatten)]
        limits: Limits,
    },
    /// Query Netdata's local otel-plugin with all native filters and fields.
    #[command(alias = "netdata")]
    Telemetry {
        /// NetdataLogQuery JSON object, @filename, or - for stdin; since is required.
        query: String,
        #[arg(long, default_value = "/usr/lib/netdata/plugins.d/otel-plugin")]
        executable: PathBuf,
        #[arg(long, default_value = "/usr/lib/netdata/conf.d/otel.yaml")]
        stock_config: PathBuf,
        #[arg(long, default_value = "/etc/netdata/otel.yaml")]
        config: PathBuf,
        #[arg(long, default_value_t = 1)]
        max_concurrency: usize,
        #[command(flatten)]
        limits: Limits,
    },
}

pub async fn run(arguments: Arguments, compact: bool) -> Result<bool> {
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let cancelled = async {
        tokio::select! {
            _ = interrupt.recv() => {},
            _ = terminate.recv() => {},
        }
    };
    tokio::pin!(cancelled);
    match arguments.command {
        Command::Journal {
            units,
            query,
            follow,
            executable,
            limits,
        } => {
            let mut value = super::input(&query)?;
            let fields = value
                .as_object_mut()
                .context("journal query must be a JSON object")?;
            if follow {
                fields.entry("direction").or_insert(json!("forward"));
                fields.entry("limit").or_insert(json!(0));
            }
            let query: JournalQuery = serde_json::from_value(value)?;
            let units = (!units.is_empty()).then_some(units.as_slice());
            let reader = JournalLogs {
                executable,
                max_record_bytes: limits.max_record_bytes,
                max_output_bytes: limits.max_output_bytes,
            };
            let duration = crate::host_operations::duration(limits.timeout)?;
            if !follow {
                super::emit(
                    &reader
                        .query_cancellable(units, &query, duration, &mut cancelled)
                        .await?,
                    compact,
                )?;
                return Ok(true);
            }

            let mut stream = reader.follow(units, &query).await?;
            let result: Result<()> = async {
                loop {
                    tokio::select! {
                        biased;
                        _ = &mut cancelled => break,
                        record = stream.next_record() => {
                            let Some(record) = record? else { break };
                            super::emit(&record, compact)?;
                        }
                    }
                }
                Ok(())
            }
            .await;
            let closed = stream.close().await;
            let diagnostics = stream.diagnostics();
            if !diagnostics.is_empty() || stream.diagnostics_truncated() {
                writeln!(
                    std::io::stderr().lock(),
                    "{}",
                    json!({
                        "diagnostics": diagnostics,
                        "diagnostics_truncated": stream.diagnostics_truncated(),
                    })
                )?;
            }
            result?;
            closed?;
        }
        Command::Telemetry {
            query,
            executable,
            stock_config,
            config,
            max_concurrency,
            limits,
        } => {
            let query: NetdataLogQuery = serde_json::from_value(super::input(&query)?)?;
            let reader = NetdataLogs::new(
                executable,
                stock_config,
                config,
                max_concurrency,
                limits.max_record_bytes,
                limits.max_output_bytes,
            )?;
            let result = reader
                .query_cancellable(
                    &query,
                    crate::host_operations::duration(limits.timeout)?,
                    &mut cancelled,
                )
                .await?;
            let mut value = serde_json::to_value(&result)?;
            value["truncated"] = json!(result.truncated());
            super::emit(&value, compact)?;
        }
    }
    Ok(true)
}
