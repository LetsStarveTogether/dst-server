//! Select journal files and units, decode native fields and page by cursor.

use super::{Diagnostics, LogOutput, LogProcessError, OUTPUT_BYTES, RECORD_BYTES};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::{
    borrow::Cow,
    ffi::OsString,
    future::{Future, pending},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::time::timeout;

#[derive(Debug, thiserror::Error)]
#[error("journal cursor is unavailable for the selected query")]
pub struct JournalCursorError {
    pub cursor: String,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Direction {
    Forward,
    #[default]
    Backward,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct JournalQuery {
    pub limit: Option<usize>,
    pub direction: Option<Direction>,
    pub cursor: Option<String>,
    pub since: Option<String>,
    pub until: Option<String>,
    pub namespace: Option<String>,
    pub grep: Option<String>,
}
impl JournalQuery {
    pub fn follow() -> Self {
        Self {
            direction: Some(Direction::Forward),
            ..Self::default()
        }
    }
    pub fn from_value(value: Value) -> Result<Self> {
        ensure!(value.is_object(), "journal query must be a JSON object");
        Ok(serde_json::from_value(value)?)
    }
    fn page_size(&self) -> usize {
        self.limit.unwrap_or(100)
    }
    fn page_lines(&self) -> Result<usize> {
        self.page_size()
            .checked_add(1 + usize::from(self.cursor.is_some()))
            .context("journal limit is too large")
    }
    pub fn validate(&self) -> Result<()> {
        for value in [
            &self.cursor,
            &self.since,
            &self.until,
            &self.namespace,
            &self.grep,
        ]
        .into_iter()
        .flatten()
        {
            ensure!(
                !value.trim().is_empty() && !value.contains(['\0', '\r', '\n']),
                "journal query values cannot be empty or contain NUL or newline"
            );
        }
        ensure!(
            self.cursor.is_none() || self.since.is_none(),
            "journal cursor and since cannot be combined"
        );
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(try_from = "JournalFields")]
pub struct JournalRecord {
    pub fields: Map<String, Value>,
}
#[derive(Deserialize)]
struct JournalFields {
    fields: Map<String, Value>,
}
impl TryFrom<JournalFields> for JournalRecord {
    type Error = anyhow::Error;
    fn try_from(value: JournalFields) -> Result<Self> {
        Self::from_fields(value.fields)
    }
}
impl JournalRecord {
    pub fn from_fields(fields: Map<String, Value>) -> Result<Self> {
        for value in fields.values() {
            field_text(value)?;
        }
        let cursor = fields
            .get("__CURSOR")
            .and_then(Value::as_str)
            .context("invalid journal record metadata")?;
        ensure!(
            !cursor.is_empty() && !cursor.contains(['\0', '\r', '\n']),
            "invalid journal record metadata"
        );
        let timestamp = fields
            .get("__REALTIME_TIMESTAMP")
            .and_then(Value::as_str)
            .context("invalid journal record metadata")?;
        ensure!(
            !timestamp.is_empty() && timestamp.bytes().all(|byte| byte.is_ascii_digit()),
            "invalid journal record metadata"
        );
        let timestamp: u64 = timestamp
            .parse()
            .context("journal timestamp is outside the supported datetime range")?;
        ensure!(
            timestamp <= 253_402_300_799_999_999,
            "journal timestamp is outside the supported datetime range"
        );
        Ok(Self { fields })
    }
    pub fn cursor(&self) -> &str {
        self.fields
            .get("__CURSOR")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }
    pub fn timestamp_us(&self) -> u64 {
        self.fields
            .get("__REALTIME_TIMESTAMP")
            .and_then(Value::as_str)
            .and_then(|value| value.parse().ok())
            .unwrap_or_default()
    }
    pub fn emitting_unit(&self, user: bool) -> Option<&str> {
        self.fields
            .get(if user {
                "_SYSTEMD_USER_UNIT"
            } else {
                "_SYSTEMD_UNIT"
            })?
            .as_str()
            .filter(|s| !s.is_empty())
    }

    pub fn unit(&self) -> String {
        ["UNIT", "_SYSTEMD_USER_UNIT", "_SYSTEMD_UNIT"]
            .iter()
            .filter_map(|key| self.fields.get(*key))
            .find(|value| match value {
                Value::Null => false,
                Value::String(text) => !text.is_empty(),
                Value::Array(items) => !items.is_empty(),
                _ => true,
            })
            .map(|value| field_text(value).unwrap_or_default())
            .unwrap_or_default()
    }
    pub fn message(&self) -> String {
        self.fields
            .get("MESSAGE")
            .map(|value| field_text(value).unwrap_or_default())
            .unwrap_or_default()
    }

    /// Decode a single MESSAGE field without altering bytes or joining repeated fields.
    pub fn message_utf8(value: &Value) -> Option<Cow<'_, str>> {
        match value {
            Value::String(text) => Some(Cow::Borrowed(text)),
            Value::Array(items) => String::from_utf8(field_bytes(items)?).ok().map(Cow::Owned),
            _ => None,
        }
    }
}

fn field_bytes(items: &[Value]) -> Option<Vec<u8>> {
    items
        .iter()
        .map(|v| u8::try_from(v.as_u64()?).ok())
        .collect()
}

fn field_text(value: &Value) -> Result<String> {
    match value {
        Value::Null => Ok(String::new()),
        Value::String(text) => Ok(text.clone()),
        Value::Array(items) if items.iter().all(|item| item.is_u64() || item.is_i64()) => {
            let bytes = field_bytes(items).context("invalid journal field value")?;
            Ok(String::from_utf8_lossy(&bytes).into_owned())
        }
        Value::Array(items) => Ok(items
            .iter()
            .map(field_text)
            .collect::<Result<Vec<_>>>()?
            .join("\n")),
        _ => bail!("invalid journal field value"),
    }
}

fn valid_unit(unit: &str) -> bool {
    if unit.is_empty() || unit.len() > 255 || unit.starts_with(['-', '.']) {
        return false;
    }
    let mut bytes = unit.bytes();
    while let Some(byte) = bytes.next() {
        if byte.is_ascii_alphanumeric() || b":_.@*?[]-".contains(&byte) {
            continue;
        }
        if byte != b'\\' || bytes.next() != Some(b'x') {
            return false;
        }
        let (Some(first), Some(second)) = (bytes.next(), bytes.next()) else {
            return false;
        };
        if !b"0123456789abcdef".contains(&first)
            || !b"0123456789abcdef".contains(&second)
            || (first == b'0' && second == b'0')
        {
            return false;
        }
    }
    true
}

#[derive(Debug, Clone, Serialize)]
pub struct JournalResult {
    pub records: Vec<JournalRecord>,
    pub next_cursor: Option<String>,
    pub has_more: bool,
    pub diagnostics: String,
    pub diagnostics_truncated: bool,
}

#[derive(Clone, Copy)]
pub enum JournalUnits<'a> {
    System(&'a [String]),
    User(&'a [String]),
    All { user: bool },
}

impl<'a> JournalUnits<'a> {
    pub fn for_manager(user: bool, units: Option<&'a [String]>) -> Self {
        match units {
            Some(units) if user => Self::User(units),
            Some(units) => Self::System(units),
            None => Self::All { user },
        }
    }

    fn append_to(self, command: &mut Vec<OsString>) -> Result<()> {
        let (flag, patterns) = match self {
            Self::All { user } => {
                command.push(if user { "--user" } else { "--system" }.into());
                return Ok(());
            }
            // With SplitMode=uid, a system unit's stdout can be in a user's journal.
            Self::System(units) => ("--unit", units),
            Self::User(units) => ("--user-unit", units),
        };
        ensure!(
            !patterns.is_empty(),
            "journal logs require at least one explicit unit pattern"
        );
        let mut seen = std::collections::HashSet::new();
        for unit in patterns {
            ensure!(valid_unit(unit), "invalid journal unit pattern");
            if seen.insert(unit) {
                command.push(format!("{flag}={unit}").into());
            }
        }
        Ok(())
    }
}

pub struct JournalLogs {
    pub executable: PathBuf,
    pub max_record_bytes: usize,
    pub max_output_bytes: usize,
}
impl Default for JournalLogs {
    fn default() -> Self {
        Self {
            executable: "journalctl".into(),
            max_record_bytes: RECORD_BYTES,
            max_output_bytes: OUTPUT_BYTES,
        }
    }
}
impl JournalLogs {
    pub fn command(
        &self,
        units: Option<JournalUnits<'_>>,
        request: &JournalQuery,
        follow: bool,
    ) -> Result<Vec<OsString>> {
        request.validate()?;
        ensure!(
            !follow || request.direction != Some(Direction::Backward),
            "journal follow requires forward direction"
        );
        let mut command = vec![
            self.executable.clone().into_os_string(),
            "--no-pager".into(),
            "--all".into(),
            "--output=json".into(),
        ];
        if let Some(scope) = units {
            scope.append_to(&mut command)?;
        }
        if follow {
            ensure!(
                request.cursor.is_none() || request.limit.is_none(),
                "journal follow cannot combine a cursor with a line limit"
            );
            command.push("--follow".into());
            let anchored = request.since.is_some() || request.cursor.is_some();
            if anchored {
                command.push("--boot=all".into());
            }
            if let Some(limit) = request.limit {
                command.push(format!("--lines={limit}").into());
            } else if anchored {
                command.push("--no-tail".into());
            } else {
                command.push("--lines=0".into());
            }
        } else {
            ensure!(
                request.page_size() > 0,
                "journal query limit must be positive"
            );
            let count = request.page_lines()?;
            if request.direction.unwrap_or_default() == Direction::Backward {
                command.push(format!("--lines={count}").into());
                command.push("--reverse".into());
            }
        }
        for (name, value) in [
            ("cursor", &request.cursor),
            ("since", &request.since),
            ("until", &request.until),
            ("namespace", &request.namespace),
            ("grep", &request.grep),
        ] {
            if let Some(value) = value {
                command.push(format!("--{name}={value}").into());
            }
        }
        Ok(command)
    }

    pub async fn query(
        &self,
        units: Option<JournalUnits<'_>>,
        request: &JournalQuery,
        completion_timeout: Duration,
    ) -> Result<JournalResult> {
        self.query_cancellable(units, request, completion_timeout, pending())
            .await
    }

    /// Await cancellation and child cleanup before returning an interrupted error.
    pub async fn query_cancellable(
        &self,
        units: Option<JournalUnits<'_>>,
        request: &JournalQuery,
        completion_timeout: Duration,
        cancelled: impl Future<Output = ()>,
    ) -> Result<JournalResult> {
        ensure!(
            !completion_timeout.is_zero(),
            "log completion timeout must be positive"
        );
        let command = self.command(units, request, false)?;
        let limit = request.page_size();
        // Older journalctl interprets --lines=+N as a request for the newest N entries.
        let forward = request.direction == Some(Direction::Forward);
        let max_lines = if forward {
            Some(request.page_lines()?)
        } else {
            None
        };
        let output = LogOutput::spawn(
            command,
            self.max_record_bytes,
            Some(self.max_output_bytes),
            max_lines,
            None,
        )?;
        let mut stream = JournalStream {
            output,
            cursor: request.cursor.clone(),
            grep: request.grep.is_some(),
        };
        let result = tokio::select! {
            biased;
            _ = cancelled => Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted, "journal query cancelled",
            ).into()),
            result = timeout(completion_timeout, async {
                let mut records = Vec::new();
                let mut has_more = false;
                while let Some(record) = stream.next_record().await? {
                    if records.len() < limit {
                        records.push(record);
                    } else {
                        has_more = true;
                        if forward {
                            break;
                        }
                    }
                }
                Ok((records, has_more))
            }) => result.context("journal query timed out").and_then(|result| result),
        };
        let closed = stream.close().await;
        let (records, has_more) = result?;
        closed?;
        Ok(JournalResult {
            next_cursor: records.last().map(|record| record.cursor().to_owned()),
            records,
            has_more,
            diagnostics: stream.diagnostics(),
            diagnostics_truncated: stream.diagnostics_truncated(),
        })
    }

    pub async fn follow(
        &self,
        units: Option<JournalUnits<'_>>,
        request: &JournalQuery,
    ) -> Result<JournalStream> {
        ensure!(
            self.max_output_bytes > 0,
            "log output byte limits must be positive integers"
        );
        let command = self.command(units, request, true)?;
        Ok(JournalStream {
            output: LogOutput::spawn(command, self.max_record_bytes, None, None, None)?,
            cursor: request.cursor.clone(),
            grep: request.grep.is_some(),
        })
    }
}

/// Dropping the stream requests cleanup; `close().await` confirms reaping.
/// Cancelling `next_record` preserves any pending cursor anchor validation.
/// Diagnostics are a live snapshot until the stream finishes or closes.
/// Closing retains an already observed process failure and reports incomplete drainage.
pub struct JournalStream {
    output: LogOutput,
    cursor: Option<String>,
    grep: bool,
}

/// Read diagnostics while another task is waiting for the next journal record.
#[derive(Clone)]
pub struct JournalDiagnostics(Arc<Mutex<Diagnostics>>);
impl JournalDiagnostics {
    pub fn snapshot(&self) -> (String, bool) {
        let state = self.0.lock().expect("diagnostics lock poisoned");
        (state.text(), state.truncated)
    }
}

impl JournalStream {
    pub fn diagnostics_handle(&self) -> JournalDiagnostics {
        JournalDiagnostics(self.output.diagnostics.clone())
    }
    pub fn pid(&self) -> u32 {
        self.output.pid
    }
    pub fn diagnostics(&self) -> String {
        self.output.diagnostics()
    }
    pub fn diagnostics_truncated(&self) -> bool {
        self.output.diagnostics_truncated()
    }
    pub async fn close(&mut self) -> Result<()> {
        self.output.close().await
    }
    pub async fn next_record(&mut self) -> Result<Option<JournalRecord>> {
        if let Some(cursor) = self.cursor.clone() {
            let anchor = self.read_record().await?;
            ensure!(
                anchor
                    .as_ref()
                    .is_some_and(|anchor| anchor.cursor() == cursor),
                JournalCursorError { cursor }
            );
            self.cursor = None;
        }
        self.read_record().await
    }
    async fn read_record(&mut self) -> Result<Option<JournalRecord>> {
        let line = match self.output.next_line().await {
            Ok(line) => line,
            Err(error)
                if self.grep
                    && error
                        .downcast_ref::<LogProcessError>()
                        .is_some_and(|error| {
                            error.returncode == 1 && error.diagnostics.is_empty()
                        }) =>
            {
                None
            }
            Err(error) => return Err(error),
        };
        line.map(|line| {
            let fields = serde_json::from_slice(&line).context("invalid journalctl JSON record")?;
            JournalRecord::from_fields(fields).context("invalid journalctl JSON record")
        })
        .transpose()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::sync::mpsc;

    #[tokio::test]
    async fn cancelled_read_after_anchor_does_not_validate_it_twice() -> Result<()> {
        let (sender, lines) = mpsc::channel(1);
        let mut stream = JournalStream {
            output: LogOutput {
                pid: 0,
                lines,
                stop: None,
                task: None,
                diagnostics: Arc::default(),
            },
            cursor: Some("anchor".into()),
            grep: false,
        };
        sender
            .send(br#"{"__CURSOR":"anchor","__REALTIME_TIMESTAMP":"1"}"#.to_vec())
            .await?;
        let mut pending = Box::pin(stream.next_record());
        assert!(futures::poll!(&mut pending).is_pending());
        drop(pending);
        sender
            .send(br#"{"__CURSOR":"next","__REALTIME_TIMESTAMP":"2"}"#.to_vec())
            .await?;
        assert_eq!(stream.next_record().await?.unwrap().cursor(), "next");
        drop(sender);
        assert!(stream.next_record().await?.is_none());
        Ok(())
    }
}
