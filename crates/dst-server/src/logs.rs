//! Bounded local journalctl and Netdata otel-plugin log readers.

use std::{
    ffi::OsString,
    fmt,
    future::{Future, pending},
    os::unix::process::ExitStatusExt,
    path::PathBuf,
    process::{ExitStatus, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, anyhow, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, BufReader},
    process::{Child, Command},
    sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot},
    task::JoinHandle,
    time::timeout,
};

const RECORD_BYTES: usize = 4 * 1024 * 1024;
const OUTPUT_BYTES: usize = 64 * 1024 * 1024;
const DIAGNOSTIC_BYTES: usize = 64 * 1024;
const CLOSE_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug)]
pub struct LogProcessError {
    pub command: Vec<OsString>,
    pub returncode: i32,
    pub diagnostics: String,
    pub diagnostics_truncated: bool,
}

impl fmt::Display for LogProcessError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} exited with status {}: {}",
            self.command[0].to_string_lossy(),
            self.returncode,
            self.diagnostics
        )
    }
}
impl std::error::Error for LogProcessError {}

#[derive(Debug)]
pub struct JournalCursorError {
    pub cursor: String,
}
impl fmt::Display for JournalCursorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("journal cursor is unavailable for the selected query")
    }
}
impl std::error::Error for JournalCursorError {}

#[derive(Default)]
struct Diagnostics {
    bytes: Vec<u8>,
    truncated: bool,
}
impl Diagnostics {
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.bytes).trim().to_owned()
    }
}

struct LogOutput {
    pid: u32,
    lines: mpsc::Receiver<Vec<u8>>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<Result<()>>>,
    diagnostics: Arc<Mutex<Diagnostics>>,
}

impl LogOutput {
    fn spawn(
        command: Vec<OsString>,
        max_record: usize,
        max_output: Option<usize>,
        permit: Option<OwnedSemaphorePermit>,
    ) -> Result<Self> {
        ensure!(
            max_record > 0 && max_output != Some(0),
            "log output byte limits must be positive integers"
        );
        let mut child = Command::new(&command[0])
            .args(&command[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0)
            .kill_on_drop(true)
            .spawn()
            .with_context(|| format!("start {}", command[0].to_string_lossy()))?;
        let pid = child.id().expect("spawned child has a PID");
        let mut stdout = BufReader::new(child.stdout.take().expect("stdout is piped"));
        let mut stderr = child.stderr.take().expect("stderr is piped");
        // One queued line and one pending send bound memory under backpressure.
        let (sender, lines) = mpsc::channel(1);
        let (stop, mut stopped) = oneshot::channel();
        let diagnostics = Arc::new(Mutex::new(Diagnostics::default()));
        let captured = diagnostics.clone();
        let task = tokio::spawn(async move {
            let _permit = permit;
            let (outcome, interrupted) = tokio::select! {
                biased;
                _ = &mut stopped => (Ok(()), true),
                result = async {
                    let (_, _, status) = tokio::try_join!(
                        read_lines(&mut stdout, &sender, max_record, max_output),
                        read_diagnostics(&mut stderr, &captured),
                        async { child.wait().await.map_err(anyhow::Error::from) },
                    )?;
                    check_status(status, command, &captured)
                } => (result, false),
            };
            // Cleanup is owned by this task, so cancellation of a consumer cannot
            // interrupt child reaping or leave a reader blocked on its pipes.
            if interrupted || outcome.is_err() {
                let cleanup =
                    terminate_and_reap(&mut child, pid, &mut stdout, &mut stderr, &captured).await;
                if outcome.is_ok() {
                    cleanup?;
                }
            }
            outcome
        });
        Ok(Self {
            pid,
            lines,
            stop: Some(stop),
            task: Some(task),
            diagnostics,
        })
    }

    async fn next_line(&mut self) -> Result<Option<Vec<u8>>> {
        if let Some(line) = self.lines.recv().await {
            return Ok(Some(line));
        }
        self.finish().await?;
        Ok(None)
    }

    async fn finish(&mut self) -> Result<()> {
        if let Some(task) = &mut self.task {
            let result = task.await.context("log reader task failed");
            self.task = None;
            result??;
        }
        Ok(())
    }

    async fn close(&mut self) -> Result<()> {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        self.lines.close();
        self.finish().await
    }

    fn diagnostics(&self) -> String {
        self.diagnostics
            .lock()
            .expect("diagnostics lock poisoned")
            .text()
    }
    fn diagnostics_truncated(&self) -> bool {
        self.diagnostics
            .lock()
            .expect("diagnostics lock poisoned")
            .truncated
    }
}

impl Drop for LogOutput {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
    }
}

async fn read_lines<R: AsyncRead + Unpin>(
    reader: &mut BufReader<R>,
    sender: &mpsc::Sender<Vec<u8>>,
    max_record: usize,
    max_output: Option<usize>,
) -> Result<()> {
    let mut received = 0usize;
    loop {
        let mut line = Vec::new();
        loop {
            let available = reader.fill_buf().await?;
            if available.is_empty() {
                break;
            }
            let size = available
                .iter()
                .position(|byte| *byte == b'\n')
                .map_or(available.len(), |at| at + 1);
            ensure!(
                size <= max_record.saturating_sub(line.len()),
                "log record exceeds the configured byte limit"
            );
            let ended = available[size - 1] == b'\n';
            line.extend_from_slice(&available[..size]);
            reader.consume(size);
            if ended {
                break;
            }
        }
        if line.is_empty() {
            return Ok(());
        }
        received = received
            .checked_add(line.len())
            .context("log query byte count overflow")?;
        ensure!(
            max_output.is_none_or(|limit| received <= limit),
            "log query exceeds the configured output byte limit"
        );
        sender
            .send(line)
            .await
            .map_err(|_| anyhow!("log consumer closed"))?;
    }
}

async fn read_diagnostics<R: AsyncRead + Unpin>(
    reader: &mut R,
    diagnostics: &Mutex<Diagnostics>,
) -> Result<()> {
    let mut chunk = [0; 8192];
    loop {
        let size = reader.read(&mut chunk).await?;
        if size == 0 {
            return Ok(());
        }
        let mut state = diagnostics.lock().expect("diagnostics lock poisoned");
        state.bytes.extend_from_slice(&chunk[..size]);
        if state.bytes.len() > DIAGNOSTIC_BYTES {
            state.truncated = true;
            let discard = state.bytes.len() - DIAGNOSTIC_BYTES;
            state.bytes.drain(..discard);
        }
    }
}

fn signal_group(pid: u32, signal: i32) {
    // SAFETY: the child is started as leader of its owned process group.
    unsafe {
        libc::kill(-(pid as i32), signal);
    }
}

async fn terminate_and_reap<R: AsyncRead + Unpin, E: AsyncRead + Unpin>(
    child: &mut Child,
    pid: u32,
    stdout: &mut R,
    stderr: &mut E,
    diagnostics: &Mutex<Diagnostics>,
) -> Result<()> {
    signal_group(pid, libc::SIGTERM);
    let drained = timeout(CLOSE_TIMEOUT, async {
        let mut sink = tokio::io::sink();
        let (status, output, errors) = tokio::join!(
            child.wait(),
            tokio::io::copy(stdout, &mut sink),
            read_diagnostics(stderr, diagnostics),
        );
        status?;
        output?;
        errors
    })
    .await;
    match drained {
        Ok(result) => result,
        Err(_) => {
            signal_group(pid, libc::SIGKILL);
            child.wait().await?;
            // Escaped descendants cannot hold the owned pipes open indefinitely.
            let _ = timeout(
                Duration::from_millis(100),
                read_diagnostics(stderr, diagnostics),
            )
            .await;
            Ok(())
        }
    }
}

fn check_status(
    status: ExitStatus,
    command: Vec<OsString>,
    diagnostics: &Mutex<Diagnostics>,
) -> Result<()> {
    if status.success() {
        return Ok(());
    }
    let diagnostics = diagnostics.lock().expect("diagnostics lock poisoned");
    Err(LogProcessError {
        command,
        returncode: status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1)),
        diagnostics: diagnostics.text(),
        diagnostics_truncated: diagnostics.truncated,
    }
    .into())
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Direction {
    Forward,
    #[default]
    Backward,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct JournalQuery {
    pub limit: usize,
    pub direction: Direction,
    pub cursor: Option<String>,
    pub since: Option<String>,
    pub until: Option<String>,
    pub namespace: Option<String>,
    pub grep: Option<String>,
}
impl Default for JournalQuery {
    fn default() -> Self {
        Self {
            limit: 100,
            direction: Direction::Backward,
            cursor: None,
            since: None,
            until: None,
            namespace: None,
            grep: None,
        }
    }
}
impl JournalQuery {
    pub fn follow() -> Self {
        Self {
            direction: Direction::Forward,
            limit: 0,
            ..Self::default()
        }
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
    pub fn unit(&self) -> String {
        ["UNIT", "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT"]
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
}

fn field_text(value: &Value) -> Result<String> {
    match value {
        Value::Null => Ok(String::new()),
        Value::String(text) => Ok(text.clone()),
        Value::Array(items) if items.iter().all(|item| item.is_u64() || item.is_i64()) => {
            let bytes = items
                .iter()
                .map(|item| {
                    item.as_u64()
                        .and_then(|byte| u8::try_from(byte).ok())
                        .context("invalid journal field value")
                })
                .collect::<Result<Vec<_>>>()?;
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
        units: Option<&[String]>,
        request: &JournalQuery,
        follow: bool,
    ) -> Result<Vec<OsString>> {
        request.validate()?;
        ensure!(
            units.is_none_or(|units| !units.is_empty()),
            "journal logs require at least one explicit unit pattern"
        );
        ensure!(
            !follow || request.direction == Direction::Forward,
            "journal follow requires forward direction"
        );
        let mut command = vec![
            self.executable.clone().into_os_string(),
            "--no-pager".into(),
            "--all".into(),
            "--output=json".into(),
        ];
        let mut seen = std::collections::HashSet::new();
        for unit in units.unwrap_or_default() {
            ensure!(valid_unit(unit), "invalid journal unit pattern");
            if seen.insert(unit) {
                command.push(format!("--unit={unit}").into());
            }
        }
        if follow {
            command.extend([
                "--follow".into(),
                format!("--lines={}", request.limit).into(),
            ]);
        } else {
            let count = request
                .limit
                .checked_add(1 + usize::from(request.cursor.is_some()))
                .context("journal limit is too large")?;
            command.push(
                format!(
                    "--lines={}{count}",
                    if request.direction == Direction::Forward {
                        "+"
                    } else {
                        ""
                    }
                )
                .into(),
            );
            if request.direction == Direction::Backward {
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
        units: Option<&[String]>,
        request: &JournalQuery,
        completion_timeout: Duration,
    ) -> Result<JournalResult> {
        self.query_cancellable(units, request, completion_timeout, pending())
            .await
    }

    /// Await cancellation and child cleanup before returning an interrupted error.
    pub async fn query_cancellable(
        &self,
        units: Option<&[String]>,
        request: &JournalQuery,
        completion_timeout: Duration,
        cancelled: impl Future<Output = ()>,
    ) -> Result<JournalResult> {
        ensure!(
            !completion_timeout.is_zero(),
            "log completion timeout must be positive"
        );
        let command = self.command(units, request, false)?;
        let output = LogOutput::spawn(
            command,
            self.max_record_bytes,
            Some(self.max_output_bytes),
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
                let mut count = 0;
                while let Some(record) = stream.next_record().await? {
                    count += 1;
                    if records.len() < request.limit {
                        records.push(record);
                    }
                }
                Ok(JournalResult {
                    next_cursor: records.last().map(|record| record.cursor().to_owned()),
                    records,
                    has_more: count > request.limit,
                    diagnostics: stream.diagnostics(),
                    diagnostics_truncated: stream.diagnostics_truncated(),
                })
            }) => result.context("journal query timed out").and_then(|result| result),
        };
        let closed = stream.close().await;
        result.and_then(|result| closed.map(|()| result))
    }

    pub async fn follow(
        &self,
        units: Option<&[String]>,
        request: &JournalQuery,
    ) -> Result<JournalStream> {
        ensure!(
            self.max_output_bytes > 0,
            "log output byte limits must be positive integers"
        );
        let command = self.command(units, request, true)?;
        Ok(JournalStream {
            output: LogOutput::spawn(command, self.max_record_bytes, None, None)?,
            cursor: request.cursor.clone(),
            grep: request.grep.is_some(),
        })
    }
}

/// Dropping the stream requests cleanup; `close().await` confirms reaping.
/// Cancelling `next_record` preserves any pending cursor anchor validation.
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

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetdataLogQuery {
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
    pub fields: Vec<String>,
    #[serde(default = "netdata_limit")]
    pub limit: usize,
}
fn netdata_limit() -> usize {
    200
}
impl NetdataLogQuery {
    pub fn new(since: u32) -> Self {
        Self {
            since,
            until: None,
            service_name: None,
            service_namespace: None,
            filters: Vec::new(),
            query: None,
            fields: Vec::new(),
            limit: 200,
        }
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(self.limit > 0, "Netdata limit must be positive");
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
}
impl NetdataLogResult {
    pub fn truncated(&self) -> Option<bool> {
        self.matched
            .map(|matched| matched > self.records.len() as u64)
    }
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
            ("name", &request.service_name),
            ("namespace", &request.service_namespace),
            ("query", &request.query),
        ] {
            if let Some(value) = value {
                command.push(format!("--{name}={value}").into());
            }
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
                let matched = matched_count(
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
                })
            }) => result.context("Netdata query timed out").and_then(|result| result),
        };
        let closed = output.close().await;
        result.and_then(|result| closed.map(|()| result))
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

fn matched_count(
    request: &NetdataLogQuery,
    returned: usize,
    diagnostics: &str,
    truncated: bool,
) -> Result<Option<u64>> {
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
        return Ok(Some(matched));
    }
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
            "no WAL/SFST files matched (tenant=default, window={}..{until}{stream})",
            request.since
        );
        if returned == 0 && diagnostics.trim_end().ends_with(&empty) {
            return Ok(Some(0));
        }
    }
    if truncated {
        return Ok(None);
    }
    bail!("Netdata did not return a valid query summary")
}
