//! Local Cap'n Proto transport. Accepted operations belong to the room dispatcher.

use std::cell::Cell;
use std::collections::BTreeMap;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use capnp_rpc::{RpcSystem, rpc_twoparty_capnp, twoparty};
use futures::AsyncReadExt;
use serde_json::{Value, json};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{Mutex as AsyncMutex, Notify, broadcast, mpsc, oneshot, watch};
use tokio_util::compat::TokioAsyncReadCompatExt;

use crate::model::{Envelope, Error, ErrorCode, Scope};
use crate::room_capnp::{self, room, subscription};

mod codec;

const MAX_MESSAGE: usize = 1024 * 1024;
const MAX_EVENT: usize = 64 * 1024;
// Each shared stream retains at most 128 native messages of at most 64 KiB.
const STREAM_ITEMS: usize = 128;
const MAX_SUBSCRIPTIONS: usize = 128;
const DRAIN_TIMEOUT: Duration = Duration::from_secs(2);
const READER_OPTIONS: capnp::message::ReaderOptions = capnp::message::ReaderOptions {
    traversal_limit_in_words: Some(4 * MAX_MESSAGE / 8),
    // Dynamic objects use a list of entries plus a value pointer at each level.
    nesting_limit: 256,
};

pub type Result<T> = std::result::Result<T, Error>;

fn transport(message: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Transport, message.to_string())
}

/// Receiving this message transfers request ownership to the Agent.
pub struct Incoming {
    pub request: Envelope,
    pub reply: oneshot::Sender<Result<Value>>,
}

// Oversized records use an empty slot so every reader can account for the loss.
type Event = Option<Arc<codec::ValueMessage>>;
type Streams = BTreeMap<&'static str, broadcast::Sender<Event>>;

#[derive(Clone)]
pub struct EventHub(Arc<Mutex<Streams>>);

impl Default for EventHub {
    fn default() -> Self {
        Self(Arc::new(Mutex::new(
            ["logs", "lifecycle", "events"]
                .map(|kind| (kind, broadcast::channel(STREAM_ITEMS).0))
                .into_iter()
                .collect(),
        )))
    }
}

impl EventHub {
    /// Encode once and share the native message across subscribers.
    /// A slow subscriber never blocks a game pipe or another subscriber.
    pub fn publish(&self, kind: &str, record: &Value) {
        if let Some(stream) = self.0.lock().unwrap().get(kind) {
            if stream.receiver_count() == 0 {
                return;
            }
            let encoded = codec::encode_value(record, MAX_EVENT).ok().map(Arc::new);
            let _ = stream.send(encoded);
        }
    }

    fn subscribe(&self, kind: &str) -> Result<broadcast::Receiver<Event>> {
        if !matches!(kind, "logs" | "lifecycle" | "events") {
            return Err(Error::new(
                ErrorCode::Invalid,
                "subscription kind must be logs, lifecycle or events",
            ));
        }
        let streams = self.0.lock().unwrap();
        let stream = streams
            .get(kind)
            .ok_or_else(|| transport("event hub is closed"))?;
        if streams
            .values()
            .map(broadcast::Sender::receiver_count)
            .sum::<usize>()
            >= MAX_SUBSCRIPTIONS
        {
            return Err(Error::new(ErrorCode::Busy, "subscription limit reached"));
        }
        Ok(stream.subscribe())
    }

    pub fn close(&self) {
        self.0.lock().unwrap().clear();
    }
}

struct SubscriptionServer {
    receiver: AsyncMutex<broadcast::Receiver<Event>>,
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
        let mut receiver = self
            .receiver
            .try_lock()
            .map_err(|_| capnp::Error::failed("subscription already has a pending read".into()))?;
        let mut closing = self.closed.subscribe();
        let mut closed = *closing.borrow_and_update();
        // Leave space for the batch struct and list tag as well as the records.
        let count = count.min((MAX_MESSAGE - 32) / MAX_EVENT);
        let mut records = Vec::with_capacity(count);
        let mut dropped = 0u64;
        if !closed {
            let mut item = tokio::select! {
                biased;
                _ = closing.changed() => Err(RecvError::Closed),
                item = receiver.recv() => item,
            };
            for index in 0..count {
                match item {
                    Ok(Some(record)) => records.push(record),
                    Ok(None) => dropped = dropped.saturating_add(1),
                    Err(RecvError::Lagged(skipped)) => dropped = dropped.saturating_add(skipped),
                    Err(RecvError::Closed) => {
                        closed = true;
                        break;
                    }
                }
                if index + 1 == count {
                    break;
                }
                item = match receiver.try_recv() {
                    Ok(record) => Ok(record),
                    Err(TryRecvError::Lagged(skipped)) => Err(RecvError::Lagged(skipped)),
                    Err(TryRecvError::Closed) => Err(RecvError::Closed),
                    Err(TryRecvError::Empty) => break,
                };
            }
            closed |= receiver.is_closed() && receiver.is_empty();
        }
        let mut batch = results.get().init_batch();
        batch.set_dropped(dropped);
        batch.set_closed(closed);
        let mut output = batch.init_records(records.len() as u32);
        for (index, record) in records.iter().enumerate() {
            output.set_with_caveats(index as u32, record.get_root_as_reader()?)?;
        }
        Ok(())
    }

    async fn close(
        self: Rc<Self>,
        _: subscription::CloseParams,
        _: subscription::CloseResults,
    ) -> capnp::Result<()> {
        self.closed.send_replace(true);
        // Drop the receiver after waking a pending read to release its subscription slot.
        *self.receiver.lock().await = broadcast::channel(1).1;
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
                    Ok(()) => response
                        .await
                        .unwrap_or_else(|_| Err(transport("room dispatcher stopped"))),
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
                "protocol": 2,
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
        let receiver = self
            .events
            .subscribe(match params.get()?.get_kind()? {
                room_capnp::StreamKind::Logs => "logs",
                room_capnp::StreamKind::Lifecycle => "lifecycle",
                room_capnp::StreamKind::Events => "events",
            })
            .map_err(|e| capnp::Error::failed(e.message))?;
        results
            .get()
            .set_subscription(capnp_rpc::new_client(SubscriptionServer {
                receiver: AsyncMutex::new(receiver),
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
                let network = twoparty::VatNetwork::new(reader, writer, rpc_twoparty_capnp::Side::Server, READER_OPTIONS);
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

enum ClientMessage {
    Call(codec::EnvelopeMessage, oneshot::Sender<Result<Value>>),
    Describe(oneshot::Sender<Result<Value>>),
    Subscribe(
        room_capnp::StreamKind,
        oneshot::Sender<Result<Subscription>>,
    ),
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
            Err(_) => Err(Error::new(
                if mutation {
                    ErrorCode::Unknown
                } else {
                    ErrorCode::Timeout
                },
                "wait timed out; query room status for the accepted operation",
            )),
        };
        result.map_err(|mut failure| {
            if mutation && failure.code == ErrorCode::Transport {
                failure.code = ErrorCode::Unknown;
                failure.message =
                    "connection ended before the mutation result was confirmed".into();
            }
            failure
        })
    }

    pub async fn describe(&self) -> Result<Value> {
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Describe(reply))?;
        response.await.map_err(transport)?
    }

    pub async fn subscribe(&self, kind: &str) -> Result<Subscription> {
        let kind = match kind {
            "logs" => room_capnp::StreamKind::Logs,
            "lifecycle" => room_capnp::StreamKind::Lifecycle,
            "events" => room_capnp::StreamKind::Events,
            _ => {
                return Err(Error::new(
                    ErrorCode::Invalid,
                    "subscription kind must be logs, lifecycle or events",
                ));
            }
        };
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Subscribe(kind, reply))?;
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

fn read_outcome(reader: room_capnp::outcome::Reader<'_>) -> Result<Value> {
    codec::check_size(reader.total_size().map_err(transport)?, MAX_MESSAGE)?;
    match reader.which().map_err(transport)? {
        room_capnp::outcome::Value(value) => {
            codec::read_value(value.map_err(transport)?).map_err(transport)
        }
        room_capnp::outcome::Error(error) => {
            Err(codec::read_error(error.map_err(transport)?).map_err(transport)?)
        }
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
        READER_OPTIONS,
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
                        Some(ClientMessage::Subscribe(_, reply)) if calls.len() >= 64 => {
                            let _ = reply.send(Err(Error::new(ErrorCode::Busy, "64 RPC requests are already pending")));
                        }
                        Some(ClientMessage::Call(encoded, reply)) => {
                            let mut request = endpoint.call_request();
                            let prepared = encoded.get_root_as_reader().and_then(|value| request.get().set_request(value));
                            if let Err(error) = prepared {
                                let _ = reply.send(Err(transport(error)));
                                continue;
                            }
                            let promise = request.send().promise;
                            calls.spawn_local(reply_or_cancel(reply, async move {
                                    let response = promise.await.map_err(transport)?;
                                    read_outcome(response.get().map_err(transport)?.get_result().map_err(transport)?)
                            }));
                        }
                        Some(ClientMessage::Describe(reply)) => {
                            let promise = endpoint.describe_request().send().promise;
                            calls.spawn_local(reply_or_cancel(reply, async move {
                                    let response = promise.await.map_err(transport)?;
                                    read_outcome(response.get().map_err(transport)?.get_result().map_err(transport)?)
                            }));
                        }
                        Some(ClientMessage::Subscribe(kind, reply)) => {
                            let mut request = endpoint.subscribe_request();
                            request.get().set_kind(kind);
                            let promise = request.send().promise;
                            calls.spawn_local(reply_or_cancel(reply, async move {
                                    let response = promise.await.map_err(transport)?;
                                    let endpoint = response.get().map_err(transport)?.get_subscription().map_err(transport)?;
                                    Ok(subscription_client(endpoint))
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

fn subscription_client(endpoint: subscription::Client) -> Subscription {
    let (requests, mut incoming) = mpsc::channel(1);
    tokio::task::spawn_local(async move {
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
                        let response = request.send().promise.await.map_err(transport)?;
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

    #[test]
    fn subscription_limit_is_shared_across_kinds_and_released_on_drop() {
        let hub = EventHub::default();
        assert_eq!(
            hub.subscribe("unknown").unwrap_err().code,
            ErrorCode::Invalid
        );
        let mut subscribers: Vec<_> = (0..MAX_SUBSCRIPTIONS)
            .map(|_| hub.subscribe("events").unwrap())
            .collect();
        assert_eq!(hub.subscribe("logs").unwrap_err().code, ErrorCode::Busy);
        subscribers.pop();
        let _logs = hub.subscribe("logs").unwrap();
        assert_eq!(
            hub.subscribe("lifecycle").unwrap_err().code,
            ErrorCode::Busy
        );
    }

    #[test]
    fn oversized_errors_obey_the_same_rpc_limit_as_values() {
        let large = Error::new(ErrorCode::PartialFailure, "x".repeat(MAX_MESSAGE));
        let mut encoded = capnp::message::Builder::new_default();
        set_outcome(encoded.init_root(), Err(large.clone())).unwrap();
        let result = read_outcome(encoded.get_root_as_reader().unwrap()).unwrap_err();
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
                let stream = client.subscribe("events").await.unwrap();
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
