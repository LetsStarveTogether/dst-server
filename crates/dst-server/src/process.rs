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
    sync::{Mutex, OwnedSemaphorePermit, Semaphore, mpsc, watch},
    task::JoinHandle,
    time::{Instant, sleep, sleep_until, timeout_at},
};

pub const MAX_REQUEST_BYTES: usize = 4096;
pub const MAX_PROTOCOL_LINE_BYTES: usize = 64 * 1024;
pub const MAX_LOG_LINE_BYTES: usize = 1024 * 1024;
const LOG_QUEUE_LINES: usize = 1024;
const LOG_QUEUE_BYTES: usize = 8 * 1024 * 1024;
const PROTOCOL_QUEUE_LINES: usize = 128;
const OUTPUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
pub struct ProcessSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamKind {
    Reply,
    Lifecycle,
    Stdout,
    Stderr,
}

impl StreamKind {
    fn protocol(self) -> bool {
        matches!(self, Self::Reply | Self::Lifecycle)
    }

    fn index(self) -> usize {
        self as usize
    }
}

#[derive(Debug)]
pub enum EventKind {
    /// Original bytes, including the newline when present.
    Line(Vec<u8>),
    /// The whole line was discarded; reading resumes at the next newline.
    Oversized,
    ReadError(String),
}

#[derive(Debug)]
pub struct ProcessEvent {
    pub stream: StreamKind,
    pub kind: EventKind,
}

/// Counter arrays are ordered Reply, Lifecycle, Stdout, Stderr.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct OutputStats {
    pub dropped: [u64; 4],
    pub oversized: [u64; 4],
    pub read_errors: [u64; 4],
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

struct QueuedEvent {
    event: ProcessEvent,
    _bytes: Option<OwnedSemaphorePermit>,
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
    events: Mutex<(mpsc::Receiver<QueuedEvent>, mpsc::Receiver<QueuedEvent>)>,
    stop: watch::Sender<Stop>,
    completion: watch::Receiver<Completion>,
    counters: Arc<Counters>,
}

impl GameProcess {
    pub fn spawn(spec: ProcessSpec) -> io::Result<Self> {
        Self::spawn_inner(spec, None)
    }

    /// Replace the child's environment while retaining the same supervised
    /// process group, pipe bounds and cancellation semantics.
    pub fn spawn_with_environment(
        spec: ProcessSpec,
        environment: Vec<(OsString, OsString)>,
    ) -> io::Result<Self> {
        Self::spawn_inner(spec, Some(environment))
    }

    fn spawn_inner(
        spec: ProcessSpec,
        environment: Option<Vec<(OsString, OsString)>>,
    ) -> io::Result<Self> {
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
        let (protocol_tx, protocol) = mpsc::channel(PROTOCOL_QUEUE_LINES);
        let (logs_tx, logs) = mpsc::channel(LOG_QUEUE_LINES);
        let (fatal_tx, fatal) = watch::channel(None);
        let counters = Arc::new(Counters::default());
        let log_bytes = Arc::new(Semaphore::new(LOG_QUEUE_BYTES));
        let outputs = [
            spawn_reader(
                reply,
                StreamKind::Reply,
                protocol_tx.clone(),
                protocol_tx.clone(),
                None,
                fatal_tx.clone(),
                counters.clone(),
            ),
            spawn_reader(
                lifecycle,
                StreamKind::Lifecycle,
                protocol_tx.clone(),
                protocol_tx.clone(),
                None,
                fatal_tx.clone(),
                counters.clone(),
            ),
            spawn_reader(
                stdout,
                StreamKind::Stdout,
                logs_tx.clone(),
                protocol_tx.clone(),
                Some(log_bytes.clone()),
                fatal_tx.clone(),
                counters.clone(),
            ),
            spawn_reader(
                stderr,
                StreamKind::Stderr,
                logs_tx,
                protocol_tx,
                Some(log_bytes),
                fatal_tx,
                counters.clone(),
            ),
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
        Ok(Self {
            pid,
            input: Mutex::new(input),
            events: Mutex::new((protocol, logs)),
            stop,
            completion,
            counters,
        })
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub fn stats(&self) -> OutputStats {
        self.counters.snapshot()
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
            match input.try_write(bytes) {
                Ok(written) if written == bytes.len() => return Ok(()),
                Ok(_) => return Err(io::Error::other("atomic pipe write was incomplete")),
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    continue;
                }
                Err(error) => return Err(error),
            }
        }
    }

    /// Control output has a separate queue and takes priority over game logs.
    pub async fn next_event(&self) -> Option<ProcessEvent> {
        let mut events = self.events.lock().await;
        let (protocol, logs) = &mut *events;
        loop {
            tokio::select! {
                biased;
                item = protocol.recv(), if !protocol.is_closed() || !protocol.is_empty() => {
                    if let Some(item) = item { return Some(item.event); }
                }
                item = logs.recv(), if !logs.is_closed() || !logs.is_empty() => {
                    if let Some(item) = item { return Some(item.event); }
                }
                else => return None,
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
        self.wait().await
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
    events: mpsc::Sender<QueuedEvent>,
    protocol_events: mpsc::Sender<QueuedEvent>,
    bytes: Option<Arc<Semaphore>>,
    fatal: watch::Sender<Option<String>>,
    counters: Arc<Counters>,
}

impl Output {
    fn mandatory(&self, line: &[u8]) -> bool {
        self.stream.protocol()
            || (self.stream == StreamKind::Stdout
                && [b"DST_DRIVER|".as_slice(), b"DST_CONTROL|".as_slice()]
                    .iter()
                    .any(|prefix| native_message(line).starts_with(prefix)))
    }

    fn emit(&self, kind: EventKind, mandatory: bool) {
        let size = match &kind {
            EventKind::Line(bytes) => bytes.len(),
            _ => 1,
        };
        let permit = match self.bytes.as_ref().filter(|_| !mandatory) {
            Some(budget) => match budget.clone().try_acquire_many_owned(size.max(1) as u32) {
                Ok(permit) => Some(permit),
                Err(_) => {
                    self.dropped(mandatory);
                    return;
                }
            },
            None => None,
        };
        let event = QueuedEvent {
            event: ProcessEvent {
                stream: self.stream,
                kind,
            },
            _bytes: permit,
        };
        let events = if mandatory {
            &self.protocol_events
        } else {
            &self.events
        };
        if events.try_send(event).is_err() {
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

    fn oversized(&self, mandatory: bool) {
        self.counters.oversized[self.stream.index()].fetch_add(1, Ordering::Relaxed);
        if mandatory {
            fail_protocol(
                &self.fatal,
                format!(
                    "{:?} line exceeds {MAX_PROTOCOL_LINE_BYTES} bytes",
                    self.stream
                ),
            );
        }
        self.emit(EventKind::Oversized, mandatory);
    }
}

fn spawn_reader<R: AsyncRead + Unpin + Send + 'static>(
    reader: R,
    stream: StreamKind,
    events: mpsc::Sender<QueuedEvent>,
    protocol_events: mpsc::Sender<QueuedEvent>,
    bytes: Option<Arc<Semaphore>>,
    fatal: watch::Sender<Option<String>>,
    counters: Arc<Counters>,
) -> JoinHandle<()> {
    tokio::spawn(read_output(
        reader,
        Output {
            stream,
            events,
            protocol_events,
            bytes,
            fatal,
            counters,
        },
    ))
}

async fn read_output<R: AsyncRead + Unpin>(mut reader: R, output: Output) {
    let mut chunk = [0_u8; 8192];
    let mut line = Vec::new();
    let mut oversized = false;
    let mut mandatory = output.stream.protocol();
    loop {
        let count = match reader.read(&mut chunk).await {
            Ok(0) => {
                if !oversized && !line.is_empty() {
                    if mandatory {
                        fail_protocol(
                            &output.fatal,
                            format!("{:?} ended with an incomplete line", output.stream),
                        );
                    } else {
                        output.emit(EventKind::Line(line), false);
                    }
                }
                return;
            }
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
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
                let limit = if mandatory {
                    MAX_PROTOCOL_LINE_BYTES
                } else {
                    MAX_LOG_LINE_BYTES
                };
                if line.len() + part.len() > limit {
                    line.clear();
                    oversized = true;
                    output.oversized(mandatory);
                } else {
                    line.extend_from_slice(part);
                    mandatory = output.mandatory(&line);
                    if mandatory && line.len() > MAX_PROTOCOL_LINE_BYTES {
                        line.clear();
                        oversized = true;
                        output.oversized(true);
                    }
                }
            }
            if ends_line {
                if !oversized {
                    output.emit(EventKind::Line(std::mem::take(&mut line)), mandatory);
                }
                oversized = false;
                mandatory = output.stream.protocol();
            }
        }
    }
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
