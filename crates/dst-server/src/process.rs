//! Linux process ownership and the native cloudserver pipes.
//!
//! Each child has its own FD 3 (requests), FD 4 (replies), and FD 5 (lifecycle).
//! Waiting or stopping can be cancelled without cancelling the supervisor.

use std::{
    ffi::OsString,
    io,
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::process::ExitStatusExt,
    },
    path::PathBuf,
    process::{ExitStatus, Stdio},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use tokio::{
    io::{AsyncRead, AsyncReadExt},
    net::unix::pipe,
    process::{Child, Command},
    sync::{Mutex, mpsc, watch},
    task::JoinHandle,
    time::{Instant, sleep, sleep_until, timeout_at},
};

use crate::bounded;

pub const MAX_REQUEST_BYTES: usize = 4096;
pub const MAX_PROTOCOL_LINE_BYTES: usize = 64 * 1024;
pub const MAX_RPC_REPLY_BYTES: usize = 1024 * 1024;
pub const MAX_LOG_LINE_BYTES: usize = 1024 * 1024;
const LOG_QUEUE_LINES: usize = 1024;
const LOG_QUEUE_BYTES: usize = 8 * 1024 * 1024;
const PROTOCOL_QUEUE_LINES: usize = 128;
const PROTOCOL_QUEUE_BYTES: usize = PROTOCOL_QUEUE_LINES * MAX_PROTOCOL_LINE_BYTES;
const OUTPUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
pub struct ProcessSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StreamKind {
    Reply,
    Lifecycle,
    Stdout,
    Stderr,
}

impl StreamKind {
    fn line_limit(self, line: &[u8]) -> usize {
        match self {
            Self::Reply if line.starts_with(b"DST_RPC|") => MAX_RPC_REPLY_BYTES,
            Self::Reply | Self::Lifecycle => MAX_PROTOCOL_LINE_BYTES,
            Self::Stdout | Self::Stderr => MAX_LOG_LINE_BYTES,
        }
    }

    fn index(self) -> usize {
        self as usize
    }
}

#[derive(Debug)]
pub enum EventKind {
    /// Native or SDK line, with misplaced SDK frames reduced to a diagnostic.
    Line(Vec<u8>),
    /// The whole line was discarded; reading resumes at the next newline.
    Oversized {
        mandatory: bool,
    },
    ReadError(String),
}

#[derive(Debug)]
pub struct ProcessEvent {
    pub stream: StreamKind,
    pub kind: EventKind,
}

/// Counter arrays are ordered Reply, Lifecycle, Stdout, Stderr.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct OutputStats {
    pub dropped: [u64; 4],
    pub oversized: [u64; 4],
    pub read_errors: [u64; 4],
}

/// Present only after the child was reaped and every output reader finished.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExitSummary {
    pub returncode: Option<i32>,
    pub forced: bool,
    pub output_drained: bool,
    pub protocol_error: Option<String>,
    pub stats: OutputStats,
}

impl From<&ExitReport> for ExitSummary {
    fn from(report: &ExitReport) -> Self {
        Self {
            returncode: report.returncode(),
            forced: report.forced,
            output_drained: report.output_drained,
            protocol_error: report.protocol_error.clone(),
            stats: report.stats.clone(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeDiagnosticKind {
    LoginTransport,
    LuaError,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeDiagnostic {
    pub kind: NativeDiagnosticKind,
    pub observed_at_ms: u64,
    pub count: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeDiagnostics {
    pub login_transport: Option<NativeDiagnostic>,
    pub lua_error: Option<NativeDiagnostic>,
}

#[derive(Default)]
struct NativeCapture {
    diagnostics: NativeDiagnostics,
    login_pending: Option<Instant>,
}

impl NativeCapture {
    fn observe(
        &mut self,
        line: &[u8],
        now: impl FnOnce() -> Instant,
        wall_ms: impl FnOnce() -> u64,
    ) {
        let pending = self.login_pending.take();
        let Some(line) = line.strip_suffix(b"\n") else {
            return;
        };
        let message = line.strip_suffix(b"\r").unwrap_or(line);
        // shortcut: only the verified native DNS pair is supported; extend with new native evidence.
        let kind = if message == b"CURL ERROR: (login.kleientertainment.com) [6]Could not resolve host: login.kleientertainment.com" {
            self.login_pending = Some(now());
            return;
        } else if message == b"[0] Account Failed (6): \"\""
            && pending.is_some_and(|at| now().saturating_duration_since(at) <= Duration::from_secs(5))
        {
            NativeDiagnosticKind::LoginTransport
        } else if message == b"LUA ERROR stack traceback:" {
            NativeDiagnosticKind::LuaError
        } else {
            return;
        };
        let slot = match kind {
            NativeDiagnosticKind::LoginTransport => &mut self.diagnostics.login_transport,
            NativeDiagnosticKind::LuaError => &mut self.diagnostics.lua_error,
        };
        let observed_at_ms = wall_ms();
        let diagnostic = slot.get_or_insert(NativeDiagnostic {
            kind,
            observed_at_ms,
            count: 0,
        });
        diagnostic.observed_at_ms = observed_at_ms;
        diagnostic.count = diagnostic.count.saturating_add(1);
    }
}

#[derive(Debug, Clone)]
pub struct ExitReport {
    pub status: ExitStatus,
    /// The supervisor requested SIGTERM or SIGKILL for the child.
    pub forced: bool,
    pub output_drained: bool,
    pub protocol_error: Option<String>,
    pub stats: OutputStats,
}

impl ExitReport {
    /// Match Python subprocess return codes: a terminating signal is negative.
    pub fn returncode(&self) -> Option<i32> {
        self.status
            .code()
            .or_else(|| self.status.signal().map(|signal| -signal))
    }
}

#[derive(Default)]
struct Counters {
    dropped: [AtomicU64; 4],
    oversized: [AtomicU64; 4],
    read_errors: [AtomicU64; 4],
    native: std::sync::Mutex<NativeCapture>,
}

impl Counters {
    fn snapshot(&self) -> OutputStats {
        OutputStats {
            dropped: std::array::from_fn(|i| self.dropped[i].load(Ordering::Relaxed)),
            oversized: std::array::from_fn(|i| self.oversized[i].load(Ordering::Relaxed)),
            read_errors: std::array::from_fn(|i| self.read_errors[i].load(Ordering::Relaxed)),
        }
    }
}

#[derive(Clone, Copy)]
enum Stop {
    Running,
    Terminate(Instant),
    Kill,
}

type Completion = Option<Result<ExitReport, String>>;

/// A single-use process handle. Drop requests a kill; explicit wait confirms reaping.
pub struct GameProcess {
    pid: u32,
    input: Mutex<pipe::Sender>,
    stop: watch::Sender<Stop>,
    completion: watch::Receiver<Completion>,
    counters: Arc<Counters>,
}

/// The sole reader for a process's filtered output.
pub struct ProcessOutput {
    protocol: mpsc::Receiver<bounded::Item<ProcessEvent>>,
    logs: mpsc::Receiver<bounded::Item<ProcessEvent>>,
}

impl ProcessOutput {
    /// Control output has a separate queue and takes priority over game logs.
    pub async fn next_event(&mut self) -> Option<ProcessEvent> {
        tokio::select! {
            biased;
            Some(item) = self.protocol.recv() => Some(item.into_inner()),
            Some(item) = self.logs.recv() => Some(item.into_inner()),
            else => None,
        }
    }
}

impl GameProcess {
    pub fn spawn(spec: ProcessSpec) -> io::Result<(Self, ProcessOutput)> {
        Self::spawn_inner(spec, None)
    }

    /// Replace the child's environment while retaining the same supervised
    /// process group, pipe bounds and cancellation semantics.
    pub fn spawn_with_environment(
        spec: ProcessSpec,
        environment: Vec<(OsString, OsString)>,
    ) -> io::Result<(Self, ProcessOutput)> {
        Self::spawn_inner(spec, Some(environment))
    }

    fn spawn_inner(
        spec: ProcessSpec,
        environment: Option<Vec<(OsString, OsString)>>,
    ) -> io::Result<(Self, ProcessOutput)> {
        // Keep child source descriptors above 5 before dup2. A source must not be
        // overwritten while installing a different protocol descriptor.
        let (request_read, request_write) = open_pipe()?;
        let (reply_read, reply_write) = open_pipe()?;
        let (lifecycle_read, lifecycle_write) = open_pipe()?;
        let sources = [
            above_protocol(request_read)?,
            above_protocol(reply_write)?,
            above_protocol(lifecycle_write)?,
        ];
        let input = pipe::Sender::from_owned_fd(request_write)?;
        let reply = pipe::Receiver::from_owned_fd(reply_read)?;
        let lifecycle = pipe::Receiver::from_owned_fd(lifecycle_read)?;
        let descriptors = sources.each_ref().map(AsRawFd::as_raw_fd);
        let mut command = Command::new(&spec.program);
        command
            .args(&spec.args)
            .current_dir(&spec.cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0)
            .kill_on_drop(true);
        if let Some(environment) = environment {
            command.env_clear().envs(environment);
        }
        // SAFETY: only async-signal-safe dup2/close run between fork and exec.
        // Sources are owned by this stack frame until spawn returns.
        unsafe {
            command.pre_exec(move || {
                for (source, target) in descriptors.into_iter().zip(3..=5) {
                    if libc::dup2(source, target) == -1 {
                        return Err(io::Error::last_os_error());
                    }
                }
                for source in descriptors {
                    libc::close(source);
                }
                Ok(())
            });
        }
        let mut child = command.spawn()?;
        drop(sources);
        let pid = child.id().expect("spawned child has a PID");
        let stdout = child.stdout.take().expect("stdout is piped");
        let stderr = child.stderr.take().expect("stderr is piped");
        let (protocol_tx, protocol) = bounded::channel(PROTOCOL_QUEUE_LINES, PROTOCOL_QUEUE_BYTES);
        let (logs_tx, logs) = bounded::channel(LOG_QUEUE_LINES, LOG_QUEUE_BYTES);
        let (fatal_tx, fatal) = watch::channel(None);
        let counters = Arc::new(Counters::default());
        let output = |stream| Output {
            stream,
            logs: logs_tx.clone(),
            protocol: protocol_tx.clone(),
            fatal: fatal_tx.clone(),
            counters: counters.clone(),
        };
        let outputs = [
            tokio::spawn(read_output(reply, output(StreamKind::Reply))),
            tokio::spawn(read_output(lifecycle, output(StreamKind::Lifecycle))),
            tokio::spawn(read_output(stdout, output(StreamKind::Stdout))),
            tokio::spawn(read_output(stderr, output(StreamKind::Stderr))),
        ];
        let (stop, stop_rx) = watch::channel(Stop::Running);
        let (complete_tx, completion) = watch::channel(None);
        tokio::spawn(supervise(
            child,
            stop_rx,
            fatal,
            outputs,
            counters.clone(),
            complete_tx,
        ));
        Ok((
            Self {
                pid,
                input: Mutex::new(input),
                stop,
                completion,
                counters,
            },
            ProcessOutput { protocol, logs },
        ))
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub fn stats(&self) -> OutputStats {
        self.counters.snapshot()
    }

    pub fn native_diagnostics(&self) -> NativeDiagnostics {
        self.counters
            .native
            .lock()
            .expect("native diagnostic lock")
            .diagnostics
            .clone()
    }

    /// Write one native read chunk atomically. The caller owns request framing.
    /// Cancellation before the write sends nothing; cancellation after it must
    /// not cause the caller to resend a mutation whose result is unknown.
    pub async fn send(&self, bytes: &[u8]) -> io::Result<()> {
        if bytes.is_empty() || bytes.len() > MAX_REQUEST_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "request must contain 1..=4096 bytes",
            ));
        }
        self.with_writable_input(|input| write_request(input, bytes))
            .await
    }

    pub(crate) async fn with_writable_input<T>(
        &self,
        mut write: impl FnMut(&pipe::Sender) -> io::Result<T>,
    ) -> io::Result<T> {
        let input = self.input.lock().await;
        loop {
            if self.completion.borrow().is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "game process has exited",
                ));
            }
            let mut remaining: libc::c_int = 0;
            // SAFETY: the descriptor is owned and remaining points to an int.
            if unsafe { libc::ioctl(input.as_raw_fd(), libc::FIONREAD, &mut remaining) } == -1 {
                return Err(io::Error::last_os_error());
            }
            // Linux atomic writes do not imply message boundaries. Wait for the
            // prior chunk to be consumed so native cannot combine two requests.
            if remaining != 0 {
                sleep(Duration::from_millis(10)).await;
                continue;
            }
            input.writable().await?;
            match write(&input) {
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    continue;
                }
                result => return result,
            }
        }
    }

    pub async fn wait(&self) -> io::Result<ExitReport> {
        let mut completion = self.completion.clone();
        loop {
            if let Some(result) = completion.borrow_and_update().clone() {
                return result.map_err(io::Error::other);
            }
            completion.changed().await.map_err(|_| {
                io::Error::other("process supervisor stopped without an exit report")
            })?;
        }
    }

    /// Send SIGTERM, escalate after the grace period, reap, then drain output.
    /// A native save-and-exit request should be sent before using this fallback.
    pub async fn shutdown(&self, grace: Duration) -> io::Result<ExitReport> {
        let deadline = Instant::now().checked_add(grace).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "shutdown grace period is too large",
            )
        })?;
        self.request_shutdown(deadline);
        self.wait().await
    }

    pub(crate) fn request_shutdown(&self, deadline: Instant) {
        if deadline <= Instant::now() {
            self.request_kill();
            return;
        }
        self.stop.send_if_modified(|stop| match stop {
            Stop::Running => {
                *stop = Stop::Terminate(deadline);
                true
            }
            Stop::Terminate(current) if deadline < *current => {
                *current = deadline;
                true
            }
            _ => false,
        });
    }

    pub async fn kill(&self) -> io::Result<ExitReport> {
        self.request_kill();
        self.wait().await
    }

    /// Request termination synchronously; `wait` confirms exit and reaping.
    pub fn request_kill(&self) {
        self.stop.send_replace(Stop::Kill);
    }
}

pub(crate) fn write_request(input: &pipe::Sender, bytes: &[u8]) -> io::Result<()> {
    if input.try_write(bytes)? == bytes.len() {
        Ok(())
    } else {
        Err(io::Error::other("atomic pipe write was incomplete"))
    }
}

impl Drop for GameProcess {
    fn drop(&mut self) {
        self.request_kill();
    }
}

fn open_pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut descriptors = [-1; 2];
    // SAFETY: pipe2 writes two descriptors to the supplied array.
    if unsafe { libc::pipe2(descriptors.as_mut_ptr(), libc::O_CLOEXEC) } == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful pipe2 returns two newly owned descriptors.
    Ok(unsafe {
        (
            OwnedFd::from_raw_fd(descriptors[0]),
            OwnedFd::from_raw_fd(descriptors[1]),
        )
    })
}

fn above_protocol(fd: OwnedFd) -> io::Result<OwnedFd> {
    if fd.as_raw_fd() > 5 {
        return Ok(fd);
    }
    // SAFETY: F_DUPFD_CLOEXEC returns a new descriptor, preserving fd ownership.
    let replacement = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 6) };
    if replacement == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: replacement is a newly owned descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(replacement) })
}

fn fail_protocol(fatal: &watch::Sender<Option<String>>, message: String) {
    fatal.send_if_modified(|current| {
        if current.is_some() {
            return false;
        }
        *current = Some(message);
        true
    });
}

struct Output {
    stream: StreamKind,
    logs: bounded::Sender<ProcessEvent>,
    protocol: bounded::Sender<ProcessEvent>,
    fatal: watch::Sender<Option<String>>,
    counters: Arc<Counters>,
}

impl Output {
    fn observe(&self, line: &[u8]) {
        if self.stream == StreamKind::Stdout {
            self.counters
                .native
                .lock()
                .expect("native diagnostic lock")
                .observe(line, Instant::now, || {
                    crate::operation::timestamp() / 1_000_000
                });
        }
    }

    fn clear_native_pending(&self) {
        if self.stream == StreamKind::Stdout {
            self.counters
                .native
                .lock()
                .expect("native diagnostic lock")
                .login_pending = None;
        }
    }

    fn mandatory(&self, line: &[u8]) -> bool {
        self.stream == StreamKind::Lifecycle
            || (self.stream == StreamKind::Reply && !line.starts_with(b"DST_OTEL|"))
    }

    fn line(&self, mut line: Vec<u8>) {
        let mandatory = self.mandatory(&line);
        if mandatory && !line.ends_with(b"\n") {
            fail_protocol(
                &self.fatal,
                format!("{:?} ended with an incomplete line", self.stream),
            );
            return;
        }
        match self.stream {
            StreamKind::Stdout | StreamKind::Stderr => {
                let message = native_message(&line);
                self.observe(message);
                if sdk_frame_name(message).is_some() {
                    return;
                }
            }
            StreamKind::Lifecycle if native_message(&line).starts_with(b"DST_Stats|") => return,
            // Only raw FD4 frames reach the SDK decoder. Keep misplaced bodies out of logs.
            StreamKind::Reply if !line.starts_with(b"[") => (),
            StreamKind::Reply | StreamKind::Lifecycle => {
                if let Some(summary) = sdk_frame_summary(&line) {
                    line = summary.into_bytes();
                }
            }
        }
        self.emit(EventKind::Line(line), mandatory);
    }

    fn emit(&self, kind: EventKind, mandatory: bool) {
        let size = match &kind {
            EventKind::Line(bytes) => bytes.len(),
            _ => 1,
        };
        let event = ProcessEvent {
            stream: self.stream,
            kind,
        };
        let queue = if mandatory {
            &self.protocol
        } else {
            &self.logs
        };
        if queue.try_send(event, size).is_err() {
            self.dropped(mandatory);
        }
    }

    fn dropped(&self, mandatory: bool) {
        self.counters.dropped[self.stream.index()].fetch_add(1, Ordering::Relaxed);
        if mandatory {
            fail_protocol(
                &self.fatal,
                format!(
                    "{:?} consumer is unavailable or its queue is full",
                    self.stream
                ),
            );
        }
    }

    fn oversized(&self, mandatory: bool, limit: usize) {
        self.clear_native_pending();
        self.counters.oversized[self.stream.index()].fetch_add(1, Ordering::Relaxed);
        if mandatory {
            fail_protocol(
                &self.fatal,
                format!("{:?} line exceeds {limit} bytes", self.stream),
            );
        }
        self.emit(EventKind::Oversized { mandatory }, mandatory);
    }
}

async fn read_output<R: AsyncRead + Unpin>(mut reader: R, output: Output) {
    let mut chunk = [0_u8; 8192];
    let mut line = Vec::new();
    let mut oversized = false;
    loop {
        let count = match reader.read(&mut chunk).await {
            Ok(0) => {
                if !oversized && !line.is_empty() {
                    output.line(line);
                }
                return;
            }
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
                output.clear_native_pending();
                output.counters.read_errors[output.stream.index()].fetch_add(1, Ordering::Relaxed);
                if output.stream != StreamKind::Stderr {
                    fail_protocol(&output.fatal, format!("{:?}: {error}", output.stream));
                }
                output.emit(
                    EventKind::ReadError(error.to_string()),
                    output.stream != StreamKind::Stderr,
                );
                return;
            }
        };
        for part in chunk[..count].split_inclusive(|byte| *byte == b'\n') {
            let ends_line = part.last() == Some(&b'\n');
            if !oversized {
                // Each read is smaller than the shortest limit, so the prefix is
                // complete in `line` before any frame can cross its limit.
                let limit = output.stream.line_limit(&line);
                if line.len() + part.len() > limit {
                    output.oversized(output.mandatory(&line), limit);
                    line = Vec::new();
                    oversized = true;
                } else {
                    line.extend_from_slice(part);
                }
            }
            if ends_line {
                if !oversized {
                    output.line(std::mem::take(&mut line));
                }
                oversized = false;
            }
        }
    }
}

fn sdk_frame_name(message: &[u8]) -> Option<&'static str> {
    let message = message.strip_prefix(b"DST_")?;
    ["DRIVER|", "RPC|", "CONTROL|", "OTEL|"]
        .into_iter()
        .find(|name| message.starts_with(name.as_bytes()))
}

/// A diagnostic may retain the frame kind and physical length, including the newline.
pub(crate) fn sdk_frame_summary(line: &[u8]) -> Option<String> {
    let name = sdk_frame_name(native_message(line))?.trim_end_matches('|');
    Some(format!("SDK {name} frame ({} bytes)\n", line.len()))
}

/// Remove only an anchored native `[hours:mm:ss]: ` timestamp, if present.
/// Remove the optional space, leaving message bytes and newline unchanged.
pub fn native_message(line: &[u8]) -> &[u8] {
    let Some(clock) = line.strip_prefix(b"[") else {
        return line;
    };
    // Native hours fit a machine integer; bound scanning even on malformed logs.
    let Some(end) = clock.iter().take(32).position(|byte| *byte == b']') else {
        return line;
    };
    let Some(message) = clock[end + 1..].strip_prefix(b":") else {
        return line;
    };
    let mut parts = clock[..end].split(|byte| *byte == b':');
    for width in [None, Some(2), Some(2)] {
        let Some(part) = parts.next() else {
            return line;
        };
        if part.is_empty()
            || width.is_some_and(|width| part.len() != width)
            || !part.iter().all(u8::is_ascii_digit)
        {
            return line;
        }
    }
    if parts.next().is_some() {
        return line;
    }
    message.strip_prefix(b" ").unwrap_or(message)
}

fn signal_group(pid: u32, signal: libc::c_int) -> io::Result<()> {
    // SAFETY: the child was started as leader of this owned process group.
    if unsafe { libc::kill(-(pid as libc::pid_t), signal) } == -1 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error);
        }
    }
    Ok(())
}

async fn supervise(
    mut child: Child,
    mut stop: watch::Receiver<Stop>,
    mut fatal: watch::Receiver<Option<String>>,
    mut outputs: [JoinHandle<()>; 4],
    counters: Arc<Counters>,
    completion: watch::Sender<Completion>,
) {
    let pid = child.id().expect("supervisor owns a running child");
    let mut deadline = None;
    let mut forced = false;
    let mut fatal_open = true;
    let status = loop {
        let request = *stop.borrow_and_update();
        match request {
            Stop::Running => (),
            Stop::Terminate(at) => {
                if deadline.is_none() {
                    let _ = signal_group(pid, libc::SIGTERM);
                }
                deadline = Some(deadline.map_or(at, |current: Instant| current.min(at)));
            }
            Stop::Kill => {
                forced = true;
                let _ = signal_group(pid, libc::SIGKILL);
            }
        }
        if fatal.borrow_and_update().is_some() {
            forced = true;
            let _ = signal_group(pid, libc::SIGKILL);
        }
        tokio::select! {
            status = child.wait() => break status,
            changed = stop.changed(), if !forced => {
                if changed.is_err() { forced = true; let _ = signal_group(pid, libc::SIGKILL); }
            }
            changed = fatal.changed(), if !forced && fatal_open => {
                // Reader tasks own the senders; ordinary EOF closes this watch.
                if changed.is_err() { fatal_open = false; }
            }
            _ = async { if let Some(at) = deadline { sleep_until(at).await; } }, if deadline.is_some() && !forced => {
                forced = true;
                let _ = signal_group(pid, libc::SIGKILL);
            }
        }
    };
    // Reclaim descendants that inherited a pipe after the game exited.
    let _ = signal_group(pid, libc::SIGKILL);
    let drain_deadline = Instant::now() + OUTPUT_DRAIN_TIMEOUT;
    let mut output_drained = true;
    for task in &mut outputs {
        match timeout_at(drain_deadline, &mut *task).await {
            Ok(Ok(())) => (),
            Ok(Err(_)) => output_drained = false,
            Err(_) => {
                output_drained = false;
                task.abort();
                let _ = task.await;
            }
        }
    }
    completion.send_replace(Some(
        status
            .map(|status| ExitReport {
                status,
                forced: forced || deadline.is_some(),
                output_drained,
                protocol_error: fatal.borrow().clone(),
                stats: counters.snapshot(),
            })
            .map_err(|error| error.to_string()),
    ));
}

#[cfg(test)]
mod tests {
    use super::*;

    const DNS: &[u8] = b"CURL ERROR: (login.kleientertainment.com) [6]Could not resolve host: login.kleientertainment.com\n";
    const ACCOUNT: &[u8] = b"[0] Account Failed (6): \"\"\n";

    #[test]
    fn native_pair_requires_exact_adjacent_complete_lines_within_five_seconds() {
        let now = Instant::now();
        let cases = [
            (DNS.to_vec(), ACCOUNT.to_vec(), Duration::ZERO, true),
            (DNS.to_vec(), ACCOUNT.to_vec(), Duration::from_secs(5), true),
            (
                DNS.to_vec(),
                ACCOUNT.to_vec(),
                Duration::from_secs(5) + Duration::from_nanos(1),
                false,
            ),
            (
                [b"[125:01:02]: ".as_slice(), DNS].concat(),
                [b"[125:01:02]: ".as_slice(), ACCOUNT].concat(),
                Duration::ZERO,
                true,
            ),
            (
                [b"[125:01:02]: [125:01:02]: ".as_slice(), DNS].concat(),
                ACCOUNT.to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                String::from_utf8_lossy(DNS)
                    .replace("login.kleientertainment.com", "dst.metrics.klei.com")
                    .into_bytes(),
                ACCOUNT.to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                String::from_utf8_lossy(DNS)
                    .replace("login.kleientertainment.com", "items.kleientertainment.com")
                    .into_bytes(),
                ACCOUNT.to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                String::from_utf8_lossy(DNS)
                    .replace("[6]", "[28]")
                    .into_bytes(),
                ACCOUNT.to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                DNS.to_vec(),
                b"[0] Account Failed (6): \"private player text\"\n".to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                DNS.to_vec(),
                b"[0] Account Failed (7): \"\"\n".to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                [b"player: ".as_slice(), DNS].concat(),
                ACCOUNT.to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                DNS.to_vec(),
                [b"player: ".as_slice(), ACCOUNT].concat(),
                Duration::ZERO,
                false,
            ),
            (
                DNS.to_vec(),
                ACCOUNT[..ACCOUNT.len() - 1].to_vec(),
                Duration::ZERO,
                false,
            ),
            (
                DNS[..DNS.len() - 1].to_vec(),
                ACCOUNT.to_vec(),
                Duration::ZERO,
                false,
            ),
        ];
        for (first, second, delay, expected) in cases {
            let mut capture = NativeCapture::default();
            capture.observe(native_message(&first), || now, || 1);
            capture.observe(native_message(&second), || now + delay, || 2);
            assert_eq!(
                capture.diagnostics.login_transport.is_some(),
                expected,
                "{first:?} {second:?} {delay:?}"
            );
            assert!(capture.login_pending.is_none());
        }
        for separator in [b"other\n".as_slice(), b"", b"partial"] {
            let mut capture = NativeCapture::default();
            capture.observe(DNS, || now, || 1);
            capture.observe(separator, || unreachable!(), || unreachable!());
            capture.observe(ACCOUNT, || now, || 3);
            assert!(capture.diagnostics.login_transport.is_none());
        }
        let mut first_process = NativeCapture::default();
        let mut second_process = NativeCapture::default();
        first_process.observe(DNS, || now, || 1);
        second_process.observe(ACCOUNT, || now, || 2);
        assert!(first_process.diagnostics.login_transport.is_none());
        assert!(second_process.diagnostics.login_transport.is_none());
        for _ in 0..2 {
            first_process.observe(DNS, || now, || 3);
            first_process.observe(ACCOUNT, || now, || 4);
        }
        assert_eq!(
            first_process
                .diagnostics
                .login_transport
                .as_ref()
                .unwrap()
                .count,
            2
        );
        first_process
            .diagnostics
            .login_transport
            .as_mut()
            .unwrap()
            .count = u64::MAX;
        first_process.observe(DNS, || now, || 5);
        first_process.observe(ACCOUNT, || now, || 6);
        assert_eq!(
            first_process
                .diagnostics
                .login_transport
                .as_ref()
                .unwrap()
                .count,
            u64::MAX
        );
        for text in [
            b"LUA ERROR stack traceback:".as_slice(),
            b"player: LUA ERROR stack traceback:\n",
            b"LUA ERROR stack traceback: private\n",
        ] {
            first_process.observe(text, || now, || 7);
        }
        assert!(first_process.diagnostics.lua_error.is_none());
        first_process.observe(
            native_message(b"[0:00:01]: LUA ERROR stack traceback:\r\n"),
            || now,
            || 8,
        );
        assert_eq!(
            first_process.diagnostics.lua_error.as_ref().unwrap().count,
            1
        );
        let encoded = serde_json::to_string(&first_process.diagnostics).unwrap();
        assert!(!encoded.contains("private") && encoded.len() < 256);
    }
}
