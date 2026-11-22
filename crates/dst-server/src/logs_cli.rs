//! CLI adapters for the shared local log readers.

use std::{io::Write, path::PathBuf};

use anyhow::Result;
use clap::{Args, Subcommand};
use serde_json::json;

use crate::logs::history::{EventFilter, HistoryQuery, HistorySelection};
use crate::logs::{JournalLogs, JournalQuery, JournalUnits, NetdataLogQuery, NetdataLogs};

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

#[derive(Args)]
struct History {
    /// Managed room number, comma-separated list or range. Repeat to add rooms.
    #[arg(long = "room", required = true)]
    rooms: Vec<String>,
    /// Inclusive RFC 3339 journal receipt time.
    #[arg(long)]
    since: chrono::DateTime<chrono::Utc>,
    /// Exclusive RFC 3339 journal receipt time.
    #[arg(long)]
    until: chrono::DateTime<chrono::Utc>,
    #[arg(long)]
    user: bool,
    #[arg(long)]
    namespace: Option<String>,
    #[arg(long = "event")]
    events: Vec<String>,
    #[arg(long = "shard")]
    shards: Vec<String>,
    /// Exact participant ID in any recorded role.
    #[arg(long)]
    userid: Option<String>,
    #[arg(long, conflicts_with_all = ["events", "userid"])]
    coverage_only: bool,
    /// Maximum raw candidates scanned in this page.
    #[arg(long, default_value_t = 500)]
    page_size: usize,
    /// A raw cursor from the same selection.
    #[arg(long, conflicts_with = "checkpoint")]
    cursor: Option<String>,
    /// Resume a saved checkpoint, given as JSON, @filename or - for stdin.
    #[arg(long)]
    checkpoint: Option<String>,
    #[arg(long, default_value = "journalctl")]
    executable: PathBuf,
    #[command(flatten)]
    limits: Limits,
}

#[derive(Subcommand)]
enum Command {
    /// Read one page of validated gameplay and capture evidence from managed room journals.
    History(History),
    /// Query or follow journalctl records, retaining native fields and cursors.
    Journal {
        /// Match a system unit across readable journals. Repeat for additional patterns.
        #[arg(long = "unit")]
        units: Vec<String>,
        /// Select units belonging to the current user's manager. Without units select that user's journal.
        #[arg(long)]
        user: bool,
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
    Telemetry {
        /// NetdataLogQuery JSON object, @filename, or - for stdin; since is required.
        query: String,
        /// Accept a room instance, exact time bounds and gameplay predicate.
        #[arg(long)]
        structured: bool,
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
    // Keep the default signal action while synchronous input waits for EOF.
    let value = match &arguments.command {
        Command::History(args) => args
            .checkpoint
            .as_deref()
            .map(super::input)
            .transpose()?
            .unwrap_or(serde_json::Value::Null),
        Command::Journal { query, .. } | Command::Telemetry { query, .. } => super::input(query)?,
    };
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
        Command::History(args) => {
            let query = HistoryQuery {
                selection: HistorySelection {
                    rooms: super::host_cli::numbers(&args.rooms)?.into_iter().collect(),
                    since: args.since,
                    until: args.until,
                    user: args.user,
                    namespace: args.namespace,
                    coverage_only: args.coverage_only,
                    filter: EventFilter {
                        events: args.events.into_iter().collect(),
                        shards: args.shards.into_iter().collect(),
                        userid: args.userid,
                    },
                },
                cursor: args.cursor,
                page_size: args.page_size,
                checkpoint: serde_json::from_value(value)?,
            };
            let reader = JournalLogs {
                executable: args.executable,
                max_record_bytes: args.limits.max_record_bytes,
                max_output_bytes: args.limits.max_output_bytes,
            };
            let page = query
                .read(
                    &reader,
                    crate::host_operations::duration(args.limits.timeout)?,
                    &mut cancelled,
                )
                .await?;
            super::emit(&page, compact)?;
        }
        Command::Journal {
            units,
            user,
            follow,
            executable,
            limits,
            ..
        } => {
            let query = JournalQuery::from_value(value)?;
            let units = Some(JournalUnits::for_manager(
                user,
                (!units.is_empty()).then_some(units.as_slice()),
            ));
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
            structured,
            executable,
            stock_config,
            config,
            max_concurrency,
            limits,
            ..
        } => {
            let reader = NetdataLogs::new(
                executable,
                stock_config,
                config,
                max_concurrency,
                limits.max_record_bytes,
                limits.max_output_bytes,
            )?;
            if structured {
                let search: crate::logs::history::TelemetrySearch = serde_json::from_value(value)?;
                let timeout = crate::host_operations::duration(limits.timeout)?;
                let result = search
                    .read_cancellable(&reader, timeout, &mut cancelled)
                    .await?;
                super::emit(&result, compact)?;
                return Ok(result.gameplay.error.is_none() && result.coverage.error.is_none());
            }
            let query: NetdataLogQuery = serde_json::from_value(value)?;
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
