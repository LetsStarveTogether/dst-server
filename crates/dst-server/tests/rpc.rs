use std::{collections::BTreeSet, os::fd::AsRawFd, sync::Arc, time::Duration};

use dst_server::{
    model::{Envelope, Error, ErrorCode, Request, Scope, Target},
    rpc::{self, Client, EventHub, Incoming, StreamKind},
};
use serde_json::json;
use tokio::sync::{Notify, mpsc, oneshot};

static TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[path = "fixtures/large_events.rs"]
mod large_events;

#[tokio::test]
async fn legal_maximum_game_frames_survive_subscription_encoding() {
    let _test = TEST_LOCK.lock().await;
    tokio::task::LocalSet::new()
        .run_until(async {
            let directory = tempfile::tempdir().unwrap();
            let socket = directory.path().join("large.sock");
            let (dispatch, _incoming) = mpsc::channel(1);
            let (shutdown, stop) = oneshot::channel();
            let hub = EventHub::default();
            let path = socket.clone();
            let events = hub.clone();
            let server =
                tokio::task::spawn_local(
                    async move { rpc::serve(&path, dispatch, events, stop).await },
                );
            tokio::time::timeout(Duration::from_secs(2), async {
                while !socket.exists() {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            let client = Client::connect(&socket).await.unwrap();
            let stream = client.subscribe(StreamKind::Events).await.unwrap();
            let (resource, cluster, shard) = large_events::dense_context().await;
            let mut largest = 0;
            for event in large_events::records() {
                let mut expected = large_events::local_record(&event);
                expected["resource"] = json!(resource);
                expected["attributes"]["dst.cluster.name"] = json!(cluster);
                expected["attributes"]["dst.shard.name"] = json!(shard);
                hub.publish(StreamKind::Events, &expected);
                largest = largest.max(hub.stats()["events"].pending_bytes);
                let batch = stream.next(512).await.unwrap();
                assert_eq!(batch.dropped, 0, "{}", event.event_name);
                assert!(
                    batch.records == vec![expected],
                    "{} changed",
                    event.event_name
                );
            }
            eprintln!(
                "schema_capacity resource_attributes={} maximum_encoded_bytes={largest}",
                resource.len()
            );
            assert!(largest < 1024 * 1024 - 32);
            let records = vec![
                json!({"sequence":0,"value":"x".repeat(700_000)}),
                json!({"sequence":1,"value":null}),
                json!({"sequence":2,"value":vec![0; 32_000]}),
                json!({"sequence":3,"value":"中文🌲"}),
            ];
            for record in &records {
                hub.publish(StreamKind::Events, record);
            }
            let first = stream.next(512).await.unwrap();
            assert_eq!((first.records.len(), first.dropped), (2, 0));
            assert!(first.records == records[..2]);
            assert_eq!(hub.stats()["events"].pending_records, 2);
            let second = stream.next(512).await.unwrap();
            assert!(second.records == records[2..]);
            assert_eq!(second.dropped, 0);
            assert_eq!(hub.stats()["events"].pending_bytes, 0);

            for record in &records {
                hub.publish(StreamKind::Events, record);
            }
            stream.next(512).await.unwrap();
            assert_eq!(hub.stats()["events"].pending_records, 2);
            stream.close().await.unwrap();
            assert_eq!(hub.stats()["events"].pending_bytes, 0);
            client.close().await.unwrap();
            shutdown.send(()).unwrap();
            server.await.unwrap().unwrap();
        })
        .await;
}

#[tokio::test]
async fn native_commands_results_and_events_preserve_fields() {
    let _test = TEST_LOCK.lock().await;
    tokio::task::LocalSet::new().run_until(async {
        let directory = tempfile::tempdir().unwrap();
        let socket = directory.path().join("native.sock");
        let (dispatch, mut incoming) = mpsc::channel::<Incoming>(64);
        let (shutdown, stop) = oneshot::channel();
        let events = EventHub::default();
        let hub = events.clone();
        let path = socket.clone();
        let server = tokio::task::spawn_local(async move {
            rpc::serve(&path, dispatch, events, stop).await
        });
        let failure = Error::new(ErrorCode::PartialFailure, "洞穴 unavailable").with_details(json!({
            "exact": u64::MAX,
            "nested": [null, {}, []],
            "shard_results": "x".repeat(5 * 1024 * 1024),
        }));
        let failures = [
            failure,
            Error::new(ErrorCode::Transport, "native preloader did not exit cleanly")
                .with_details(json!({"returncode":7})),
        ];
        let mut replies = failures.clone().into_iter();
        let actor = tokio::spawn(async move {
            while let Some(call) = incoming.recv().await {
                let result = if matches!(call.request.request, Request::Kill {}) {
                    Err(replies.next().unwrap())
                } else {
                    Ok(serde_json::to_value(call.request).unwrap())
                };
                call.reply.send(result).unwrap();
            }
        });
        tokio::time::timeout(Duration::from_secs(2), async {
            while !socket.exists() {
                tokio::task::yield_now().await;
            }
        }).await.unwrap();
        let client = Client::connect(&socket).await.unwrap();
        let dynamic = json!({
            "numbers": [i64::MIN, u64::MAX, 9_007_199_254_740_993_u64, 0.25, -0.0],
            "large": "123456789012345678901234567890".parse::<serde_json::Number>().unwrap(),
            "decimal": "0.12345678901234567890123456789".parse::<serde_json::Number>().unwrap(),
            "unknown_season": "mod_season", "empty": [null, false, 0, "", [], {}],
        });
        let cases = [
            ("status", json!({})), ("start", json!({})),
            ("stop", json!({"notice": null})),
            ("restart", json!({"notice": {"template":"{reason}: {remaining}","delay":0,"interval":0.5,"parameters":{"reason":"维护"}}})),
            ("update_mods", json!({"restart":true,"notice":null})),
            ("read_configuration", json!({})),
            ("configure", json!({"configuration":dynamic,"replace_permissions":true})),
            ("edit_configuration", json!({"changes":[["/cluster/settings/cluster_name","新名字"],["/cluster/settings/max_players",12]],"unset":["/policy/idle"]})),
            ("set_policy", json!({"policy":{"mod_auto_update":false}})),
            ("execute", json!({"source":"print('test')"})),
            ("execute_json", json!({"source":"return {test=1}"})),
            ("evaluate", json!({"source":"TheWorld.state.cycles"})),
            ("execute_all", json!({"source":"print('all')"})),
            ("announce", json!({"message":"公告","count":3,"interval":1.25})),
            ("save", json!({})), ("pause", json!({"paused":false})),
            ("reset", json!({})), ("rollback", json!({"count":0})),
            ("regenerate", json!({"expected_session_id":"SESSION","require_empty":false})),
            ("regenerate_shard", json!({"preserve_settings":false})),
            ("list_snapshots", json!({"limit":3,"before":0})),
            ("rollback_to_day", json!({"day":2})),
            ("rollback_to_snapshot", json!({"session_id":"SESSION","snapshot_id":7})),
            ("health", json!({})), ("presence", json!({})), ("room", json!({})),
            ("world", json!({})), ("runtime", json!({})), ("mods", json!({})),
            ("connected_shards", json!({})), ("list_players", json!({})),
            ("get_player", json!({"userid":"KU_1"})),
            ("inventory", json!({"userid":"KU_2"})), ("kick", json!({"userid":"KU_3"})),
            ("ban", json!({"userid":"KU_4","seconds":100})),
            ("blocklist", json!({})), ("is_blocked", json!({"userid":"KU_5"})),
            ("unban", json!({"userid":"KU_6"})), ("is_admin", json!({"userid":"KU_7"})),
            ("read_permissions", json!({})),
            ("set_admin", json!({"userid":"KU_8","enabled":false})),
            ("set_vitals", json!({"userid":"KU_9","health":0,"hunger":0.25,"sanity":0.5,"temperature":-15.25,"moisture":1})),
            ("kill_player", json!({"userid":"KU_10"})),
            ("revive", json!({"userid":"KU_11"})), ("despawn", json!({"userid":"KU_12"})),
            ("migrate", json!({"userid":"KU_13","shard_id":"Caves","portal_id":2})),
            ("teleport", json!({"userid":"KU_14","x":1.25,"y":-2.5,"z":-0.0})),
            ("give", json!({"userid":"KU_15","item":"twigs","count":3})),
            ("remove", json!({"userid":"KU_16","item":"rocks","count":7})),
            ("is_whitelisted", json!({"userid":"KU_17"})),
            ("whitelist", json!({"userid":"KU_18"})),
            ("unwhitelist", json!({"userid":"KU_19"})),
            ("list_vote_commands", json!({})), ("get_vote", json!({})), ("cancel_vote", json!({})),
            ("start_vote", json!({"command":"kick","starter_userid":"KU_A","target_userid":"KU_B"})),
            ("start_vote", json!({"command":"rollback","starter_userid":"KU_A"})),
            // Exercise absent pointers separately from explicit false and zero.
            ("regenerate", json!({})), ("list_snapshots", json!({})),
            ("ban", json!({"userid":"KU_20"})),
            ("set_vitals", json!({"userid":"KU_21","health":0})),
            ("stop", json!({})),
        ];
        let mut methods = BTreeSet::from(["kill"]);
        for (method, arguments) in cases {
            let request: Request = serde_json::from_value(json!({"method":method,"arguments":arguments})).unwrap();
            for scope in request.metadata().scopes {
                let target = match scope {
                    Scope::Room => Target::Room,
                    Scope::Shard => Target::Shard("洞穴".into()),
                };
                let mut envelope = Envelope::new(target, request.clone()).unwrap();
                envelope.timeout = Some(3.25);
                let expected = serde_json::to_value(&envelope).unwrap();
                assert_eq!(client.call(envelope).await.unwrap(), expected, "{method}");
            }
            methods.insert(method);
        }
        assert_eq!(methods, dst_server::model::METHODS.iter().map(|method| method.name).collect());
        for failure in failures {
            assert_eq!(client.call(Envelope::new(Target::Room, Request::Kill {}).unwrap()).await.unwrap_err(), failure);
        }
        let stream = client.subscribe(StreamKind::Events).await.unwrap();
        hub.publish(StreamKind::Events, &dynamic);
        assert_eq!(stream.next(1).await.unwrap().records, vec![dynamic]);
        let mut subscribers = Vec::new();
        for _ in 1..128 {
            subscribers.push(client.subscribe(StreamKind::Logs).await.unwrap());
        }
        assert_eq!(client.subscribe(StreamKind::Lifecycle).await.err().unwrap().code, ErrorCode::Busy);
        stream.close().await.unwrap();
        client.subscribe(StreamKind::Lifecycle).await.unwrap().close().await.unwrap();
        drop(subscribers);
        client.close().await.unwrap();
        shutdown.send(()).unwrap();
        server.await.unwrap().unwrap();
        actor.await.unwrap();
    }).await;
}

#[tokio::test]
async fn final_response_is_delivered_before_server_shutdown() {
    let _test = TEST_LOCK.lock().await;
    tokio::task::LocalSet::new()
        .run_until(async {
            for size in [0, 768 * 1024] {
                let directory = tempfile::tempdir().unwrap();
                let socket = directory.path().join("agent.sock");
                let (dispatch, mut incoming) = mpsc::channel::<Incoming>(1);
                let (shutdown, stop) = oneshot::channel();
                let path = socket.clone();
                let server = tokio::task::spawn_local(async move {
                    rpc::serve(&path, dispatch, EventHub::default(), stop).await
                });
                tokio::time::timeout(Duration::from_secs(2), async {
                    while !socket.exists() {
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                let client = Client::connect(&socket).await.unwrap();
                let stream = client.subscribe(StreamKind::Lifecycle).await.unwrap();
                let clone = client.clone();
                let request = tokio::spawn(async move {
                    clone
                        .call(Envelope::new(Target::Room, Request::Stop { notice: None }).unwrap())
                        .await
                });
                let accepted = incoming.recv().await.unwrap();
                let expected = json!({"stopped":true,"details":"x".repeat(size)});
                accepted.reply.send(Ok(expected.clone())).unwrap();
                shutdown.send(()).unwrap();
                server.await.unwrap().unwrap();
                assert!(!socket.exists());
                assert_eq!(
                    tokio::time::timeout(Duration::from_secs(2), request)
                        .await
                        .unwrap()
                        .unwrap()
                        .unwrap(),
                    expected
                );
                stream.close().await.unwrap();
                stream.close().await.unwrap();
                client.close().await.unwrap();
                client.close().await.unwrap();
            }
        })
        .await;
}

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
            let methods = client.describe().await.unwrap();
            for (scope, name) in [("room", "save"), ("shard", "runtime")] {
                let available = methods[scope].as_array().unwrap();
                assert!(available.iter().any(|method| method["name"] == name));
            }
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
            let stream = client.subscribe(StreamKind::Events).await.unwrap();
            let fast = client.subscribe(StreamKind::Events).await.unwrap();
            for sequence in 0..1100 {
                hub.publish(StreamKind::Events, &json!({"sequence":sequence}));
                let batch = fast.next(1).await.unwrap();
                assert_eq!(batch.records, vec![json!({"sequence": sequence})]);
                assert_eq!(batch.dropped, 0);
            }
            fast.close().await.unwrap();
            let batch = stream.next(512).await.unwrap();
            assert_eq!(batch.records.len(), 128);
            assert_eq!(batch.dropped, 1100 - 128);
            assert_eq!(batch.records[0]["sequence"], 1100 - 128);
            assert!(!batch.closed);
            stream.close().await.unwrap();

            let stream = client.subscribe(StreamKind::Events).await.unwrap();
            for sequence in 0..1024 {
                hub.publish(StreamKind::Events, &json!({"sequence": sequence}));
                let batch = stream.next(1).await.unwrap();
                assert_eq!(batch.records, vec![json!({"sequence": sequence})]);
            }
            let (closed, next) = tokio::join!(stream.close(), stream.next(1));
            closed.unwrap();
            assert_eq!(next.unwrap_err().code, ErrorCode::Transport);
            assert_eq!(stream.next(1).await.unwrap_err().code, ErrorCode::Transport);

            let stream = client.subscribe(StreamKind::Logs).await.unwrap();
            for sequence in 0..200 {
                hub.publish(
                    StreamKind::Logs,
                    &json!({"sequence":sequence,"line":"x".repeat(64 * 1024 - 128)}),
                );
            }
            let mut batch = stream.next(512).await.unwrap();
            assert!(batch.dropped > 0 && batch.dropped < 200);
            hub.publish(StreamKind::Logs, &json!({"sequence":200,"sentinel":true}));
            let mut received = Vec::new();
            let mut dropped = 0;
            loop {
                dropped += batch.dropped;
                received.extend(
                    batch
                        .records
                        .iter()
                        .map(|record| record["sequence"].as_u64().unwrap()),
                );
                if received.last() == Some(&200) {
                    break;
                }
                batch = tokio::time::timeout(Duration::from_secs(1), stream.next(512))
                    .await
                    .unwrap()
                    .unwrap();
            }
            assert!(received.windows(2).all(|pair| pair[0] < pair[1]));
            assert_eq!(received.len() as u64 + dropped, 201);
            stream.close().await.unwrap();

            let oversized = client.subscribe(StreamKind::Events).await.unwrap();
            let mut waiting = Box::pin(oversized.next(1));
            assert!(futures::poll!(&mut waiting).is_pending());
            hub.publish(StreamKind::Events, &json!("x".repeat(1024 * 1024)));
            let batch = tokio::time::timeout(Duration::from_secs(1), waiting)
                .await
                .unwrap()
                .unwrap();
            assert!(batch.records.is_empty());
            assert_eq!(batch.dropped, 1);
            assert!(!batch.closed);
            oversized.close().await.unwrap();

            let pending = client.subscribe(StreamKind::Lifecycle).await.unwrap();
            let mut reading = Box::pin(pending.next(1));
            assert!(futures::poll!(&mut reading).is_pending());
            drop(reading);
            tokio::time::timeout(Duration::from_secs(1), pending.close())
                .await
                .unwrap()
                .unwrap();
            let active = client.subscribe(StreamKind::Events).await.unwrap();
            let mut waiting = Box::pin(active.next(1));
            assert!(futures::poll!(&mut waiting).is_pending());
            active.close().await.unwrap();
            assert!(
                tokio::time::timeout(Duration::from_secs(1), waiting)
                    .await
                    .unwrap()
                    .unwrap()
                    .closed
            );
            let draining = client.subscribe(StreamKind::Lifecycle).await.unwrap();
            for sequence in 0..20 {
                hub.publish(StreamKind::Lifecycle, &json!({"sequence": sequence}));
            }
            hub.close();
            let first = draining.next(15).await.unwrap();
            assert_eq!(first.records.len(), 15);
            assert!(!first.closed);
            let last = draining.next(5).await.unwrap();
            assert_eq!(last.records.len(), 5);
            assert!(last.closed);
            assert_eq!(first.dropped + last.dropped, 0);
            let sequences: Vec<_> = first
                .records
                .into_iter()
                .chain(last.records)
                .map(|record| record["sequence"].as_u64().unwrap())
                .collect();
            assert_eq!(sequences, (0..20).collect::<Vec<_>>());
            assert!(client.subscribe(StreamKind::Lifecycle).await.is_err());
            draining.close().await.unwrap();
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
                client.describe().await.unwrap();
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
