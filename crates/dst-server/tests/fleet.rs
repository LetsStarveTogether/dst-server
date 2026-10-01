//! Exercise all 120 real configurations with mock systemd and real SDK sockets.

use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use dst_server::{
    host::{AGENT_SOCKET, Host},
    host_operations::HostOperation,
    model::{Error, ErrorCode, Request},
    rooms,
    rpc::{self, EventHub, Incoming},
};
use serde::Serialize;
use serde_json::{Value, json};
use tokio::{
    sync::{Notify, mpsc, oneshot},
    task::LocalSet,
    time::{sleep, timeout},
};

#[derive(Clone, Copy, Debug, Serialize)]
struct Resources {
    descriptors: usize,
    threads: usize,
    rss_kib: usize,
}

fn resources() -> Resources {
    let status = fs::read_to_string("/proc/self/status").unwrap();
    Resources {
        descriptors: fs::read_dir("/proc/self/fd").unwrap().count(),
        threads: fs::read_dir("/proc/self/task").unwrap().count(),
        rss_kib: status
            .lines()
            .find_map(|line| line.strip_prefix("VmRSS:"))
            .unwrap()
            .split_whitespace()
            .next()
            .unwrap()
            .parse()
            .unwrap(),
    }
}

#[derive(Default)]
struct Calls {
    active: AtomicUsize,
    peak: AtomicUsize,
    accepted_saves: AtomicUsize,
    completed_saves: AtomicUsize,
    saved_rooms: Mutex<BTreeSet<u16>>,
    release_saves: Notify,
}

async fn actor(number: u16, mut incoming: mpsc::Receiver<Incoming>, calls: Arc<Calls>) {
    while let Some(message) = incoming.recv().await {
        let active = calls.active.fetch_add(1, Ordering::SeqCst) + 1;
        calls.peak.fetch_max(active, Ordering::SeqCst);
        let save = matches!(message.request.request, Request::Save {});
        if save {
            assert!(calls.saved_rooms.lock().unwrap().insert(number));
            calls.accepted_saves.fetch_add(1, Ordering::SeqCst);
            calls.release_saves.notified().await;
            calls.completed_saves.fetch_add(1, Ordering::SeqCst);
        } else {
            // Differing completion times exercise result ordering through the batch.
            sleep(Duration::from_millis(4 + u64::from(number % 4))).await;
        }
        let result = if number == 42 && !save {
            Err(Error::new(ErrorCode::NotReady, "injected room failure"))
        } else {
            Ok(json!({"number": number, "phase": "running", "saved": save}))
        };
        calls.active.fetch_sub(1, Ordering::SeqCst);
        let _ = message.reply.send(result);
    }
}

async fn settled(baseline: Resources, calls: &Calls) -> Resources {
    timeout(Duration::from_secs(3), async {
        loop {
            let current = resources();
            if calls.active.load(Ordering::SeqCst) == 0
                && current.descriptors <= baseline.descriptors + 2
                && current.threads <= baseline.threads + 1
            {
                return current;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap_or_else(|_| {
        panic!(
            "resources did not settle: {baseline:?} -> {:?}",
            resources()
        )
    })
}

fn ordered_outcomes(outcomes: &[Value], numbers: &[u16], systemd_status: bool) {
    assert_eq!(outcomes.len(), 120);
    for (outcome, number) in outcomes.iter().zip(numbers) {
        assert_eq!(outcome["number"], *number);
        let result = &outcome["result"];
        if systemd_status {
            assert_eq!(result["ok"], true);
            assert_eq!(result["value"]["active"], "active");
            if *number == 42 {
                assert_eq!(result["value"]["error"], "injected room failure");
            } else {
                assert_eq!(result["value"]["game"]["number"], *number);
                assert_eq!(result["value"]["error"], Value::Null);
            }
        } else if *number == 42 {
            assert_eq!(result["ok"], false);
            assert_eq!(result["error"]["code"], "not_ready");
        } else {
            assert_eq!(result["ok"], true);
            assert_eq!(result["value"]["number"], *number);
        }
    }
}

#[tokio::test(flavor = "current_thread")]
async fn all_120_rooms_keep_bounded_batches_and_resources_after_cancellation() {
    LocalSet::new()
        .run_until(async {
            let started = Instant::now();
            let temporary = tempfile::tempdir().unwrap();
            let directory = temporary.path();
            let mut host = Host::new(directory.join("rooms"), directory.join("units"));
            let executable = directory.join("systemctl");
            fs::write(&executable, include_str!("fixtures/fleet_systemctl.py")).unwrap();
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
            host.systemd.executable = executable;
            fs::write(directory.join("systemd-counts.json"),
                r#"{"active":0,"peak":0,"started":0,"finished":0}"#).unwrap();

            let numbers = rooms::room_numbers();
            assert_eq!(numbers.len(), 120);
            let definitions: Vec<_> = numbers.iter().map(|number| {
                rooms::fleet_room(*number, "fleet-fixture-token", Some("fleet-fixture-key")).unwrap()
            }).collect();
            let provisioned = host.provision(&definitions).await.unwrap();
            let mut ports = BTreeSet::new();
            let mut families = BTreeSet::new();
            let mut shard_count = 0;
            for (outcome, definition) in provisioned.iter().zip(&definitions) {
                assert_eq!(outcome["number"], definition.number);
                assert_eq!(outcome["result"]["ok"], true, "{outcome}");
                let loaded = host.rooms.load(definition.number).unwrap();
                loaded.validate().unwrap();
                assert_eq!(loaded.template, definition.template);
                assert_eq!(loaded.policy, definition.policy);
                // Native Lua uses the same empty table for [] and {}; compare the
                // actual complete game files so defaults and Mods are checked too.
                let loaded_files = loaded.cluster.files().unwrap();
                let expected_files = definition.cluster.files().unwrap();
                assert_eq!(loaded_files.keys().collect::<Vec<_>>(), expected_files.keys().collect::<Vec<_>>());
                for (path, expected) in expected_files {
                    assert!(loaded_files[&path] == expected, "room {} file {} changed", definition.number, path.display());
                }
                families.insert(loaded.template.unwrap());
                shard_count += loaded.cluster.resolved()["shards"].as_object().unwrap().len();
                for mapping in loaded.deployment.ports {
                    assert!(ports.insert(mapping.host), "duplicate published UDP port");
                }
            }
            assert_eq!(host.rooms.numbers().unwrap(), numbers);
            assert_eq!(families.len(), 12);
            assert_eq!(shard_count, 227);
            assert_eq!(ports.len(), 454);
            assert!(!serde_json::to_string(&provisioned).unwrap().contains("fleet-fixture-token"));
            drop(provisioned);
            drop(definitions);
            let before_sockets = resources();

            let calls = Arc::new(Calls::default());
            let mut servers = Vec::new();
            let mut actors = Vec::new();
            let mut stops = Vec::new();
            let mut sockets = Vec::new();
            for number in &numbers {
                let path = host.rooms.path(*number).unwrap().join(AGENT_SOCKET);
                let socket = path.clone();
                let (dispatch, incoming) = mpsc::channel(8);
                let (shutdown, stop) = oneshot::channel();
                servers.push(tokio::task::spawn_local(async move {
                    rpc::serve(&socket, dispatch, EventHub::default(), stop).await
                }));
                actors.push(tokio::task::spawn_local(actor(*number, incoming, calls.clone())));
                stops.push(shutdown);
                sockets.push(path);
            }
            timeout(Duration::from_secs(3), async {
                while !sockets.iter().all(|path| path.exists()) {
                    tokio::task::yield_now().await;
                }
            }).await.unwrap();
            fs::write(directory.join("services-active"), "").unwrap();
            // Warm the single SDK broker before measuring steady-state resources.
            host.call(numbers[0], Request::Status {}).await.unwrap();
            sleep(Duration::from_millis(20)).await;
            let baseline = resources();
            assert!(baseline.descriptors >= before_sockets.descriptors + 120);
            assert!(baseline.threads <= before_sockets.threads + 1);

            let peak = Arc::new(Mutex::new(baseline));
            let monitor_peak = peak.clone();
            let monitor = tokio::task::spawn_local(async move {
                loop {
                    let current = resources();
                    {
                        let mut peak = monitor_peak.lock().unwrap();
                        peak.descriptors = peak.descriptors.max(current.descriptors);
                        peak.threads = peak.threads.max(current.threads);
                        peak.rss_kib = peak.rss_kib.max(current.rss_kib);
                    }
                    sleep(Duration::from_millis(2)).await;
                }
            });
            let status = HostOperation::Status { game: true };
            let call = HostOperation::Call { request: Request::Status {} };
            let mut checkpoints = Vec::new();
            for round in 0..5 {
                let mut order = numbers.clone();
                order.rotate_left(round * 17);
                let statuses = host.batch(&order, &status).await.unwrap();
                ordered_outcomes(&statuses, &order, true);
                let results = host.batch(&order, &call).await.unwrap();
                ordered_outcomes(&results, &order, false);
                checkpoints.push(settled(baseline, &calls).await);
            }

            let save = HostOperation::Call { request: Request::Save {} };
            let mut cancelled = Box::pin(host.batch(&numbers, &save));
            tokio::select! {
                result = &mut cancelled => panic!("blocked save batch completed: {result:?}"),
                ready = timeout(Duration::from_secs(3), async {
                    while calls.accepted_saves.load(Ordering::SeqCst) != 8 {
                        tokio::task::yield_now().await;
                    }
                }) => ready.unwrap()
            }
            assert_eq!(calls.accepted_saves.load(Ordering::SeqCst), 8);
            drop(cancelled);
            calls.release_saves.notify_waiters();
            checkpoints.push(settled(baseline, &calls).await);
            assert_eq!(calls.completed_saves.load(Ordering::SeqCst), 8);
            ordered_outcomes(&host.batch(&numbers, &call).await.unwrap(), &numbers, false);
            checkpoints.push(settled(baseline, &calls).await);
            assert_eq!(calls.accepted_saves.load(Ordering::SeqCst), 8);
            assert_eq!(calls.saved_rooms.lock().unwrap().len(), 8);
            assert!((2..=8).contains(&calls.peak.load(Ordering::SeqCst)));
            let minimum_fds = checkpoints.iter().map(|sample| sample.descriptors).min().unwrap();
            let maximum_fds = checkpoints.iter().map(|sample| sample.descriptors).max().unwrap();
            assert!(maximum_fds - minimum_fds <= 2, "{checkpoints:?}");

            monitor.abort();
            let _ = monitor.await;
            let peak = *peak.lock().unwrap();
            assert!(peak.descriptors <= baseline.descriptors + 64, "{baseline:?} -> {peak:?}");
            assert!(peak.threads <= baseline.threads + 1, "{baseline:?} -> {peak:?}");
            for stop in stops { stop.send(()).unwrap(); }
            for server in servers { server.await.unwrap().unwrap(); }
            for actor in actors { actor.await.unwrap(); }
            assert!(sockets.iter().all(|path| !path.exists()));
            let final_resources = settled(
                Resources {
                    descriptors: baseline.descriptors - 120,
                    ..baseline
                },
                &calls,
            )
            .await;
            let systemd: Value = serde_json::from_slice(&fs::read(directory.join("systemd-counts.json")).unwrap()).unwrap();
            assert_eq!(systemd["active"], 0);
            assert_eq!(systemd["started"], systemd["finished"]);
            assert!((2..=8).contains(&systemd["peak"].as_u64().unwrap()));
            assert_eq!(systemd["started"], 840);
            let report = json!({
                "rooms": 120, "shards": shard_count, "families": families,
                "published_udp_ports": ports.len(), "mock_systemd": systemd,
                "rounds": 5, "ordered_batch_results": 1320, "injected_rpc_failures": 11,
                "accepted_cancelled_saves": 8, "completed_cancelled_saves": 8,
                "peak_rpc_in_flight": calls.peak.load(Ordering::SeqCst),
                "before_sockets": before_sockets, "baseline": baseline, "peak": peak,
                "checkpoints": checkpoints, "after_socket_shutdown": final_resources,
                "elapsed_ms": started.elapsed().as_millis(),
                "scope": "120 full configurations, mock systemd, real SDK sockets; no game processes"
            });
            if let Some(path) = std::env::var_os("DST_FLEET_REPORT") {
                fs::write(Path::new(&path), serde_json::to_vec_pretty(&report).unwrap()).unwrap();
            }
            println!("FLEET_REPORT {report}");
        })
        .await;
}
