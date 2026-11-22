//! Search projected fields for an explicitly attributed room instance.

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use futures::FutureExt;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, future::pending, time::Duration};

use super::{COVERAGE, EventFilter, valid_uid};
use crate::{
    events::{EVENT_NAMES, types::participant_fields},
    logs::{
        LogProcessError, NetdataLogQuery, NetdataLogRecord, NetdataLogResult, NetdataLogs,
        netdata::{NetdataReadFailure, userid_query},
    },
    rooms::MAX_ROOM_SLOT,
};

const NAME: &str = "event_name";
const INSTANCE: &str = "resource.attributes.service.instance.id";
const CLUSTER: &str = "resource.attributes.dst.cluster.name";
const SHARD: &str = "attributes.dst.shard.name";
const UID: &str = "attributes.log.record.uid";

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RoomStream {
    pub room: u16,
    pub service_name: String,
    pub service_namespace: String,
    pub cluster: String,
    pub instance_id: String,
    pub tenant: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TelemetrySearch {
    pub source: RoomStream,
    pub since: DateTime<Utc>,
    pub until: DateTime<Utc>,
    #[serde(default, rename = "where")]
    pub filter: EventFilter,
    #[serde(default)]
    pub fields: Vec<String>,
    pub limit: usize,
}

#[derive(Debug, Serialize)]
pub struct TelemetryHit {
    pub uid: String,
    pub event_name: String,
    pub shard: String,
    #[serde(flatten)]
    pub record: NetdataLogRecord,
}

#[derive(Debug, Serialize)]
pub struct SearchPage {
    pub hits: Vec<TelemetryHit>,
    pub matched_candidates: Option<u64>,
    pub returned_candidates: usize,
    pub distinct_uids: usize,
    pub unverified: usize,
    pub truncated: Option<bool>,
    pub error: Option<String>,
    pub file_status: &'static str,
    pub diagnostics: String,
    pub diagnostics_truncated: bool,
}

#[derive(Debug, Serialize)]
pub struct TelemetrySearchResult {
    pub selection: TelemetrySearch,
    pub since: u32,
    pub until: u32,
    pub gameplay: SearchPage,
    pub coverage: SearchPage,
}

fn unique<'a>(row: &'a NetdataLogRecord, key: &str) -> Option<&'a str> {
    let mut values = row
        .fields
        .iter()
        .filter(|(k, _)| k == key)
        .map(|(_, v)| v.as_str());
    let first = values.next()?;
    (!first.is_empty() && values.all(|v| v == first)).then_some(first)
}

fn push_exact(filters: &mut Vec<(String, String)>, key: &str, values: &[&str]) {
    if values
        .iter()
        .all(|v| !v.contains([',', '\0']) && v.trim() == *v)
    {
        filters.extend(values.iter().map(|v| (key.to_owned(), (*v).to_owned())));
    }
}

impl TelemetrySearch {
    pub async fn read(
        self,
        reader: &NetdataLogs,
        timeout: Duration,
    ) -> Result<TelemetrySearchResult> {
        self.read_cancellable(reader, timeout, pending()).await
    }

    pub async fn read_cancellable(
        self,
        reader: &NetdataLogs,
        timeout: Duration,
        cancelled: impl Future<Output = ()>,
    ) -> Result<TelemetrySearchResult> {
        ensure!(
            self.source.room <= MAX_ROOM_SLOT
                && !self.source.instance_id.trim().is_empty()
                && !self.source.cluster.is_empty(),
            "structured Netdata search requires a room and its known cluster and Agent instance"
        );
        ensure!(
            self.since < self.until,
            "Netdata search requires a positive time window"
        );
        ensure!(
            [self.since, self.until]
                .iter()
                .all(|time| time.timestamp_subsec_nanos() < 1_000_000_000),
            "Netdata Unix time bounds cannot represent leap seconds"
        );
        self.filter.validate(false)?;
        let since = self
            .since
            .timestamp()
            .try_into()
            .context("invalid Netdata start")?;
        let until = (self.until.timestamp() + i64::from(self.until.timestamp_subsec_nanos() != 0))
            .try_into()
            .context("invalid Netdata end")?;
        let mut query = NetdataLogQuery::new(since);
        query.until = Some(until);
        query.service_name = Some(self.source.service_name.clone());
        query.service_namespace = Some(self.source.service_namespace.clone());
        query.tenant = self.source.tenant.clone();
        query.limit = self.limit;
        push_exact(&mut query.filters, INSTANCE, &[&self.source.instance_id]);
        push_exact(&mut query.filters, CLUSTER, &[&self.source.cluster]);
        push_exact(
            &mut query.filters,
            SHARD,
            &self
                .filter
                .shards
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
        );
        let names: Vec<_> = if self.filter.events.is_empty() {
            EVENT_NAMES.to_vec()
        } else {
            self.filter.events.iter().map(String::as_str).collect()
        };
        let mut capture = query.clone();
        push_exact(&mut capture.filters, NAME, &[COVERAGE]);
        push_exact(&mut query.filters, NAME, &names);
        query.fields.clone_from(&self.fields);
        if let Some(userid) = &self.filter.userid {
            let roles: BTreeSet<_> = names
                .iter()
                .flat_map(|name| participant_fields(name))
                .copied()
                .collect();
            query.query = Some(userid_query(userid, roles.iter().copied()));
            if !self.fields.is_empty() {
                query
                    .fields
                    .extend(roles.into_iter().map(|field| format!("body.{field}")));
            }
        }
        if !self.fields.is_empty() {
            query
                .fields
                .extend([INSTANCE, CLUSTER, NAME, SHARD, UID].map(str::to_owned));
            query.fields.sort_unstable();
            query.fields.dedup();
        }
        query.validate()?;
        capture.validate()?;
        let cancelled = cancelled.shared();
        let (events, coverage) = tokio::join!(
            reader.query_cancellable(&query, timeout, cancelled.clone()),
            reader.query_cancellable(&capture, timeout, cancelled.clone()),
        );
        if cancelled.peek().is_some() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted,
                "Netdata search cancelled",
            )
            .into());
        }
        Ok(TelemetrySearchResult {
            since,
            until,
            gameplay: self.select(events, false),
            coverage: self.select(coverage, true),
            selection: self,
        })
    }

    fn select(&self, read: Result<NetdataLogResult>, coverage: bool) -> SearchPage {
        let raw = match read {
            Ok(result) => result,
            Err(error) => {
                let (cause, diagnostics, diagnostics_truncated) =
                    if let Some(p) = error.downcast_ref::<LogProcessError>() {
                        (
                            format!("Netdata plugin exited with status {}", p.returncode),
                            p.diagnostics.clone(),
                            p.diagnostics_truncated,
                        )
                    } else if let Some(read) = error.downcast_ref::<NetdataReadFailure>() {
                        (read.cause.clone(), read.diagnostics.clone(), read.truncated)
                    } else {
                        (format!("{error:#}"), String::new(), false)
                    };
                return SearchPage {
                    hits: vec![],
                    returned_candidates: 0,
                    matched_candidates: None,
                    distinct_uids: 0,
                    unverified: 0,
                    truncated: None,
                    file_status: "unknown",
                    error: Some(cause),
                    diagnostics,
                    diagnostics_truncated,
                };
            }
        };
        let file_status = raw.file_status();
        let truncated = raw.truncated();
        let mut unverified = 0;
        let mut hits = Vec::new();
        let returned_candidates = raw.records.len();
        let lower = self
            .since
            .timestamp_nanos_opt()
            .expect("validated u32 Unix seconds") as u64;
        let upper = self
            .until
            .timestamp_nanos_opt()
            .expect("validated u32 Unix seconds") as u64;
        for mut record in raw.records {
            if !(lower..upper).contains(&record.timestamp_ns) {
                continue;
            }
            let (Some(instance), Some(cluster), Some(name), Some(shard), Some(uid)) = (
                unique(&record, INSTANCE),
                unique(&record, CLUSTER),
                unique(&record, NAME),
                unique(&record, SHARD),
                unique(&record, UID),
            ) else {
                unverified += 1;
                continue;
            };
            if !valid_uid(uid) {
                unverified += 1;
                continue;
            }
            if instance != self.source.instance_id
                || cluster != self.source.cluster
                || !self.filter.selects_shard(shard)
            {
                continue;
            }
            if (coverage && name == COVERAGE) || (!coverage && self.filter.selects_name(name)) {
                if !coverage && let Some(userid) = &self.filter.userid {
                    // The native regex selected this row. Verify its witness belongs to
                    // this event; only array roles may carry several different IDs.
                    let witnessed = participant_fields(name).iter().any(|field| {
                        let key = format!("body.{field}");
                        if field.contains("[]") {
                            record.fields.iter().any(|(k, v)| k == &key && v == userid)
                        } else {
                            unique(&record, &key) == Some(userid)
                        }
                    });
                    if !witnessed {
                        unverified += 1;
                        continue;
                    }
                }
                let (uid, event_name, shard) = (uid.to_owned(), name.to_owned(), shard.to_owned());
                if !coverage && !self.fields.is_empty() {
                    record.fields.retain(|(k, _)| self.fields.contains(k));
                }
                hits.push(TelemetryHit {
                    uid,
                    event_name,
                    shard,
                    record,
                });
            }
        }
        let distinct_uids = hits.iter().map(|h| &h.uid).collect::<BTreeSet<_>>().len();
        SearchPage {
            hits,
            returned_candidates,
            matched_candidates: raw.matched,
            unverified,
            distinct_uids,
            truncated,
            file_status,
            error: None,
            diagnostics: raw.diagnostics,
            diagnostics_truncated: raw.diagnostics_truncated,
        }
    }
}
