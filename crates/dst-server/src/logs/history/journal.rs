//! Bounded journal selection, source attribution and portable page results.

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, TimeDelta, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, fs, future::Future, path::Path, time::Duration};

use super::{
    EventFilter, Observation, PREFIX, Rejection, candidate_pattern, decode_message, room_writer,
};
use crate::{
    host::Host,
    logs::{Direction, JournalLogs, JournalQuery, JournalRecord, JournalUnits},
    rooms::MAX_ROOM_SLOT,
};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistorySelection {
    pub rooms: BTreeSet<u16>,
    pub since: DateTime<Utc>,
    pub until: DateTime<Utc>,
    #[serde(default)]
    pub user: bool,
    pub namespace: Option<String>,
    #[serde(default)]
    pub coverage_only: bool,
    #[serde(default, rename = "where")]
    pub filter: EventFilter,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoryCheckpoint {
    pub scope: String,
    pub cursor: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct JournalPosition {
    pub unit: String,
    pub cursor: String,
    pub receipt_timestamp_us: u64,
}

#[derive(Debug, Serialize)]
pub struct JournalObservation {
    #[serde(flatten)]
    pub position: JournalPosition,
    #[serde(flatten)]
    pub observation: Observation,
}

#[derive(Debug, Serialize)]
pub struct RejectedEntry {
    #[serde(flatten)]
    pub position: JournalPosition,
    pub reason: Rejection,
}

#[derive(Debug, Serialize)]
pub struct HistoryReader {
    pub host_id: String,
    pub host_id_kind: &'static str,
    pub effective_uid: u32,
    pub effective_gid: u32,
    pub groups: BTreeSet<u32>,
    pub executable: String,
}

#[derive(Debug, Serialize)]
pub struct HistoryPage {
    pub reader: HistoryReader,
    pub selection: HistorySelection,
    pub observations: Vec<JournalObservation>,
    pub rejections: Vec<RejectedEntry>,
    pub visible_units: BTreeSet<String>,
    pub candidates_read: usize,
    pub next_cursor: Option<String>,
    pub checkpoint: HistoryCheckpoint,
    pub has_more: bool,
    pub diagnostics: String,
    pub diagnostics_truncated: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoryQuery {
    pub selection: HistorySelection,
    pub cursor: Option<String>,
    pub checkpoint: Option<HistoryCheckpoint>,
    pub page_size: usize,
}

fn has_frame_prefix(field: &Value) -> bool {
    match field {
        Value::String(text) => text.starts_with(PREFIX),
        Value::Array(items) => {
            let bytes = items.iter().take(PREFIX.len()).map(Value::as_u64);
            bytes.eq(PREFIX.bytes().map(|b| Some(u64::from(b))))
                || items.iter().any(has_frame_prefix)
        }
        _ => false,
    }
}

pub fn decode_journal_field(
    field: Option<&Value>,
    filter: &EventFilter,
    coverage_only: bool,
) -> Result<Option<Observation>, Rejection> {
    let Some(field) = field else { return Ok(None) };
    if let Some(text) = JournalRecord::message_utf8(field) {
        decode_message(&text, filter, coverage_only)
    } else if has_frame_prefix(field) {
        Err(Rejection::InvalidEnvelope)
    } else {
        Ok(None)
    }
}

fn reader_scope(
    selection: &HistorySelection,
    executable: &Path,
) -> Result<(HistoryReader, String)> {
    let (host_id_kind, host_id) = match fs::read_to_string("/etc/machine-id")
        .ok()
        .filter(|id| !id.trim().is_empty() && id.trim() != "uninitialized")
    {
        Some(id) => ("machine_id", id),
        None => (
            "boot_id",
            fs::read_to_string("/proc/sys/kernel/random/boot_id")?,
        ),
    };
    let status = fs::read_to_string("/proc/thread-self/status")?;
    let field = |key| {
        status
            .lines()
            .find_map(|line| line.strip_prefix(key))
            .context("missing journal reader identity")
    };
    let effective = |ids: &str| -> Result<u32> {
        Ok(ids
            .split_whitespace()
            .nth(1)
            .context("missing effective journal reader identity")?
            .parse()?)
    };
    let uids = field("Uid:")?;
    let gids = field("Gid:")?;
    let reader = HistoryReader {
        host_id: host_id.trim().to_owned(),
        host_id_kind,
        effective_uid: effective(uids)?,
        effective_gid: effective(gids)?,
        groups: field("Groups:")?
            .split_whitespace()
            .map(str::parse)
            .collect::<Result<_, _>>()?,
        executable: executable.to_string_lossy().into_owned(),
    };
    let mut digest = Sha256::new();
    digest.update(b"dst-server/journal-history/2\0");
    digest.update(serde_json::to_vec(&reader)?);
    digest.update(uids.as_bytes());
    digest.update(gids.as_bytes());
    if executable.is_relative() {
        let path = std::env::var_os("PATH").unwrap_or_default();
        let uses_path = !executable.as_os_str().as_encoded_bytes().contains(&b'/');
        if uses_path {
            digest.update(path.as_encoded_bytes());
        }
        if !uses_path || std::env::split_paths(&path).any(|dir| dir.is_relative()) {
            digest.update([0]);
            digest.update(std::env::current_dir()?.as_os_str().as_encoded_bytes());
        }
    }
    digest.update([0]);
    digest.update(executable.as_os_str().as_encoded_bytes());
    digest.update([0]);
    digest.update(serde_json::to_vec(selection)?);
    let scope = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok((reader, scope))
}

impl HistoryQuery {
    pub async fn read(
        self,
        reader: &JournalLogs,
        timeout: Duration,
        cancelled: impl Future<Output = ()>,
    ) -> Result<HistoryPage> {
        let selection = self.selection;
        ensure!(
            !selection.rooms.is_empty() && selection.rooms.iter().all(|r| *r <= MAX_ROOM_SLOT),
            "select rooms between 000 and 299"
        );
        ensure!(
            selection.since.timestamp_subsec_nanos() < 1_000_000_000
                && selection.until.timestamp_subsec_nanos() < 1_000_000_000,
            "journal history cannot represent leap-second time bounds"
        );
        ensure!(
            self.page_size > 0
                && selection.since < selection.until
                && selection.since.timestamp() >= 0,
            "history requires a positive UTC window and page size"
        );
        if let Some(ns) = &selection.namespace {
            ensure!(
                !ns.is_empty() && !ns.starts_with('+') && !ns.contains('*'),
                "history requires one journal namespace"
            );
        }
        selection.filter.validate(selection.coverage_only)?;
        let (source, scope) = reader_scope(&selection, &reader.executable)?;
        let cursor = if let Some(saved) = self.checkpoint {
            ensure!(
                self.cursor.is_none() && saved.scope == scope,
                "checkpoint belongs to a different history selection or reader"
            );
            saved.cursor
        } else {
            self.cursor
        };
        let until = selection
            .until
            .checked_sub_signed(TimeDelta::nanoseconds(1))
            .context("history end out of range")?;
        let stamp = |time: DateTime<Utc>| time.format("%Y-%m-%d %H:%M:%S%.6f UTC").to_string();
        let request = JournalQuery {
            limit: Some(self.page_size),
            direction: Some(Direction::Forward),
            namespace: selection.namespace.clone(),
            since: cursor.is_none().then(|| stamp(selection.since)),
            until: Some(stamp(until)),
            grep: Some(candidate_pattern(selection.coverage_only).to_owned()),
            cursor: cursor.clone(),
        };
        let units: Vec<_> = selection.rooms.iter().copied().map(Host::unit).collect();
        let raw = reader
            .query_cancellable(
                Some(JournalUnits::for_manager(selection.user, Some(&units))),
                &request,
                timeout,
                cancelled,
            )
            .await?;
        let next_cursor = raw.next_cursor.or(cursor);
        let mut page = HistoryPage {
            reader: source,
            candidates_read: raw.records.len(),
            has_more: raw.has_more,
            diagnostics: raw.diagnostics,
            diagnostics_truncated: raw.diagnostics_truncated,
            checkpoint: HistoryCheckpoint {
                scope,
                cursor: next_cursor.clone(),
            },
            next_cursor,
            observations: Vec::new(),
            rejections: Vec::new(),
            visible_units: BTreeSet::new(),
            selection,
        };
        for entry in raw.records {
            let Some(unit) = entry.emitting_unit(page.selection.user) else {
                continue;
            };
            if !room_writer(
                unit,
                entry.fields.get("_UID").and_then(serde_json::Value::as_str),
                page.selection.user,
            )
            .is_some_and(|n| page.selection.rooms.contains(&n))
            {
                continue;
            }
            let receipt = entry.timestamp_us();
            let time = DateTime::from_timestamp_micros(i64::try_from(receipt)?)
                .context("invalid journal receipt time")?;
            if time < page.selection.since || time >= page.selection.until {
                continue;
            }
            page.visible_units.insert(unit.to_owned());
            let position = JournalPosition {
                unit: unit.to_owned(),
                cursor: entry.cursor().to_owned(),
                receipt_timestamp_us: receipt,
            };
            match decode_journal_field(
                entry.fields.get("MESSAGE"),
                &page.selection.filter,
                page.selection.coverage_only,
            ) {
                Ok(Some(observation)) => page.observations.push(JournalObservation {
                    position,
                    observation,
                }),
                Err(reason) => page.rejections.push(RejectedEntry { position, reason }),
                Ok(None) => (),
            }
        }
        Ok(page)
    }
}
