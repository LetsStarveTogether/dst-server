//! Local delivery shared by runtime records and exporter health.

use super::{LocalRecord, LogEvent};
use crate::{
    bounded::{self, TrySendError},
    rpc::{EventHub, StreamKind},
};
use serde::Serialize;
use serde_json::{Map, Value};
use std::{
    io::{self, Write},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{sync::watch, time::timeout};

#[derive(Clone)]
pub(crate) struct LocalLog {
    pub(crate) events: EventHub,
    pub(crate) resource: Arc<Map<String, Value>>,
    pub(crate) output: LocalOutput,
}

pub(crate) struct FinishOutput(pub(crate) LocalOutput);

impl Drop for FinishOutput {
    fn drop(&mut self) {
        self.0.finish();
    }
}

impl LocalLog {
    pub(crate) fn publish(&self, log: &LogEvent, stream: StreamKind) {
        let record = LocalRecord {
            log,
            resource: &self.resource,
        };
        self.events.publish_record(stream, &record);
        self.output.submit(&record);
    }
}

type EncodedLine = Box<[u8]>;

#[derive(Clone)]
pub(crate) struct LocalOutput {
    sender: Arc<Mutex<Option<bounded::Sender<EncodedLine>>>>,
    stats: Arc<Mutex<LocalOutputStats>>,
    finished: watch::Receiver<bool>,
}

#[derive(Clone, Copy, Default, Serialize)]
pub(crate) struct LocalOutputStats {
    pub(crate) queued_records: usize,
    pub(crate) pending_records: usize,
    pub(crate) pending_bytes: usize,
    pub(crate) closed: bool,
    pub(crate) accepted: u64,
    pub(crate) written: u64,
    pub(crate) dropped: u64,
    pub(crate) queue_full: u64,
    pub(crate) byte_full: u64,
    pub(crate) closed_dropped: u64,
    pub(crate) write_failed: u64,
}

impl LocalOutput {
    pub(crate) fn new() -> Self {
        Self::with_writer(1024, 8 * 1024 * 1024, |record| {
            io::stdout().lock().write_all(record)
        })
    }

    pub(crate) fn with_writer(
        slots: usize,
        bytes: usize,
        mut write: impl FnMut(&[u8]) -> io::Result<()> + Send + 'static,
    ) -> Self {
        let (sender, mut receiver) = bounded::channel::<EncodedLine>(slots, bytes);
        let (complete, finished) = watch::channel(false);
        let stats = Arc::new(Mutex::new(LocalOutputStats::default()));
        let writing = stats.clone();
        // A blocked journal reader must never occupy the game protocol task or runtime pool.
        std::thread::spawn(move || {
            while let Some(record) = receiver.blocking_recv() {
                writing.lock().unwrap().queued_records -= 1;
                let result = write(record.value());
                let mut stats = writing.lock().unwrap();
                stats.pending_records -= 1;
                stats.pending_bytes -= record.value().len();
                drop(record);
                if result.is_ok() {
                    stats.written += 1;
                } else {
                    stats.dropped += 1;
                    stats.write_failed += 1;
                }
            }
            complete.send_replace(true);
        });
        Self {
            sender: Arc::new(Mutex::new(Some(sender))),
            stats,
            finished,
        }
    }

    fn submit(&self, record: &LocalRecord<'_>) {
        let mut framed = Vec::with_capacity(128);
        framed.extend_from_slice(b"DST_RECORD|");
        if serde_json::to_writer(&mut framed, record).is_err() {
            self.stats.lock().unwrap().dropped += 1;
            return;
        }
        framed.push(b'\n');
        self.enqueue(framed.into_boxed_slice());
    }

    fn enqueue(&self, record: EncodedLine) -> bool {
        let bytes = record.len();
        let sender = self.sender.lock().unwrap();
        let mut stats = self.stats.lock().unwrap();
        let result = sender
            .as_ref()
            .ok_or(TrySendError::Closed)
            .and_then(|sender| sender.try_send(record, bytes));
        match result {
            Ok(()) => {
                stats.accepted += 1;
                stats.queued_records += 1;
                stats.pending_records += 1;
                stats.pending_bytes += bytes;
                return true;
            }
            Err(TrySendError::Slots) => stats.queue_full += 1,
            Err(TrySendError::Bytes) => stats.byte_full += 1,
            Err(TrySendError::Closed) => stats.closed_dropped += 1,
        }
        stats.dropped += 1;
        false
    }

    pub(crate) fn dropped(&self) -> u64 {
        self.stats.lock().unwrap().dropped
    }

    pub(crate) fn stats(&self) -> LocalOutputStats {
        *self.stats.lock().unwrap()
    }

    pub(crate) fn finish(&self) {
        let mut sender = self.sender.lock().unwrap();
        self.stats.lock().unwrap().closed = true;
        sender.take();
    }

    pub(crate) async fn close(&self) -> bool {
        self.finish();
        timeout(
            Duration::from_secs(5),
            self.finished.clone().wait_for(|done| *done),
        )
        .await
        .is_ok_and(|result| result.is_ok())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn blocked_writer_retains_its_bytes_and_drains_after_any_producer_finishes() {
        let (started, mut writing) = tokio::sync::mpsc::unbounded_channel();
        let (release, resume) = std::sync::mpsc::channel();
        let output = LocalOutput::with_writer(1, 4, move |record| {
            started.send(record.to_vec()).unwrap();
            resume.recv_timeout(Duration::from_secs(5)).unwrap();
            Ok(())
        });
        let producer = output.clone();
        assert!(producer.enqueue(b"aa".as_slice().into()));
        assert_eq!(writing.recv().await.unwrap(), b"aa");
        assert_eq!(output.stats().queued_records, 0);
        assert_eq!(output.stats().pending_bytes, 2);
        assert!(!producer.enqueue(b"bbb".as_slice().into()));
        assert!(producer.enqueue(b"cc".as_slice().into()));
        assert!(!producer.enqueue(b"".as_slice().into()));
        let stats = output.stats();
        assert_eq!(
            (
                stats.queued_records,
                stats.pending_records,
                stats.pending_bytes
            ),
            (1, 2, 4)
        );
        assert_eq!((stats.byte_full, stats.queue_full), (1, 1));
        tokio::time::pause();
        assert!(!output.close().await);
        tokio::time::resume();
        assert!(producer.stats().closed);
        assert_eq!(producer.stats().pending_bytes, 4);
        assert!(!producer.enqueue(b"dd".as_slice().into()));
        release.send(()).unwrap();
        assert_eq!(writing.recv().await.unwrap(), b"cc");
        release.send(()).unwrap();
        assert!(output.close().await);
        let stats = producer.stats();
        assert_eq!(
            (stats.accepted, stats.written, stats.closed_dropped),
            (2, 2, 1)
        );
        assert_eq!((stats.pending_records, stats.pending_bytes), (0, 0));
    }
}
