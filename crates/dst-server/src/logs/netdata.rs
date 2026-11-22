//! Query the local otel-plugin and verify its result window and counts.

use super::{EVENT_USERID_FIELDS, LogOutput, OUTPUT_BYTES, RECORD_BYTES};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    ffi::OsString,
    future::{Future, pending},
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Semaphore;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetdataLogQuery {
    #[serde(default)]
    pub tenant: Option<String>,
    pub since: u32,
    #[serde(default)]
    pub until: Option<u32>,
    #[serde(default)]
    pub service_name: Option<String>,
    #[serde(default)]
    pub service_namespace: Option<String>,
    #[serde(default)]
    pub filters: Vec<(String, String)>,
    #[serde(default)]
    pub query: Option<String>,
    #[serde(default)]
    pub userid: Option<String>,
    #[serde(default)]
    pub fields: Vec<String>,
    #[serde(default = "netdata_limit")]
    pub limit: usize,
}
fn netdata_limit() -> usize {
    200
}

pub(super) fn userid_query<'a>(userid: &str, fields: impl IntoIterator<Item = &'a str>) -> String {
    let roles = fields
        .into_iter()
        .map(regex::escape)
        .collect::<Vec<_>>()
        .join("|");
    if roles.is_empty() {
        // Every searchable pair contains '='. This event selection has no participant roles.
        return "^$".into();
    }
    // OS arguments cannot contain NUL, but the regex can match it literally.
    let userid = regex::escape(userid).replace('\0', r"\x00");
    format!("^body\\.(?:{roles})={userid}$")
}
impl NetdataLogQuery {
    pub fn new(since: u32) -> Self {
        Self {
            tenant: None,
            since,
            until: None,
            service_name: None,
            service_namespace: None,
            filters: Vec::new(),
            query: None,
            userid: None,
            fields: Vec::new(),
            limit: 200,
        }
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(self.limit > 0, "Netdata limit must be positive");
        if let Some(tenant) = &self.tenant {
            ensure!(
                !tenant.is_empty()
                    && !matches!(tenant.as_str(), "." | "..")
                    && !tenant.contains(['/', '\\', '\0']),
                "Netdata tenant must be one directory name"
            );
        }
        if let Some(userid) = &self.userid {
            crate::events::types::validate_userid(userid)?;
            ensure!(
                self.query.is_none(),
                "Netdata userid and query cannot be combined"
            );
        }
        ensure!(
            self.until.is_none_or(|until| until > self.since),
            "Netdata query until must be later than since"
        );
        ensure!(
            self.service_namespace.is_none() || self.service_name.is_some(),
            "Netdata service namespace requires a service name"
        );
        for value in [&self.service_name, &self.query].into_iter().flatten() {
            ensure!(!value.is_empty(), "Netdata arguments cannot be empty");
        }
        for value in [&self.service_name, &self.service_namespace, &self.query]
            .into_iter()
            .flatten()
        {
            ensure!(
                !value.contains('\0'),
                "Netdata arguments cannot contain NUL"
            );
        }
        for (field, value) in &self.filters {
            ensure!(
                !field.is_empty() && field == field.trim() && value == value.trim(),
                "Netdata filters cannot be empty or encode surrounding whitespace"
            );
            ensure!(
                !field.contains([',', '=', '~', '\0']),
                "Netdata filter fields cannot contain comma, '=', '~' or NUL"
            );
            ensure!(
                !value.contains([',', '\0']),
                "Netdata filter values cannot contain comma or NUL"
            );
        }
        for field in &self.fields {
            ensure!(
                !field.is_empty() && field == field.trim(),
                "Netdata fields cannot be empty or encode surrounding whitespace"
            );
            ensure!(
                !field.contains([',', '\0']),
                "Netdata fields cannot contain comma or NUL"
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetdataLogRecord {
    pub timestamp_ns: u64,
    pub fields: Vec<(String, String)>,
}
impl NetdataLogRecord {
    pub fn values(&self, key: &str) -> Vec<&str> {
        self.fields
            .iter()
            .filter(|(field, _)| field == key)
            .map(|(_, value)| value.as_str())
            .collect()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct NetdataLogResult {
    pub records: Vec<NetdataLogRecord>,
    pub matched: Option<u64>,
    pub since: u32,
    pub until: u32,
    pub diagnostics: String,
    pub diagnostics_truncated: bool,
    file_status: &'static str,
}
impl NetdataLogResult {
    pub fn file_status(&self) -> &'static str {
        self.file_status
    }

    pub fn truncated(&self) -> Option<bool> {
        self.matched
            .map(|matched| matched > self.records.len() as u64)
    }
}

#[derive(Debug, thiserror::Error)]
#[error("{cause}: {diagnostics}")]
pub(super) struct NetdataReadFailure {
    pub cause: String,
    pub diagnostics: String,
    pub truncated: bool,
}

pub struct NetdataLogs {
    executable: PathBuf,
    stock_config: PathBuf,
    config: PathBuf,
    max_record_bytes: usize,
    max_output_bytes: usize,
    semaphore: Arc<Semaphore>,
}
impl Default for NetdataLogs {
    fn default() -> Self {
        Self::new(
            "/usr/lib/netdata/plugins.d/otel-plugin".into(),
            "/usr/lib/netdata/conf.d/otel.yaml".into(),
            "/etc/netdata/otel.yaml".into(),
            1,
            RECORD_BYTES,
            OUTPUT_BYTES,
        )
        .expect("valid default Netdata configuration")
    }
}
impl NetdataLogs {
    pub fn new(
        executable: PathBuf,
        stock_config: PathBuf,
        config: PathBuf,
        max_concurrency: usize,
        max_record_bytes: usize,
        max_output_bytes: usize,
    ) -> Result<Self> {
        use std::os::unix::ffi::OsStrExt;
        for path in [&executable, &stock_config, &config] {
            ensure!(
                !path.as_os_str().is_empty() && !path.as_os_str().as_bytes().contains(&0),
                "Netdata paths must be nonempty and cannot contain NUL"
            );
        }
        ensure!(
            max_concurrency > 0 && max_concurrency <= Semaphore::MAX_PERMITS,
            "Netdata concurrency must be a positive supported integer"
        );
        ensure!(
            max_record_bytes > 0 && max_output_bytes > 0,
            "log output byte limits must be positive integers"
        );
        Ok(Self {
            executable,
            stock_config,
            config,
            max_record_bytes,
            max_output_bytes,
            semaphore: Arc::new(Semaphore::new(max_concurrency)),
        })
    }

    pub fn command(&self, request: &NetdataLogQuery) -> Result<Vec<OsString>> {
        request.validate()?;
        let mut stock = OsString::from("--stock-config=");
        stock.push(&self.stock_config);
        let mut config = OsString::from("--config=");
        config.push(&self.config);
        let mut command = vec![
            self.executable.clone().into_os_string(),
            "logs".into(),
            stock,
            config,
            format!("--since={}", request.since).into(),
        ];
        if let Some(until) = request.until {
            command.push(format!("--until={until}").into());
        }
        for (name, value) in [
            ("tenant", &request.tenant),
            ("name", &request.service_name),
            ("namespace", &request.service_namespace),
            ("query", &request.query),
        ] {
            if let Some(value) = value {
                command.push(format!("--{name}={value}").into());
            }
        }
        if let Some(userid) = &request.userid {
            let query = userid_query(userid, EVENT_USERID_FIELDS.iter().copied());
            command.push(format!("--query={query}").into());
        }
        if !request.filters.is_empty() {
            command.push(
                format!(
                    "--filter={}",
                    request
                        .filters
                        .iter()
                        .map(|(key, value)| format!("{key}={value}"))
                        .collect::<Vec<_>>()
                        .join(",")
                )
                .into(),
            );
        }
        if !request.fields.is_empty() {
            command.push(format!("--fields={}", request.fields.join(",")).into());
        }
        command.extend([
            format!("--limit={}", request.limit).into(),
            "--output=ndjson".into(),
        ]);
        Ok(command)
    }

    pub async fn query(
        &self,
        request: &NetdataLogQuery,
        completion_timeout: Duration,
    ) -> Result<NetdataLogResult> {
        self.query_cancellable(request, completion_timeout, pending())
            .await
    }

    /// Cancellation includes waiting for a slot and confirms any child is reaped.
    pub async fn query_cancellable(
        &self,
        request: &NetdataLogQuery,
        completion_timeout: Duration,
        cancelled: impl Future<Output = ()>,
    ) -> Result<NetdataLogResult> {
        ensure!(
            !completion_timeout.is_zero(),
            "log completion timeout must be positive"
        );
        let mut effective = request.clone();
        if effective.until.is_none() {
            effective.until = Some(
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)?
                    .as_secs()
                    .checked_add(1)
                    .and_then(|seconds| u32::try_from(seconds).ok())
                    .context("Netdata query time must fit unsigned 32-bit Unix seconds")?,
            );
        }
        let command = self.command(&effective)?;
        let until = effective.until.expect("effective query has an end");
        // The deadline includes waiting for a concurrency slot, and `until` is
        // fixed before that wait so every result describes its actual window.
        let deadline = tokio::time::Instant::now()
            .checked_add(completion_timeout)
            .context("log completion timeout is too large")?;
        tokio::pin!(cancelled);
        let permit = tokio::select! {
            biased;
            _ = &mut cancelled => return Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted, "Netdata query cancelled",
            ).into()),
            permit = tokio::time::timeout_at(deadline, self.semaphore.clone().acquire_owned()) => {
                permit.context("Netdata query timed out")??
            }
        };
        let mut output = LogOutput::spawn(
            command,
            self.max_record_bytes,
            Some(self.max_output_bytes),
            None,
            Some(permit),
        )?;
        let result = tokio::select! {
            biased;
            _ = &mut cancelled => Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted, "Netdata query cancelled",
            ).into()),
            result = tokio::time::timeout_at(deadline, async {
                let mut records = Vec::new();
                while let Some(line) = output.next_line().await? {
                    ensure!(
                        records.len() < effective.limit,
                        "Netdata returned more records than the requested limit"
                    );
                    records.push(
                        serde_json::from_slice::<NetdataLogRecord>(&line).with_context(|| {
                            format!(
                                "invalid Netdata NDJSON record on line {}",
                                records.len() + 1
                            )
                        })?,
                    );
                }
                let diagnostics = output.diagnostics();
                let diagnostics_truncated = output.diagnostics_truncated();
                let (matched, file_status) = read_summary(
                    &effective,
                    records.len(),
                    &diagnostics,
                    diagnostics_truncated,
                )?;
                Ok(NetdataLogResult {
                    records,
                    matched,
                    since: effective.since,
                    until,
                    diagnostics,
                    diagnostics_truncated,
                    file_status,
                })
            }) => result.context("Netdata query timed out").and_then(|result| result),
        };
        let closed = output.close().await;
        result
            .and_then(|result| closed.map(|()| result))
            .map_err(|error| {
                let diagnostics = output.diagnostics();
                if error.is::<super::LogProcessError>()
                    || (diagnostics.is_empty() && !output.diagnostics_truncated())
                {
                    return error;
                }
                let failure = NetdataReadFailure {
                    cause: format!("{error:#}"),
                    diagnostics,
                    truncated: output.diagnostics_truncated(),
                };
                error.context(failure)
            })
    }
}

fn summary(line: &str) -> Option<[u64; 4]> {
    let (matched, rest) = line.strip_prefix("matched=")?.split_once(" returned=")?;
    let (returned, window) = rest.split_once(" window=")?;
    let (since, until) = window.split_once("..")?;
    let mut numbers = [0; 4];
    for (number, text) in numbers.iter_mut().zip([matched, returned, since, until]) {
        if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        *number = text.parse().ok()?;
    }
    Some(numbers)
}

fn read_summary(
    request: &NetdataLogQuery,
    returned: usize,
    diagnostics: &str,
    truncated: bool,
) -> Result<(Option<u64>, &'static str)> {
    let status = |clean, extra_diagnostics| {
        if truncated {
            "unknown"
        } else if extra_diagnostics {
            "diagnostics"
        } else {
            clean
        }
    };
    if let Some(until) = request.until {
        let stream = request
            .service_name
            .as_ref()
            .map(|name| {
                format!(
                    ", stream={}/{name}",
                    request.service_namespace.as_deref().unwrap_or_default()
                )
            })
            .unwrap_or_default();
        let empty = format!(
            "no WAL/SFST files matched (tenant={}, window={}..{until}{stream})",
            request.tenant.as_deref().unwrap_or("default"),
            request.since
        );
        if returned == 0
            && let Some(prefix) = diagnostics.strip_suffix(&empty)
        {
            ensure!(
                prefix.is_empty() || prefix.ends_with('\n'),
                "Netdata did not return a valid query summary"
            );
            return Ok((Some(0), status("no_files", !prefix.trim().is_empty())));
        }
    }
    let mut summaries = diagnostics.lines().filter_map(summary);
    if let Some([matched, count, since, until]) = summaries.next() {
        ensure!(
            summaries.next().is_none(),
            "Netdata returned multiple query summaries"
        );
        ensure!(
            count == returned as u64,
            "Netdata summary returned count does not match its records"
        );
        ensure!(
            since == u64::from(request.since) && Some(until) == request.until.map(u64::from),
            "Netdata summary window does not match the query"
        );
        let clean = if returned == 0 {
            "files_found"
        } else {
            "observed"
        };
        let extra_diagnostics = diagnostics
            .lines()
            .filter(|line| !line.trim().is_empty())
            .count()
            > 1;
        return Ok((
            (matched >= count).then_some(matched),
            status(clean, extra_diagnostics),
        ));
    }
    if truncated {
        return Ok((None, "unknown"));
    }
    bail!("Netdata did not return a valid query summary")
}
