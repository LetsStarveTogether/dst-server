//! Local log readers and their shared bounded subprocess runner.

pub mod history;
mod journal;
mod netdata;

pub use journal::{
    Direction, JournalCursorError, JournalDiagnostics, JournalLogs, JournalQuery, JournalRecord,
    JournalResult, JournalStream, JournalUnits,
};
pub use netdata::{NetdataLogQuery, NetdataLogRecord, NetdataLogResult, NetdataLogs};

use anyhow::{Context, Result, anyhow, ensure};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    os::unix::process::ExitStatusExt,
    process::{ExitStatus, Stdio},
    sync::{Arc, LazyLock, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, BufReader},
    process::{Child, Command},
    sync::{OwnedSemaphorePermit, mpsc, oneshot},
    task::JoinHandle,
    time::timeout,
};

const RECORD_BYTES: usize = 4 * 1024 * 1024;
const OUTPUT_BYTES: usize = 64 * 1024 * 1024;
const DIAGNOSTIC_BYTES: usize = 64 * 1024;
const CLOSE_TIMEOUT: Duration = Duration::from_secs(3);

/// Paths relative to a gameplay event's body. Netdata uses `[]` for repeated fields.
pub static EVENT_USERID_FIELDS: LazyLock<BTreeSet<&'static str>> = LazyLock::new(|| {
    crate::events::EVENT_NAMES
        .iter()
        .flat_map(|name| crate::events::types::participant_fields(name))
        .copied()
        .collect()
});

#[derive(Debug, thiserror::Error)]
#[error("{} exited with status {returncode}: {diagnostics}", .command[0].to_string_lossy())]
pub struct LogProcessError {
    pub command: Vec<OsString>,
    pub returncode: i32,
    pub diagnostics: String,
    pub diagnostics_truncated: bool,
}

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
        max_lines: Option<usize>,
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
            let mut exit_status = None;
            let (outcome, interrupted) = tokio::select! {
                biased;
                _ = &mut stopped => (Ok(()), true),
                result = async {
                    tokio::try_join!(
                        read_lines(&mut stdout, &sender, max_record, max_output, max_lines),
                        read_diagnostics(&mut stderr, &captured),
                        async {
                            exit_status = Some(reap(&mut child, pid).await?);
                            Ok::<_, anyhow::Error>(())
                        },
                    )?;
                    Ok::<_, anyhow::Error>(())
                } => (result, false),
            };
            // Only this supervisor owns cleanup; cancelling a waiter cannot stop it.
            let (status, terminated) = if interrupted || outcome.is_err() {
                let (status, drained) = tokio::join!(
                    async {
                        match exit_status {
                            // Buffered lines can outlive the reaped process group.
                            Some(status) => Ok((status, false)),
                            None => terminate_and_reap(&mut child, pid).await,
                        }
                    },
                    drain_output(&mut stdout, &mut stderr, &captured),
                );
                outcome?;
                let status = status?;
                drained?;
                status
            } else {
                (
                    exit_status.expect("completed reader reaped its child"),
                    false,
                )
            };
            if terminated && matches!(status.signal(), Some(libc::SIGTERM | libc::SIGKILL)) {
                return Ok(());
            }
            check_status(status, command, &captured)
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
    max_lines: Option<usize>,
) -> Result<()> {
    let mut received = 0usize;
    for _ in 0..max_lines.unwrap_or(usize::MAX) {
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
    Ok(())
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

async fn reap(child: &mut Child, pid: u32) -> Result<ExitStatus> {
    let status = child.wait().await?;
    // Descendants must not survive the leader or keep its output pipes open.
    signal_group(pid, libc::SIGKILL);
    Ok(status)
}

async fn terminate_and_reap(child: &mut Child, pid: u32) -> Result<(ExitStatus, bool)> {
    if let Some(status) = child.try_wait()? {
        signal_group(pid, libc::SIGKILL);
        return Ok((status, false));
    }
    signal_group(pid, libc::SIGTERM);
    let status = match timeout(CLOSE_TIMEOUT, reap(child, pid)).await {
        Ok(result) => result?,
        Err(_) => {
            signal_group(pid, libc::SIGKILL);
            child.wait().await?
        }
    };
    Ok((status, true))
}

async fn drain_output<R: AsyncRead + Unpin, E: AsyncRead + Unpin>(
    stdout: &mut R,
    stderr: &mut E,
    diagnostics: &Mutex<Diagnostics>,
) -> Result<()> {
    timeout(CLOSE_TIMEOUT + Duration::from_millis(100), async {
        let mut sink = tokio::io::sink();
        let (output, errors) = tokio::join!(
            tokio::io::copy(stdout, &mut sink),
            read_diagnostics(stderr, diagnostics),
        );
        output?;
        errors
    })
    .await
    .context("log reader output did not close after termination")?
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, pin::Pin, task::Poll};
    use tokio::io::{AsyncWriteExt, ReadBuf};

    #[tokio::test(start_paused = true)]
    async fn cleanup_reports_a_pipe_that_does_not_close() -> Result<()> {
        let (mut writer, mut stderr) = tokio::io::duplex(64);
        writer.write_all(b"last warning").await?;
        let diagnostics = Mutex::new(Diagnostics::default());
        let error = drain_output(&mut tokio::io::empty(), &mut stderr, &diagnostics)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("output did not close"));
        assert_eq!(diagnostics.lock().unwrap().text(), "last warning");
        Ok(())
    }

    #[tokio::test]
    async fn blocked_consumer_bounds_reading_and_resumes_without_losing_lines() -> Result<()> {
        struct Measured<'a> {
            remaining: &'a [u8],
            read: &'a Cell<usize>,
        }
        impl AsyncRead for Measured<'_> {
            fn poll_read(
                mut self: Pin<&mut Self>,
                _: &mut std::task::Context<'_>,
                buffer: &mut ReadBuf<'_>,
            ) -> Poll<std::io::Result<()>> {
                let size = self.remaining.len().min(buffer.remaining());
                buffer.put_slice(&self.remaining[..size]);
                self.remaining = &self.remaining[size..];
                self.read.set(self.read.get() + size);
                Poll::Ready(Ok(()))
            }
        }
        let read = Cell::new(0);
        let mut reader = BufReader::with_capacity(
            1,
            Measured {
                remaining: b"one\ntwo\nthree\n",
                read: &read,
            },
        );
        let (sender, mut lines) = mpsc::channel(1);
        let mut pending = Box::pin(read_lines(&mut reader, &sender, 16, None, None));
        assert!(futures::poll!(&mut pending).is_pending());
        assert_eq!(read.get(), b"one\ntwo\n".len());
        assert_eq!(lines.try_recv()?, b"one\n");
        assert!(futures::poll!(&mut pending).is_pending());
        assert_eq!(lines.try_recv()?, b"two\n");
        pending.await?;
        assert_eq!(lines.try_recv()?, b"three\n");
        assert!(lines.try_recv().is_err());
        Ok(())
    }
}
