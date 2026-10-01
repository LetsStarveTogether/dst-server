use std::{collections::BTreeSet, os::fd::AsRawFd, sync::Arc, time::Duration};

use dst_server::{
    model::{Envelope, ErrorCode, Request, Target},
    rpc::{self, Client, EventHub, Incoming},
};
use serde_json::json;
use tokio::sync::{Notify, mpsc, oneshot};

static TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[tokio::test]
async fn native_socket_calls_survive_client_cancellation_and_streams_report_loss() {
    let _test = TEST_LOCK.lock().await;
    tokio::task::LocalSet::new()
        .run_until(async {
            let directory = tempfile::tempdir().unwrap();
            let path = directory.path().join("agent.sock");
            let hub = EventHub::default();
            let (dispatch, mut incoming) = mpsc::channel::<Incoming>(64);
            let (shutdown, stop) = oneshot::channel();
            let socket = path.clone();
            let events = hub.clone();
            let server = tokio::task::spawn_local(async move {
                rpc::serve(&socket, dispatch, events, stop).await
            });
            let finished = Arc::new(Notify::new());
            let accepted = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let actor_finished = finished.clone();
            let actor_accepted = accepted.clone();
            let actor_release = release.clone();
            let actor = tokio::spawn(async move {
                while let Some(message) = incoming.recv().await {
                    let finished = actor_finished.clone();
                    let accepted = actor_accepted.clone();
                    let release = actor_release.clone();
                    tokio::spawn(async move {
                        if matches!(message.request.request, Request::Save {}) {
                            accepted.notify_one();
                            release.notified().await;
                            finished.notify_one();
                        }
                        let _ = message.reply.send(Ok(json!({"completed":true})));
                    });
                }
            });
            tokio::time::timeout(Duration::from_secs(1), async {
                while !path.exists() {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
            })
            .await
            .unwrap();
            let client = Client::connect(&path).await.unwrap();
            assert_eq!(client.describe().await.unwrap()["protocol"], 1);
            let clone = client.clone();
            let call = tokio::spawn(async move {
                clone
                    .call(Envelope::new(Target::Room, Request::Save {}).unwrap())
                    .await
            });
            tokio::time::timeout(Duration::from_secs(2), accepted.notified())
                .await
                .unwrap();
            assert!(!call.is_finished());
            call.abort();
            client.close().await.unwrap();
            release.notify_one();
            tokio::time::timeout(Duration::from_secs(1), finished.notified())
                .await
                .unwrap();

            let client = Client::connect(&path).await.unwrap();
            let stream = client.subscribe("events").await.unwrap();
            for sequence in 0..1100 {
                hub.publish("events", &json!({"sequence":sequence}));
            }
            let batch = stream.next(512).await.unwrap();
            assert_eq!(batch.records.len(), 512);
            assert_eq!(batch.dropped, 76);
            assert_eq!(batch.records[0]["sequence"], 0);
            assert!(!batch.closed);
            stream.close().await.unwrap();

            let stream = client.subscribe("logs").await.unwrap();
            let record = json!({"line":"x".repeat(60 * 1024)});
            for _ in 0..200 {
                hub.publish("logs", &record);
            }
            let batch = stream.next(512).await.unwrap();
            assert!(batch.dropped > 0 && batch.dropped < 200);
            assert!(batch.records.len() < 512); // Batches also obey a byte budget.
            stream.close().await.unwrap();

            let pending = client.subscribe("lifecycle").await.unwrap();
            assert!(
                tokio::time::timeout(Duration::from_millis(10), pending.next(1))
                    .await
                    .is_err()
            );
            tokio::time::timeout(Duration::from_secs(1), pending.close())
                .await
                .unwrap()
                .unwrap();
            let active = client.subscribe("events").await.unwrap();
            let reader = active.clone();
            let waiting = tokio::spawn(async move { reader.next(1).await });
            tokio::time::sleep(Duration::from_millis(10)).await;
            active.close().await.unwrap();
            assert!(
                tokio::time::timeout(Duration::from_secs(1), waiting)
                    .await
                    .unwrap()
                    .unwrap()
                    .unwrap()
                    .closed
            );
            client.close().await.unwrap();
            shutdown.send(()).unwrap();
            server.await.unwrap().unwrap();
            actor.await.unwrap();
            assert!(!path.exists());
        })
        .await;
}

#[tokio::test]
async fn cancelled_waits_release_slots_and_disconnected_mutations_stay_unknown() {
    let _test = TEST_LOCK.lock().await;
    tokio::task::LocalSet::new()
        .run_until(async {
            let directory = tempfile::tempdir().unwrap();
            let path = directory.path().join("agent.sock");
            let (dispatch, mut accepted) = mpsc::channel::<Incoming>(64);
            let (shutdown, stop) = oneshot::channel();
            let socket = path.clone();
            let server = tokio::task::spawn_local(async move {
                rpc::serve(&socket, dispatch, EventHub::default(), stop).await
            });
            tokio::time::timeout(Duration::from_secs(2), async {
                while !path.exists() {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            let client = Client::connect(&path).await.unwrap();
            let mut owned_operations = Vec::new();
            // A stalled Agent retains every accepted operation; canceled SDK waits
            // must still release their own per-connection request slots.
            for _ in 0..80 {
                let clone = client.clone();
                let waiting = tokio::spawn(async move {
                    clone
                        .call(Envelope::new(Target::Room, Request::Save {}).unwrap())
                        .await
                });
                owned_operations.push(
                    tokio::time::timeout(Duration::from_secs(2), accepted.recv())
                        .await
                        .unwrap()
                        .unwrap(),
                );
                waiting.abort();
                let _ = waiting.await;
                assert_eq!(client.describe().await.unwrap()["protocol"], 1);
            }
            assert_eq!(owned_operations.len(), 80);
            let clone = client.clone();
            let waiting = tokio::spawn(async move {
                clone
                    .call(Envelope::new(Target::Room, Request::Save {}).unwrap())
                    .await
            });
            owned_operations.push(
                tokio::time::timeout(Duration::from_secs(2), accepted.recv())
                    .await
                    .unwrap()
                    .unwrap(),
            );
            shutdown.send(()).unwrap();
            server.await.unwrap().unwrap();
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), waiting)
                    .await
                    .unwrap()
                    .unwrap()
                    .unwrap_err()
                    .code,
                ErrorCode::Unknown
            );
            // The dispatcher still owns all mutations after their clients disappear.
            assert_eq!(owned_operations.len(), 81);
        })
        .await;
}

fn socket_fds() -> BTreeSet<i32> {
    std::fs::read_dir("/proc/self/fd")
        .unwrap()
        .filter_map(|entry| {
            let entry = entry.ok()?;
            let target = std::fs::read_link(entry.path()).ok()?;
            target
                .to_string_lossy()
                .starts_with("socket:")
                .then(|| entry.file_name().to_string_lossy().parse().unwrap())
        })
        .collect()
}

#[tokio::test]
async fn close_releases_its_socket_before_acknowledging_a_silent_peer() {
    let _test = TEST_LOCK.lock().await;
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("silent.sock");
    let listener = tokio::net::UnixListener::bind(&path).unwrap();
    // Initialize the shared client broker before measuring per-client resources.
    let (warm, peer) = tokio::join!(Client::connect(&path), listener.accept());
    warm.unwrap().close().await.unwrap();
    drop(peer.unwrap());
    let before = socket_fds();
    let (client, peer) = tokio::join!(Client::connect(&path), listener.accept());
    let client = client.unwrap();
    let (peer, _) = peer.unwrap();
    let opened: Vec<_> = socket_fds()
        .difference(&before)
        .copied()
        .filter(|fd| *fd != peer.as_raw_fd())
        .collect();
    assert_eq!(opened.len(), 1);
    let clone = client.clone();
    let waiting = tokio::spawn(async move { clone.describe().await });
    tokio::task::yield_now().await;
    tokio::time::timeout(Duration::from_secs(3), client.close())
        .await
        .unwrap()
        .unwrap();
    // The peer never processed Cap'n Proto, and cleanup still closed our FD.
    assert!(!socket_fds().contains(&opened[0]));
    assert!(waiting.await.unwrap().is_err());
}
