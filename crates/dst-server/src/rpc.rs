//! Local Cap'n Proto transport. Accepted operations belong to the room dispatcher.

use std::cell::Cell;
use std::collections::BTreeMap;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use capnp_rpc::{RpcSystem, rpc_twoparty_capnp, twoparty};
use futures::AsyncReadExt;
use serde_json::{Value, json};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{
    Mutex as AsyncMutex, Notify, OwnedSemaphorePermit, Semaphore, broadcast, mpsc, oneshot, watch,
};
use tokio_util::compat::TokioAsyncReadCompatExt;

use crate::model::{Envelope, Error, ErrorCode, Scope};
pub use crate::room_capnp::StreamKind;
use crate::room_capnp::{self, room, subscription};

mod codec;
use codec::read_outcome;

const MAX_MESSAGE: usize = 1024 * 1024;
const MAX_OUTCOME: usize = 8 * 1024 * 1024;
const MAX_EVENT: usize = MAX_MESSAGE - 32;
const STREAM_ITEMS: usize = 128;
const STREAM_BYTES: usize = 8 * 1024 * 1024;
const MAX_SUBSCRIPTIONS: usize = 128;
const DRAIN_TIMEOUT: Duration = Duration::from_secs(2);
const fn reader_options(limit: usize) -> capnp::message::ReaderOptions {
    capnp::message::ReaderOptions {
        traversal_limit_in_words: Some(4 * limit / 8),
        // Dynamic objects use a list of entries plus a value pointer at each level.
        nesting_limit: 256,
    }
}

pub type Result<T> = std::result::Result<T, Error>;

fn transport(message: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Transport, message.to_string())
}

fn rpc_error(failure: capnp::Error) -> Error {
    if failure.kind == capnp::ErrorKind::Overloaded {
        Error::new(ErrorCode::Busy, failure.to_string())
    } else {
        transport(failure)
    }
}

/// Receiving this message transfers request ownership to the Agent.
pub struct Incoming {
    pub request: Envelope,
    pub reply: oneshot::Sender<Result<Value>>,
}

impl StreamKind {
    pub(crate) const ALL: [Self; 3] = [Self::Logs, Self::Lifecycle, Self::Events];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Logs => "logs",
            Self::Lifecycle => "lifecycle",
            Self::Events => "events",
        }
    }
}

impl std::str::FromStr for StreamKind {
    type Err = Error;

    fn from_str(name: &str) -> Result<Self> {
        Self::ALL
            .into_iter()
            .find(|kind| kind.as_str() == name)
            .ok_or_else(|| {
                Error::new(
                    ErrorCode::Invalid,
                    "subscription kind must be logs, lifecycle or events",
                )
            })
    }
}

// Rejected records use an empty slot so every reader can account for the loss.
type Event = Option<Arc<StoredEvent>>;

struct StoredEvent {
    value: codec::EncodedValue,
    storage: Arc<StreamStorage>,
    _bytes: OwnedSemaphorePermit,
}

impl Drop for StoredEvent {
    fn drop(&mut self) {
        self.storage.records.fetch_sub(1, Ordering::Relaxed);
    }
}

struct StreamStorage {
    bytes: Arc<Semaphore>,
    records: AtomicUsize,
}

struct Stream {
    sender: Option<broadcast::Sender<Event>>,
    storage: Arc<StreamStorage>,
    stats: StreamStats,
}

#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct StreamStats {
    pub accepted: u64,
    pub invalid: u64,
    pub too_large: u64,
    pub byte_full: u64,
    pub pending_records: usize,
    pub pending_bytes: usize,
    pub closed: bool,
}

#[derive(Clone)]
pub struct EventHub(Arc<[Mutex<Stream>; StreamKind::ALL.len()]>);

impl Default for EventHub {
    fn default() -> Self {
        Self(Arc::new(StreamKind::ALL.map(|_| {
            Mutex::new(Stream {
                sender: Some(broadcast::channel(STREAM_ITEMS).0),
                storage: Arc::new(StreamStorage {
                    bytes: Arc::new(Semaphore::new(STREAM_BYTES)),
                    records: AtomicUsize::new(0),
                }),
                stats: StreamStats::default(),
            })
        })))
    }
}

impl EventHub {
    pub(crate) fn has_subscribers(&self, kind: StreamKind) -> bool {
        self.0[kind as usize]
            .lock()
            .unwrap()
            .sender
            .as_ref()
            .is_some_and(|sender| sender.receiver_count() > 0)
    }

    #[cfg(test)]
    pub(crate) fn subscribe_for_test(&self, kind: StreamKind) -> TestEvents {
        TestEvents(self.subscribe(kind).unwrap())
    }

    /// Encode once and share the native message across subscribers.
    pub fn publish(&self, kind: StreamKind, record: &Value) {
        self.publish_encoded(kind, || codec::encode_value(record, MAX_EVENT));
    }

    pub(crate) fn publish_record(&self, kind: StreamKind, record: &impl serde::Serialize) {
        self.publish_encoded(kind, || {
            let value = serde_json::to_value(record)
                .map_err(|error| Error::new(ErrorCode::Invalid, error.to_string()))?;
            codec::encode_value(&value, MAX_EVENT)
        });
    }

    fn publish_encoded(
        &self,
        kind: StreamKind,
        encode: impl FnOnce() -> Result<codec::EncodedValue>,
    ) {
        let mut guard = self.0[kind as usize].lock().unwrap();
        let stream = &mut *guard;
        let Some(sender) = &stream.sender else { return };
        if sender.receiver_count() == 0 {
            return;
        }
        let (event, counter) = match encode() {
            Ok(value) => {
                let bytes = value.expanded_bytes.max(value.native_bytes);
                match stream
                    .storage
                    .bytes
                    .clone()
                    .try_acquire_many_owned(bytes as u32)
                {
                    Ok(permit) => {
                        stream.storage.records.fetch_add(1, Ordering::Relaxed);
                        (
                            Some(Arc::new(StoredEvent {
                                value,
                                storage: stream.storage.clone(),
                                _bytes: permit,
                            })),
                            &mut stream.stats.accepted,
                        )
                    }
                    Err(_) => (None, &mut stream.stats.byte_full),
                }
            }
            Err(error) if error.code == ErrorCode::Overflow => (None, &mut stream.stats.too_large),
            Err(_) => (None, &mut stream.stats.invalid),
        };
        if sender.send(event).is_ok() {
            *counter += 1;
        }
    }

    fn subscribe(&self, kind: StreamKind) -> Result<broadcast::Receiver<Event>> {
        // Acquire in schema order so concurrent registrations share one subscription limit.
        let streams = self.0.each_ref().map(|stream| stream.lock().unwrap());
        let sender = streams[kind as usize]
            .sender
            .as_ref()
            .ok_or_else(|| transport("event hub is closed"))?;
        if streams
            .iter()
            .filter_map(|stream| stream.sender.as_ref())
            .map(broadcast::Sender::receiver_count)
            .sum::<usize>()
            >= MAX_SUBSCRIPTIONS
        {
            return Err(Error::new(ErrorCode::Busy, "subscription limit reached"));
        }
        Ok(sender.subscribe())
    }

    pub fn close(&self) {
        for mut stream in self.0.each_ref().map(|stream| stream.lock().unwrap()) {
            stream.sender = None;
        }
    }

    pub fn stats(&self) -> BTreeMap<&'static str, StreamStats> {
        StreamKind::ALL
            .into_iter()
            .map(|kind| {
                let stream = self.0[kind as usize].lock().unwrap();
                let mut stats = stream.stats.clone();
                stats.closed = stream.sender.is_none();
                stats.pending_records = stream.storage.records.load(Ordering::Relaxed);
                stats.pending_bytes = STREAM_BYTES - stream.storage.bytes.available_permits();
                (kind.as_str(), stats)
            })
            .collect()
    }
}

#[cfg(test)]
pub(crate) struct TestEvents(broadcast::Receiver<Event>);

#[cfg(test)]
impl TestEvents {
    pub(crate) fn drain(&mut self) -> Vec<Value> {
        let mut records = Vec::new();
        loop {
            match self.0.try_recv() {
                Ok(Some(event)) => records.push(
                    codec::read_value(event.value.message.get_root_as_reader().unwrap()).unwrap(),
                ),
                Err(
                    broadcast::error::TryRecvError::Empty | broadcast::error::TryRecvError::Closed,
                ) => return records,
                _ => panic!("test observations exceeded their subscription budget"),
            }
        }
    }
}

pub(crate) fn validate_static_context(record: &Value) -> Result<()> {
    codec::encode_value(record, 128 * 1024).map(|_| ())
}

struct SubscriptionState {
    receiver: broadcast::Receiver<Event>,
    carry: Option<Arc<StoredEvent>>,
}

struct SubscriptionServer {
    state: AsyncMutex<Option<SubscriptionState>>,
    closed: watch::Sender<bool>,
    pending: Rc<PendingCalls>,
}

impl subscription::Server for SubscriptionServer {
    async fn next(
        self: Rc<Self>,
        params: subscription::NextParams,
        mut results: subscription::NextResults,
    ) -> capnp::Result<()> {
        use broadcast::error::{RecvError, TryRecvError};

        let _pending = self.pending.enter();
        let count = usize::from(params.get()?.get_max_items());
        if !(1..=512).contains(&count) {
            return Err(capnp::Error::failed("max_items must be in 1..=512".into()));
        }
        let mut active = self.state.try_lock().map_err(|_| {
            capnp::Error::overloaded("subscription already has a pending read".into())
        })?;
        let mut closing = self.closed.subscribe();
        let Some(state) = active.as_mut().filter(|_| !*closing.borrow_and_update()) else {
            results.get().init_batch().set_closed(true);
            return Ok(());
        };
        let mut closed = false;
        let mut records = Vec::with_capacity(count);
        let mut dropped = 0u64;
        let mut expanded_bytes = 8; // The decoded list tag.
        let mut native_bytes = 32; // The batch struct and list tag.
        let mut item = if let Some(record) = state.carry.take() {
            Ok(Some(record))
        } else {
            tokio::select! {
                biased;
                _ = closing.changed() => Err(RecvError::Closed),
                item = state.receiver.recv() => item,
            }
        };
        // Empty slots and lag reports also consume a bounded scan step.
        for scanned in 1..=512 {
            match item {
                Ok(Some(record)) => {
                    if record.value.expanded_bytes > MAX_MESSAGE - expanded_bytes
                        || record.value.native_bytes > MAX_MESSAGE - native_bytes
                    {
                        if records.is_empty() {
                            dropped = dropped.saturating_add(1);
                        } else {
                            state.carry = Some(record);
                            break;
                        }
                    } else {
                        expanded_bytes += record.value.expanded_bytes;
                        native_bytes += record.value.native_bytes;
                        records.push(record);
                    }
                }
                Ok(None) => dropped = dropped.saturating_add(1),
                Err(RecvError::Lagged(skipped)) => dropped = dropped.saturating_add(skipped),
                Err(RecvError::Closed) => {
                    closed = true;
                    break;
                }
            }
            if records.len() == count || scanned == 512 {
                break;
            }
            item = match state.receiver.try_recv() {
                Ok(record) => Ok(record),
                Err(TryRecvError::Lagged(skipped)) => Err(RecvError::Lagged(skipped)),
                Err(TryRecvError::Closed) => Err(RecvError::Closed),
                Err(TryRecvError::Empty) => break,
            };
        }
        closed |= state.carry.is_none() && state.receiver.is_closed() && state.receiver.is_empty();
        let mut batch = results.get().init_batch();
        batch.set_dropped(dropped);
        batch.set_closed(closed);
        let mut output = batch.reborrow().init_records(records.len() as u32);
        for (index, record) in records.iter().enumerate() {
            output.set_with_caveats(index as u32, record.value.message.get_root_as_reader()?)?;
        }
        codec::check_size(batch.into_reader().total_size()?, MAX_MESSAGE)
            .map_err(|error| capnp::Error::failed(error.message))?;
        Ok(())
    }

    async fn close(
        self: Rc<Self>,
        _: subscription::CloseParams,
        _: subscription::CloseResults,
    ) -> capnp::Result<()> {
        self.closed.send_replace(true);
        // Drop the receiver after waking a pending read to release its subscription slot.
        self.state.lock().await.take();
        Ok(())
    }
}

#[derive(Default)]
struct PendingCalls {
    count: Cell<usize>,
    closing: Cell<bool>,
    idle: Notify,
}

impl PendingCalls {
    fn enter(self: &Rc<Self>) -> PendingCall {
        self.count.set(self.count.get() + 1);
        PendingCall(self.clone())
    }

    async fn drain(&self) {
        while self.count.get() != 0 {
            self.idle.notified().await;
        }
    }
}

struct PendingCall(Rc<PendingCalls>);

impl Drop for PendingCall {
    fn drop(&mut self) {
        self.0.count.set(self.0.count.get() - 1);
        if self.0.count.get() == 0 {
            self.0.idle.notify_one();
        }
    }
}

struct RoomServer {
    dispatch: mpsc::Sender<Incoming>,
    events: EventHub,
    pending: Rc<PendingCalls>,
}

fn set_outcome(
    mut builder: room_capnp::outcome::Builder<'_>,
    result: Result<Value>,
) -> capnp::Result<()> {
    let encoded = codec::encode_outcome(&result)
        .or_else(|error| codec::encode_outcome(&Err(error)))
        .map_err(|error| capnp::Error::failed(error.to_string()))?;
    match encoded.get_root_as_reader()?.which()? {
        room_capnp::outcome::Value(value) => builder.set_value(value?)?,
        room_capnp::outcome::Error(error) => builder.set_error(error?)?,
    }
    Ok(())
}

impl room::Server for RoomServer {
    async fn call(
        self: Rc<Self>,
        params: room::CallParams,
        mut results: room::CallResults,
    ) -> capnp::Result<()> {
        if self.pending.closing.get() {
            return set_outcome(
                results.get().init_result(),
                Err(Error::new(ErrorCode::NotReady, "room server is closing")),
            );
        }
        let _pending = self.pending.enter();
        let request = codec::read_envelope(params.get()?.get_request()?);
        let outcome = match request {
            Err(error) => Err(error),
            Ok(request) => {
                let (reply, response) = oneshot::channel();
                match self.dispatch.try_send(Incoming { request, reply }) {
                    Err(mpsc::error::TrySendError::Full(_)) => {
                        Err(Error::new(ErrorCode::Busy, "room request queue is full"))
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => {
                        Err(transport("room dispatcher is closed"))
                    }
                    Ok(()) => response.await.map_err(|_| {
                        capnp::Error::failed("room dispatcher stopped before replying".into())
                    })?,
                }
            }
        };
        set_outcome(results.get().init_result(), outcome)
    }

    async fn describe(
        self: Rc<Self>,
        _: room::DescribeParams,
        mut results: room::DescribeResults,
    ) -> capnp::Result<()> {
        set_outcome(
            results.get().init_result(),
            Ok(json!({
                "room": crate::model::describe(Scope::Room),
                "shard": crate::model::describe(Scope::Shard),
            })),
        )
    }

    async fn subscribe(
        self: Rc<Self>,
        params: room::SubscribeParams,
        mut results: room::SubscribeResults,
    ) -> capnp::Result<()> {
        if self.pending.closing.get() {
            return Err(capnp::Error::disconnected("room server is closing".into()));
        }
        let receiver =
            self.events
                .subscribe(params.get()?.get_kind()?)
                .map_err(|error| match error.code {
                    ErrorCode::Busy => capnp::Error::overloaded(error.message),
                    _ => capnp::Error::disconnected(error.message),
                })?;
        results
            .get()
            .set_subscription(capnp_rpc::new_client(SubscriptionServer {
                state: AsyncMutex::new(Some(SubscriptionState {
                    receiver,
                    carry: None,
                })),
                closed: watch::channel(false).0,
                pending: self.pending.clone(),
            }));
        Ok(())
    }
}

/// The caller holds the room lock and runs this future inside its Tokio LocalSet.
pub async fn serve(
    path: &Path,
    dispatch: mpsc::Sender<Incoming>,
    events: EventHub,
    mut shutdown: oneshot::Receiver<()>,
) -> Result<()> {
    if let Ok(metadata) = std::fs::symlink_metadata(path) {
        if !metadata.file_type().is_socket() {
            return Err(Error::new(
                ErrorCode::Invalid,
                "socket path is not a Unix socket",
            ));
        }
        match UnixStream::connect(path).await {
            Ok(_) => {
                return Err(Error::new(
                    ErrorCode::Busy,
                    "another Agent is listening on the socket",
                ));
            }
            Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {
                std::fs::remove_file(path).map_err(transport)?
            }
            Err(e) => return Err(transport(e)),
        }
    }
    let listener = UnixListener::bind(path).map_err(transport)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).map_err(transport)?;
    let metadata = std::fs::symlink_metadata(path).map_err(transport)?;
    struct SocketFile {
        path: PathBuf,
        inode: u64,
        device: u64,
    }
    impl Drop for SocketFile {
        fn drop(&mut self) {
            if std::fs::symlink_metadata(&self.path)
                .is_ok_and(|m| m.ino() == self.inode && m.dev() == self.device)
            {
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }
    let _socket = SocketFile {
        path: path.to_owned(),
        inode: metadata.ino(),
        device: metadata.dev(),
    };
    let pending = Rc::new(PendingCalls::default());
    let endpoint: room::Client = capnp_rpc::new_client(RoomServer {
        dispatch,
        events: events.clone(),
        pending: pending.clone(),
    });
    let (disconnect, _) = watch::channel(false);
    let mut connections = tokio::task::JoinSet::new();
    let result = loop {
        tokio::select! {
            biased;
            _ = &mut shutdown => break Ok(()),
            Some(_) = connections.join_next(), if !connections.is_empty() => {},
            connection = listener.accept() => {
                let (stream, _) = match connection {
                    Ok(connection) => connection,
                    Err(failure) => break Err(transport(failure)),
                };
                let Ok(credentials) = stream.peer_cred() else { continue; };
                let uid = credentials.uid();
                // SAFETY: geteuid has no pointer arguments or side effects.
                if (uid != 0 && uid != unsafe { libc::geteuid() }) || connections.len() >= 64 { continue; }
                let (reader, writer) = stream.compat().split();
                let network = twoparty::VatNetwork::new(reader, writer, rpc_twoparty_capnp::Side::Server, reader_options(MAX_MESSAGE));
                let rpc = RpcSystem::new(Box::new(network), Some(endpoint.clone().client));
                let disconnected = rpc.get_disconnector();
                let mut closing = disconnect.subscribe();
                connections.spawn_local(async move {
                    tokio::pin!(rpc);
                    tokio::select! {
                        result = &mut rpc => result,
                        _ = closing.changed() => tokio::select! {
                            result = &mut rpc => result,
                            result = disconnected => result,
                        },
                    }
                });
            }
        }
    };
    drop(listener);
    pending.closing.set(true);
    events.close();
    // Complete handlers before Cap'n Proto flushes their queued return messages.
    let _ = tokio::time::timeout(DRAIN_TIMEOUT, pending.drain()).await;
    disconnect.send_replace(true);
    let _ = tokio::time::timeout(DRAIN_TIMEOUT, async {
        while connections.join_next().await.is_some() {}
    })
    .await;
    connections.shutdown().await;
    result
}

type CallReply = oneshot::Sender<Result<Result<Value>>>;

enum ClientMessage {
    Call(codec::EnvelopeMessage, CallReply),
    Describe(CallReply),
    Subscribe(StreamKind, Client, oneshot::Sender<Result<Subscription>>),
    Close(oneshot::Sender<()>),
}

struct Connect {
    path: PathBuf,
    reply: oneshot::Sender<Result<Client>>,
}

fn broker() -> Result<&'static mpsc::Sender<Connect>> {
    static BROKER: OnceLock<std::result::Result<mpsc::Sender<Connect>, String>> = OnceLock::new();
    BROKER
        .get_or_init(|| {
            let (sender, mut requests) = mpsc::channel::<Connect>(64);
            std::thread::Builder::new()
                .name("dst-rpc".into())
                .spawn(move || {
                    let runtime = match tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                    {
                        Ok(runtime) => runtime,
                        Err(_) => return,
                    };
                    runtime.block_on(tokio::task::LocalSet::new().run_until(async move {
                        while let Some(request) = requests.recv().await {
                            tokio::task::spawn_local(async move {
                                let result = connect(request.path).await;
                                let _ = request.reply.send(result);
                            });
                        }
                    }));
                })
                .map_err(|error| error.to_string())?;
            Ok(sender)
        })
        .as_ref()
        .map_err(transport)
}

/// Sendable client; all native Cap'n Proto capabilities live on one shared LocalSet.
#[derive(Clone)]
pub struct Client {
    requests: mpsc::Sender<ClientMessage>,
}

impl Client {
    pub async fn connect(path: impl AsRef<Path>) -> Result<Self> {
        let (reply, response) = oneshot::channel();
        broker()?
            .try_send(Connect {
                path: path.as_ref().to_owned(),
                reply,
            })
            .map_err(|_| Error::new(ErrorCode::Busy, "RPC connection queue is unavailable"))?;
        response.await.map_err(transport)?
    }

    pub async fn call(&self, request: Envelope) -> Result<Value> {
        let timeout = request.timeout()?;
        let mutation = request.request.metadata().mutation;
        let encoded = codec::encode_envelope(&request)?;
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Call(encoded, reply))?;
        let result = match tokio::time::timeout(timeout, response).await {
            Ok(Ok(result)) => result,
            Ok(Err(failure)) => Err(transport(failure)),
            Err(_) if mutation => Err(Error::new(
                ErrorCode::Unknown,
                "wait timed out before the mutation result was confirmed",
            )),
            Err(_) => Err(Error::new(ErrorCode::Timeout, "query wait timed out")),
        };
        result.map_err(|mut failure| {
            if mutation && failure.code == ErrorCode::Transport {
                failure.code = ErrorCode::Unknown;
                failure.message = format!(
                    "mutation result could not be confirmed ({})",
                    failure.message
                );
            }
            failure
        })?
    }

    pub async fn describe(&self) -> Result<Value> {
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Describe(reply))?;
        response.await.map_err(transport)??
    }

    pub async fn subscribe(&self, kind: StreamKind) -> Result<Subscription> {
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Subscribe(kind, self.clone(), reply))?;
        response.await.map_err(transport)?
    }

    pub async fn close(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        if self
            .requests
            .send(ClientMessage::Close(reply))
            .await
            .is_ok()
        {
            let _ = response.await;
        }
        Ok(())
    }

    fn send(&self, message: ClientMessage) -> Result<()> {
        self.requests
            .try_send(message)
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => {
                    Error::new(ErrorCode::Busy, "RPC request queue is full")
                }
                mpsc::error::TrySendError::Closed(_) => transport("RPC connection is closed"),
            })
    }
}

async fn connect(path: PathBuf) -> Result<Client> {
    let stream = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        UnixStream::connect(path),
    )
    .await
    .map_err(transport)?
    .map_err(transport)?;
    let (reader, writer) = stream.compat().split();
    let network = twoparty::VatNetwork::new(
        reader,
        writer,
        rpc_twoparty_capnp::Side::Client,
        reader_options(MAX_OUTCOME),
    );
    let mut rpc = RpcSystem::new(Box::new(network), None);
    let endpoint: room::Client = rpc.bootstrap(rpc_twoparty_capnp::Side::Server);
    let disconnect = rpc.get_disconnector();
    let mut running = tokio::task::spawn_local(rpc);
    let (requests, mut incoming) = mpsc::channel(64);
    tokio::task::spawn_local(async move {
        let mut calls = tokio::task::JoinSet::new();
        let mut close_reply = None;
        let mut rpc_finished = false;
        loop {
            tokio::select! {
                biased;
                _ = &mut running => { rpc_finished = true; break; },
                Some(_) = calls.join_next(), if !calls.is_empty() => {},
                message = incoming.recv() => {
                    match message {
                        None => break,
                        Some(ClientMessage::Close(reply)) => { close_reply = Some(reply); break; },
                        Some(ClientMessage::Call(_, reply) | ClientMessage::Describe(reply)) if calls.len() >= 64 => {
                            let _ = reply.send(Err(Error::new(ErrorCode::Busy, "64 RPC requests are already pending")));
                        }
                        Some(ClientMessage::Subscribe(_, _, reply)) if calls.len() >= 64 => {
                            let _ = reply.send(Err(Error::new(ErrorCode::Busy, "64 RPC requests are already pending")));
                        }
                        Some(ClientMessage::Call(encoded, reply)) => {
                            let mut request = endpoint.call_request();
                            let prepared = encoded.get_root_as_reader().and_then(|value| request.get().set_request(value));
                            if let Err(error) = prepared {
                                let _ = reply.send(Err(Error::new(ErrorCode::Internal, error.to_string())));
                                continue;
                            }
                            let promise = request.send().promise;
                            calls.spawn_local(reply_or_cancel(reply, async move {
                                    let response = promise.await.map_err(rpc_error)?;
                                    read_outcome(response.get().map_err(transport)?.get_result().map_err(transport)?)
                            }));
                        }
                        Some(ClientMessage::Describe(reply)) => {
                            let promise = endpoint.describe_request().send().promise;
                            calls.spawn_local(reply_or_cancel(reply, async move {
                                    let response = promise.await.map_err(rpc_error)?;
                                    read_outcome(response.get().map_err(transport)?.get_result().map_err(transport)?)
                            }));
                        }
                        Some(ClientMessage::Subscribe(kind, connection, reply)) => {
                            let mut request = endpoint.subscribe_request();
                            request.get().set_kind(kind);
                            let promise = request.send().promise;
                            calls.spawn_local(reply_or_cancel(reply, async move {
                                    let response = promise.await.map_err(rpc_error)?;
                                    let endpoint = response.get().map_err(transport)?.get_subscription().map_err(transport)?;
                                    Ok(subscription_client(endpoint, connection))
                            }));
                        }
                    }
                }
            }
        }
        if rpc_finished {
            // A peer can close immediately after its final successful response.
            let _ = tokio::time::timeout(DRAIN_TIMEOUT, async {
                while calls.join_next().await.is_some() {}
            })
            .await;
        }
        calls.shutdown().await;
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), disconnect).await;
        running.abort();
        if !rpc_finished {
            let _ = running.await;
        }
        if let Some(reply) = close_reply {
            let _ = reply.send(());
        }
    });
    Ok(Client { requests })
}

async fn reply_or_cancel<T>(
    mut reply: oneshot::Sender<Result<T>>,
    response: impl std::future::Future<Output = Result<T>>,
) {
    let result = tokio::select! {
        _ = reply.closed() => return,
        result = response => result,
    };
    let _ = reply.send(result);
}

#[derive(Debug, serde::Serialize, serde::Deserialize)]
pub struct Batch {
    pub records: Vec<Value>,
    pub dropped: u64,
    pub closed: bool,
}

enum SubscriptionMessage {
    Next(u16, oneshot::Sender<Result<Batch>>),
    Close(oneshot::Sender<Result<()>>),
}

#[derive(Clone)]
pub struct Subscription {
    requests: mpsc::Sender<SubscriptionMessage>,
}

impl Subscription {
    pub async fn next(&self, max_items: u16) -> Result<Batch> {
        if !(1..=512).contains(&max_items) {
            return Err(Error::new(
                ErrorCode::Invalid,
                "max_items must be in 1..=512",
            ));
        }
        let (reply, response) = oneshot::channel();
        self.requests
            .send(SubscriptionMessage::Next(max_items, reply))
            .await
            .map_err(|_| transport("subscription is closed"))?;
        response.await.map_err(transport)?
    }

    pub async fn close(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        if self
            .requests
            .send(SubscriptionMessage::Close(reply))
            .await
            .is_ok()
        {
            return response.await.unwrap_or(Ok(()));
        }
        Ok(())
    }
}

fn subscription_client(endpoint: subscription::Client, connection: Client) -> Subscription {
    let (requests, mut incoming) = mpsc::channel(1);
    tokio::task::spawn_local(async move {
        // A returned capability keeps its connection until this subscription closes.
        let _connection = connection;
        let mut reads = tokio::task::JoinSet::new();
        loop {
            let message = tokio::select! {
                biased;
                Some(_) = reads.join_next(), if !reads.is_empty() => continue,
                message = incoming.recv() => message,
            };
            let Some(message) = message else {
                break;
            };
            match message {
                SubscriptionMessage::Next(_, reply) if !reads.is_empty() => {
                    let _ = reply.send(Err(Error::new(
                        ErrorCode::Busy,
                        "subscription already has a pending read",
                    )));
                }
                SubscriptionMessage::Next(count, reply) => {
                    let mut request = endpoint.next_request();
                    request.get().set_max_items(count);
                    reads.spawn_local(reply_or_cancel(reply, async move {
                        let response = request.send().promise.await.map_err(rpc_error)?;
                        let batch = response
                            .get()
                            .map_err(transport)?
                            .get_batch()
                            .map_err(transport)?;
                        codec::check_size(batch.total_size().map_err(transport)?, MAX_MESSAGE)?;
                        let records = codec::read_values(batch.get_records().map_err(transport)?)
                            .map_err(transport)?;
                        Ok(Batch {
                            records,
                            dropped: batch.get_dropped(),
                            closed: batch.get_closed(),
                        })
                    }));
                }
                SubscriptionMessage::Close(reply) => {
                    let result = close_subscription(&endpoint).await;
                    let _ = tokio::time::timeout(std::time::Duration::from_secs(1), async {
                        while reads.join_next().await.is_some() {}
                    })
                    .await;
                    reads.shutdown().await;
                    let _ = reply.send(result);
                    return;
                }
            }
        }
        reads.shutdown().await;
        let _ = close_subscription(&endpoint).await;
    });
    Subscription { requests }
}

async fn close_subscription(endpoint: &subscription::Client) -> Result<()> {
    tokio::time::timeout(
        std::time::Duration::from_secs(2),
        endpoint.close_request().send().promise,
    )
    .await
    .map_err(|_| Error::new(ErrorCode::Timeout, "subscription close timed out"))?
    .map(|_| ())
    .or_else(|failure| {
        if failure.kind == capnp::ErrorKind::Disconnected {
            Ok(())
        } else {
            Err(transport(failure))
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn another_capability_cannot_dispatch_a_room_call() {
        tokio::task::LocalSet::new()
            .run_until(async {
                let (dispatch, mut incoming) = mpsc::channel(1);
                let endpoint: room::Client = capnp_rpc::new_client(RoomServer {
                    dispatch,
                    events: EventHub::default(),
                    pending: Rc::new(PendingCalls::default()),
                });
                let request = endpoint
                    .client
                    .new_call::<room::call_params::Owned, room::call_results::Owned>(
                        <subscription::Client as capnp::traits::HasTypeId>::TYPE_ID,
                        0,
                        None,
                    );
                let error = request
                    .send()
                    .promise
                    .await
                    .err()
                    .expect("different capability must be rejected");
                assert_eq!(error.kind, capnp::ErrorKind::Unimplemented);
                assert!(incoming.try_recv().is_err());
            })
            .await;
    }

    #[test]
    fn subscription_limit_is_shared_across_kinds_and_released_on_drop() {
        let hub = EventHub::default();
        assert_eq!(
            "unknown".parse::<StreamKind>().unwrap_err().code,
            ErrorCode::Invalid
        );
        let mut subscribers: Vec<_> = (0..MAX_SUBSCRIPTIONS)
            .map(|_| hub.subscribe(StreamKind::Events).unwrap())
            .collect();
        assert_eq!(
            hub.subscribe(StreamKind::Logs).unwrap_err().code,
            ErrorCode::Busy
        );
        subscribers.pop();
        let _logs = hub.subscribe(StreamKind::Logs).unwrap();
        assert_eq!(
            hub.subscribe(StreamKind::Lifecycle).unwrap_err().code,
            ErrorCode::Busy
        );
    }

    #[tokio::test]
    async fn bounded_marker_scan_keeps_the_following_record() {
        tokio::task::LocalSet::new()
            .run_until(async {
                for skipped in [0, 7] {
                    let hub = EventHub::default();
                    hub.0[StreamKind::Events as usize].lock().unwrap().sender =
                        Some(broadcast::channel(1024).0);
                    let receiver = hub.subscribe(StreamKind::Events).unwrap();
                    {
                        let stream = hub.0[StreamKind::Events as usize].lock().unwrap();
                        let sender = stream.sender.as_ref().unwrap();
                        let markers = if skipped == 0 { 512 } else { 1023 + skipped };
                        for _ in 0..markers {
                            assert!(sender.send(None).is_ok());
                        }
                    }
                    hub.publish(StreamKind::Events, &json!({"after_markers":true}));
                    let endpoint: subscription::Client =
                        capnp_rpc::new_client(SubscriptionServer {
                            state: AsyncMutex::new(Some(SubscriptionState {
                                receiver,
                                carry: None,
                            })),
                            closed: watch::channel(false).0,
                            pending: Rc::new(PendingCalls::default()),
                        });
                    let mut dropped = 0;
                    let mut received = Vec::new();
                    for _ in 0..3 {
                        let mut request = endpoint.next_request();
                        request.get().set_max_items(512);
                        let response = request.send().promise.await.unwrap();
                        let batch = response.get().unwrap().get_batch().unwrap();
                        dropped += batch.get_dropped();
                        received.extend(codec::read_values(batch.get_records().unwrap()).unwrap());
                        if !received.is_empty() {
                            break;
                        }
                    }
                    assert_eq!(dropped, if skipped == 0 { 512 } else { 1023 + skipped });
                    assert_eq!(received, vec![json!({"after_markers":true})]);
                }
            })
            .await;
    }

    #[test]
    fn shared_byte_pressure_and_final_references_have_separate_stream_budgets() {
        let hub = EventHub::default();
        let slow = hub.subscribe(StreamKind::Events).unwrap();
        let mut fast = hub.subscribe(StreamKind::Events).unwrap();
        let mut logs = hub.subscribe(StreamKind::Logs).unwrap();
        let value = json!("x".repeat(900_000));
        hub.publish(StreamKind::Events, &value);
        let held = fast.try_recv().unwrap().unwrap();
        let bytes = held.value.expanded_bytes.max(held.value.native_bytes);
        let mut accepted = 1;
        for _ in 0..16 {
            hub.publish(StreamKind::Events, &value);
            if fast.try_recv().unwrap().is_some() {
                accepted += 1;
            }
            assert!(hub.stats()["events"].pending_bytes <= STREAM_BYTES);
        }
        let stats = hub.stats();
        assert_eq!(stats["events"].accepted, accepted);
        assert_eq!(stats["events"].byte_full, 17 - accepted);
        assert!(stats["events"].byte_full > 0);
        hub.publish(StreamKind::Logs, &value);
        assert!(logs.try_recv().unwrap().is_some());
        assert_eq!(hub.stats()["logs"].pending_bytes, 0);
        drop(slow);
        assert_eq!(hub.stats()["events"].pending_bytes, bytes);
        assert_eq!(hub.stats()["events"].pending_records, 1);
        drop(held);
        assert_eq!(hub.stats()["events"].pending_bytes, 0);
        hub.publish(StreamKind::Events, &value);
        assert!(fast.try_recv().unwrap().is_some());
        assert_eq!(hub.stats()["events"].pending_bytes, 0);
        drop(fast);
        let before = hub.stats()["events"].clone();
        hub.publish(StreamKind::Events, &json!("x".repeat(MAX_MESSAGE)));
        assert_eq!(hub.stats()["events"].too_large, before.too_large);
    }

    #[test]
    fn oversized_errors_obey_the_same_rpc_limit_as_values() {
        let large = Error::new(ErrorCode::PartialFailure, "x".repeat(MAX_OUTCOME));
        let mut encoded = capnp::message::Builder::new_default();
        set_outcome(encoded.init_root(), Err(large.clone())).unwrap();
        let result = read_outcome(encoded.get_root_as_reader().unwrap())
            .unwrap()
            .unwrap_err();
        assert_eq!(result.code, ErrorCode::Overflow);

        let mut incoming = capnp::message::Builder::new_default();
        incoming
            .init_root::<room_capnp::outcome::Builder<'_>>()
            .init_error()
            .set_message(large.message.as_str());
        let result = read_outcome(incoming.get_root_as_reader().unwrap()).unwrap_err();
        assert_eq!(result.code, ErrorCode::Overflow);
    }

    #[tokio::test]
    async fn stalled_subscription_close_is_bounded_and_releases_pending_reader() {
        use crate::room_capnp::{room, subscription};
        use capnp_rpc::{RpcSystem, rpc_twoparty_capnp, twoparty};
        use futures::AsyncReadExt;
        use std::rc::Rc;
        use tokio_util::compat::TokioAsyncReadCompatExt;

        struct SilentSubscription(Arc<Notify>);
        impl subscription::Server for SilentSubscription {
            async fn next(
                self: Rc<Self>,
                _: subscription::NextParams,
                _: subscription::NextResults,
            ) -> capnp::Result<()> {
                self.0.notify_one();
                std::future::pending().await
            }
            async fn close(
                self: Rc<Self>,
                _: subscription::CloseParams,
                _: subscription::CloseResults,
            ) -> capnp::Result<()> {
                std::future::pending().await
            }
        }
        struct SilentRoom(Arc<Notify>);
        impl room::Server for SilentRoom {
            async fn subscribe(
                self: Rc<Self>,
                _: room::SubscribeParams,
                mut results: room::SubscribeResults,
            ) -> capnp::Result<()> {
                results
                    .get()
                    .set_subscription(capnp_rpc::new_client(SilentSubscription(self.0.clone())));
                Ok(())
            }
        }
        tokio::task::LocalSet::new()
            .run_until(async {
                let directory = tempfile::tempdir().unwrap();
                let path = directory.path().join("silent-subscription.sock");
                let listener = tokio::net::UnixListener::bind(&path).unwrap();
                let started = Arc::new(Notify::new());
                let accepted = started.clone();
                let server = tokio::task::spawn_local(async move {
                    let (peer, _) = listener.accept().await.unwrap();
                    let (reader, writer) = peer.compat().split();
                    let network = twoparty::VatNetwork::new(
                        reader,
                        writer,
                        rpc_twoparty_capnp::Side::Server,
                        Default::default(),
                    );
                    let endpoint: room::Client = capnp_rpc::new_client(SilentRoom(accepted));
                    RpcSystem::new(Box::new(network), Some(endpoint.client)).await
                });
                let client = Client::connect(&path).await.unwrap();
                let stream = client.subscribe(StreamKind::Events).await.unwrap();
                let reader = stream.clone();
                let pending = tokio::spawn(async move { reader.next(1).await });
                tokio::time::timeout(Duration::from_secs(1), started.notified())
                    .await
                    .unwrap();
                let error = tokio::time::timeout(Duration::from_secs(4), stream.close())
                    .await
                    .unwrap()
                    .unwrap_err();
                assert_eq!(error.code, ErrorCode::Timeout);
                assert!(
                    tokio::time::timeout(Duration::from_secs(1), pending)
                        .await
                        .unwrap()
                        .unwrap()
                        .is_err()
                );
                client.close().await.unwrap();
                let _ = tokio::time::timeout(Duration::from_secs(1), server)
                    .await
                    .unwrap();
            })
            .await;
    }
}
