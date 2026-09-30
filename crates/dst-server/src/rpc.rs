//! Local Cap'n Proto transport. Accepted operations belong to the room dispatcher.

use std::collections::VecDeque;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::{Arc, Mutex, OnceLock, Weak};

use capnp_rpc::{RpcSystem, rpc_twoparty_capnp, twoparty};
use futures::AsyncReadExt;
use serde_json::{Value, json};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{Notify, mpsc, oneshot};
use tokio_util::compat::TokioAsyncReadCompatExt;

use crate::model::{Envelope, Error, ErrorCode, Scope};
use crate::room_capnp::{self, room, subscription};

const MAX_MESSAGE: usize = 1024 * 1024;
const MAX_EVENT: usize = 64 * 1024;
const STREAM_ITEMS: usize = 1024;
const STREAM_BYTES: usize = 8 * 1024 * 1024;
const MAX_SUBSCRIPTIONS: usize = 128;

pub type Result<T> = std::result::Result<T, Error>;

fn error(code: ErrorCode, message: impl Into<String>) -> Error {
    Error {
        code,
        message: message.into(),
        details: Value::Null,
    }
}

fn transport(message: impl std::fmt::Display) -> Error {
    error(ErrorCode::Transport, message.to_string())
}

/// Receiving this message transfers request ownership to the Agent.
pub struct Incoming {
    pub request: Envelope,
    pub reply: oneshot::Sender<Result<Value>>,
}

#[derive(Default)]
struct Queue {
    records: VecDeque<Arc<[u8]>>,
    bytes: usize,
    dropped: u64,
    closed: bool,
}

struct StreamState {
    kind: String,
    queue: Mutex<Queue>,
    available: Notify,
}

#[derive(Clone, Default)]
pub struct EventHub(Arc<Mutex<Vec<Weak<StreamState>>>>);

impl EventHub {
    /// A slow subscriber never blocks a game pipe or another subscriber.
    pub fn publish(&self, kind: &str, record: &Value) {
        let encoded = serde_json::to_vec(record).ok().map(Arc::<[u8]>::from);
        let mut streams = self.0.lock().unwrap();
        streams.retain(|weak| {
            let Some(stream) = weak.upgrade() else {
                return false;
            };
            if stream.kind != kind {
                return true;
            }
            let mut queue = stream.queue.lock().unwrap();
            if queue.closed {
                return false;
            }
            if let Some(data) = encoded.as_ref().filter(|data| {
                data.len() <= MAX_EVENT
                    && queue.records.len() < STREAM_ITEMS
                    && queue.bytes + data.len() <= STREAM_BYTES
            }) {
                queue.bytes += data.len();
                queue.records.push_back(data.clone());
            } else {
                queue.dropped = queue.dropped.saturating_add(1);
            }
            drop(queue);
            stream.available.notify_one();
            true
        });
    }

    fn subscribe(&self, kind: &str) -> Result<Arc<StreamState>> {
        if !matches!(kind, "logs" | "lifecycle" | "events") {
            return Err(error(
                ErrorCode::Invalid,
                "subscription kind must be logs, lifecycle or events",
            ));
        }
        let mut streams = self.0.lock().unwrap();
        streams.retain(|stream| stream.strong_count() > 0);
        if streams.len() >= MAX_SUBSCRIPTIONS {
            return Err(error(ErrorCode::Busy, "subscription limit reached"));
        }
        let state = Arc::new(StreamState {
            kind: kind.to_owned(),
            queue: Mutex::new(Queue::default()),
            available: Notify::new(),
        });
        streams.push(Arc::downgrade(&state));
        Ok(state)
    }

    pub fn close(&self) {
        for stream in self
            .0
            .lock()
            .unwrap()
            .drain(..)
            .filter_map(|stream| stream.upgrade())
        {
            stream.queue.lock().unwrap().closed = true;
            stream.available.notify_one();
        }
    }
}

struct SubscriptionServer {
    state: Arc<StreamState>,
    reading: std::cell::Cell<bool>,
}

impl subscription::Server for SubscriptionServer {
    async fn next(
        self: Rc<Self>,
        params: subscription::NextParams,
        mut results: subscription::NextResults,
    ) -> capnp::Result<()> {
        let count = usize::from(params.get()?.get_max_items());
        if !(1..=512).contains(&count) {
            return Err(capnp::Error::failed("max_items must be in 1..=512".into()));
        }
        if self.reading.replace(true) {
            return Err(capnp::Error::failed(
                "subscription already has a pending read".into(),
            ));
        }
        struct Reading<'a>(&'a std::cell::Cell<bool>);
        impl Drop for Reading<'_> {
            fn drop(&mut self) {
                self.0.set(false);
            }
        }
        let _reading = Reading(&self.reading);
        loop {
            let available = self.state.available.notified();
            {
                let mut queue = self.state.queue.lock().unwrap();
                if !queue.records.is_empty() || queue.closed || queue.dropped > 0 {
                    let mut bytes = 0;
                    let count = queue
                        .records
                        .iter()
                        .take(count)
                        .take_while(|record| {
                            bytes += record.len();
                            bytes <= MAX_MESSAGE
                        })
                        .count();
                    let mut batch = results.get().init_batch();
                    batch.set_dropped(std::mem::take(&mut queue.dropped));
                    batch.set_closed(queue.closed && count == queue.records.len());
                    let mut records = batch.init_records(count as u32);
                    for index in 0..count {
                        let record = queue.records.pop_front().unwrap();
                        queue.bytes -= record.len();
                        records.set(index as u32, &record);
                    }
                    return Ok(());
                }
            }
            available.await;
        }
    }

    async fn close(
        self: Rc<Self>,
        _: subscription::CloseParams,
        _: subscription::CloseResults,
    ) -> capnp::Result<()> {
        let mut queue = self.state.queue.lock().unwrap();
        queue.closed = true;
        queue.records.clear();
        queue.bytes = 0;
        drop(queue);
        self.state.available.notify_one();
        Ok(())
    }
}

struct RoomServer {
    dispatch: mpsc::Sender<Incoming>,
    events: EventHub,
}

fn set_outcome(
    mut builder: room_capnp::outcome::Builder<'_>,
    result: Result<Value>,
) -> capnp::Result<()> {
    let failed = result.is_err();
    let bytes = match result {
        Ok(value) => serde_json::to_vec(&value),
        Err(error) => serde_json::to_vec(&error),
    }
    .map_err(|error| capnp::Error::failed(error.to_string()))?;
    if bytes.len() > MAX_MESSAGE {
        return set_outcome(
            builder,
            Err(error(ErrorCode::Overflow, "RPC result exceeds 1 MiB")),
        );
    }
    if failed {
        builder.set_error(&bytes);
    } else {
        builder.set_value(&bytes);
    }
    Ok(())
}

impl room::Server for RoomServer {
    async fn call(
        self: Rc<Self>,
        params: room::CallParams,
        mut results: room::CallResults,
    ) -> capnp::Result<()> {
        let bytes = params.get()?.get_request()?;
        let request = if bytes.len() > MAX_MESSAGE {
            Err(error(ErrorCode::Invalid, "RPC request exceeds 1 MiB"))
        } else {
            Envelope::from_json(bytes)
        };
        let outcome = match request {
            Err(error) => Err(error),
            Ok(request) => {
                let (reply, response) = oneshot::channel();
                match self.dispatch.try_send(Incoming { request, reply }) {
                    Err(mpsc::error::TrySendError::Full(_)) => {
                        Err(error(ErrorCode::Busy, "room request queue is full"))
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
                "protocol": 1,
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
        let state = self
            .events
            .subscribe(params.get()?.get_kind()?.to_str()?)
            .map_err(|e| capnp::Error::failed(e.message))?;
        results
            .get()
            .set_subscription(capnp_rpc::new_client(SubscriptionServer {
                state,
                reading: std::cell::Cell::new(false),
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
            return Err(error(
                ErrorCode::Invalid,
                "socket path is not a Unix socket",
            ));
        }
        match UnixStream::connect(path).await {
            Ok(_) => {
                return Err(error(
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
    let endpoint: room::Client = capnp_rpc::new_client(RoomServer {
        dispatch,
        events: events.clone(),
    });
    let mut connections = tokio::task::JoinSet::new();
    let result = loop {
        tokio::select! {
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
                let options = capnp::message::ReaderOptions { traversal_limit_in_words: Some(2 * MAX_MESSAGE / 8), nesting_limit: 64 };
                let network = twoparty::VatNetwork::new(reader, writer, rpc_twoparty_capnp::Side::Server, options);
                connections.spawn_local(RpcSystem::new(Box::new(network), Some(endpoint.clone().client)));
            }
        }
    };
    events.close();
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    result
}

enum ClientMessage {
    Call(Vec<u8>, oneshot::Sender<Result<Value>>),
    Describe(oneshot::Sender<Result<Value>>),
    Subscribe(String, oneshot::Sender<Result<Subscription>>),
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
            .map_err(|_| error(ErrorCode::Busy, "RPC connection queue is unavailable"))?;
        response.await.map_err(transport)?
    }

    pub async fn call(&self, request: Envelope) -> Result<Value> {
        request.validate()?;
        let timeout = request.timeout()?;
        let mutation = request.request.metadata().mutation;
        let encoded = serde_json::to_vec(&request).map_err(transport)?;
        if encoded.len() > MAX_MESSAGE {
            return Err(error(ErrorCode::Invalid, "RPC request exceeds 1 MiB"));
        }
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Call(encoded, reply))?;
        let result = match tokio::time::timeout(timeout, response).await {
            Ok(Ok(result)) => result,
            Ok(Err(failure)) => Err(transport(failure)),
            Err(_) => Err(error(
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
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Subscribe(kind.to_owned(), reply))?;
        response.await.map_err(transport)?
    }

    pub async fn close(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.send(ClientMessage::Close(reply))?;
        response.await.map_err(transport)
    }

    fn send(&self, message: ClientMessage) -> Result<()> {
        self.requests
            .try_send(message)
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => {
                    crate::rpc::error(ErrorCode::Busy, "RPC request queue is full")
                }
                mpsc::error::TrySendError::Closed(_) => transport("RPC connection is closed"),
            })
    }
}

fn read_outcome(reader: room_capnp::outcome::Reader<'_>) -> Result<Value> {
    let (failed, bytes) = match reader.which().map_err(transport)? {
        room_capnp::outcome::Value(bytes) => (false, bytes),
        room_capnp::outcome::Error(bytes) => (true, bytes),
    };
    let bytes = bytes.map_err(transport)?;
    if bytes.len() > MAX_MESSAGE {
        return Err(error(ErrorCode::Overflow, "RPC result exceeds 1 MiB"));
    }
    if failed {
        Err(serde_json::from_slice(bytes).map_err(transport)?)
    } else {
        serde_json::from_slice(bytes).map_err(transport)
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
    let options = capnp::message::ReaderOptions {
        traversal_limit_in_words: Some(2 * MAX_MESSAGE / 8),
        nesting_limit: 64,
    };
    let network =
        twoparty::VatNetwork::new(reader, writer, rpc_twoparty_capnp::Side::Client, options);
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
                _ = &mut running => { rpc_finished = true; break; },
                Some(_) = calls.join_next(), if !calls.is_empty() => {},
                message = incoming.recv() => {
                    match message {
                        None => break,
                        Some(ClientMessage::Close(reply)) => { close_reply = Some(reply); break; },
                        Some(ClientMessage::Call(_, reply) | ClientMessage::Describe(reply)) if calls.len() >= 64 => {
                            let _ = reply.send(Err(error(ErrorCode::Busy, "64 RPC requests are already pending")));
                        }
                        Some(ClientMessage::Subscribe(_, reply)) if calls.len() >= 64 => {
                            let _ = reply.send(Err(error(ErrorCode::Busy, "64 RPC requests are already pending")));
                        }
                        Some(ClientMessage::Call(encoded, reply)) => {
                            let mut request = endpoint.call_request();
                            request.get().set_request(&encoded);
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
                            request.get().set_kind(&kind);
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
        incoming.close();
        calls.abort_all();
        while calls.join_next().await.is_some() {}
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
            return Err(error(ErrorCode::Invalid, "max_items must be in 1..=512"));
        }
        let (reply, response) = oneshot::channel();
        self.requests
            .try_send(SubscriptionMessage::Next(max_items, reply))
            .map_err(|_| {
                error(
                    ErrorCode::Busy,
                    "subscription is closed or has a pending read",
                )
            })?;
        response.await.map_err(transport)?
    }

    pub async fn close(&self) -> Result<()> {
        let (reply, response) = oneshot::channel();
        self.requests
            .send(SubscriptionMessage::Close(reply))
            .await
            .map_err(transport)?;
        response.await.map_err(transport)?
    }
}

fn subscription_client(endpoint: subscription::Client) -> Subscription {
    let (requests, mut incoming) = mpsc::channel(1);
    tokio::task::spawn_local(async move {
        let mut reads = tokio::task::JoinSet::new();
        loop {
            let message = tokio::select! {
                Some(_) = reads.join_next(), if !reads.is_empty() => continue,
                message = incoming.recv() => message,
            };
            let Some(message) = message else {
                break;
            };
            match message {
                SubscriptionMessage::Next(_, reply) if !reads.is_empty() => {
                    let _ = reply.send(Err(error(
                        ErrorCode::Busy,
                        "subscription already has a pending read",
                    )));
                }
                SubscriptionMessage::Next(count, reply) => {
                    let mut request = endpoint.next_request();
                    request.get().set_max_items(count);
                    reads.spawn_local(async move {
                        let mut reply = reply;
                        let result = tokio::select! {
                            _ = reply.closed() => return,
                            result = request.send().promise => (|| {
                                let response = result.map_err(transport)?;
                                let batch = response.get().map_err(transport)?.get_batch().map_err(transport)?;
                                let records = batch.get_records().map_err(transport)?.iter()
                                    .map(|data| serde_json::from_slice(data.map_err(transport)?).map_err(transport))
                                    .collect::<Result<Vec<_>>>()?;
                                Ok(Batch { records, dropped: batch.get_dropped(), closed: batch.get_closed() })
                            })(),
                        };
                        let _ = reply.send(result);
                    });
                }
                SubscriptionMessage::Close(reply) => {
                    incoming.close();
                    let result = close_subscription(&endpoint).await;
                    let _ = tokio::time::timeout(std::time::Duration::from_secs(1), async {
                        while reads.join_next().await.is_some() {}
                    })
                    .await;
                    reads.abort_all();
                    while reads.join_next().await.is_some() {}
                    let _ = reply.send(result);
                    return;
                }
            }
        }
        reads.abort_all();
        while reads.join_next().await.is_some() {}
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
    .map_err(|_| error(ErrorCode::Timeout, "subscription close timed out"))?
    .map(|_| ())
    .map_err(transport)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn oversized_errors_obey_the_same_rpc_limit_as_values() {
        let large = error(ErrorCode::PartialFailure, "x".repeat(MAX_MESSAGE));
        let mut encoded = capnp::message::Builder::new_default();
        set_outcome(encoded.init_root(), Err(large.clone())).unwrap();
        let result = read_outcome(encoded.get_root_as_reader().unwrap()).unwrap_err();
        assert_eq!(result.code, ErrorCode::Overflow);

        let mut incoming = capnp::message::Builder::new_default();
        incoming
            .init_root::<room_capnp::outcome::Builder<'_>>()
            .set_error(&serde_json::to_vec(&large).unwrap());
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

        struct SilentSubscription;
        impl subscription::Server for SilentSubscription {
            async fn next(
                self: Rc<Self>,
                _: subscription::NextParams,
                _: subscription::NextResults,
            ) -> capnp::Result<()> {
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
        struct SilentRoom;
        impl room::Server for SilentRoom {
            async fn subscribe(
                self: Rc<Self>,
                _: room::SubscribeParams,
                mut results: room::SubscribeResults,
            ) -> capnp::Result<()> {
                results
                    .get()
                    .set_subscription(capnp_rpc::new_client(SilentSubscription));
                Ok(())
            }
        }
        tokio::task::LocalSet::new()
            .run_until(async {
                let directory = tempfile::tempdir().unwrap();
                let path = directory.path().join("silent-subscription.sock");
                let listener = tokio::net::UnixListener::bind(&path).unwrap();
                let server = tokio::task::spawn_local(async move {
                    let (peer, _) = listener.accept().await.unwrap();
                    let (reader, writer) = peer.compat().split();
                    let network = twoparty::VatNetwork::new(
                        reader,
                        writer,
                        rpc_twoparty_capnp::Side::Server,
                        Default::default(),
                    );
                    let endpoint: room::Client = capnp_rpc::new_client(SilentRoom);
                    RpcSystem::new(Box::new(network), Some(endpoint.client)).await
                });
                let client = Client::connect(&path).await.unwrap();
                let stream = client.subscribe("events").await.unwrap();
                let reader = stream.clone();
                let pending = tokio::spawn(async move { reader.next(1).await });
                tokio::time::sleep(Duration::from_millis(10)).await;
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
