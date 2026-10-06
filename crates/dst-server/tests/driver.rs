//! Protocol fixtures exercise the real Rust process and persistent driver together.
use std::{path::PathBuf, time::Duration};

use dst_server::{
    driver::{
        ControlFailureReason, Driver, DriverErrorCode, DriverEventKind, DriverEventSender,
        DriverEvents, DriverState,
    },
    process::ProcessSpec,
    telemetry::LogEvent,
};
use serde_json::json;
use tokio::{
    io::AsyncWriteExt,
    net::UnixListener,
    time::{sleep, timeout},
};

const NONCE: &str = "01J00000000000000000000000";
const FIXTURE: &str = r#"
import json, os, resource, signal, socket, sys, time
nonce = sys.argv[1]
mode = sys.argv[2]
if mode == 'slow_shutdown':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
generation = 1
count = 0
busy = False
def emit(fd, prefix, record):
    os.write(fd, prefix + json.dumps(record, separators=(',', ':')).encode() + b'\n')
def bootstrap():
    emit(4, b'DST_DRIVER|', {'nonce': nonce, 'generation': generation})
    emit(4, b'DST_DRIVER|', {'nonce': nonce, 'health': {'generation': generation,
        'profile': 'play', 'control_ready': True,
        'telemetry_status': 'failed', 'capabilities': {'players': 'failed'}}})
def ready():
    os.write(5, f'DST_SessionId|S{generation}\nDST_Master_Ready\n'.encode())
def control(shutdown=False):
    emit(4, b'DST_CONTROL|', {'nonce': nonce, 'generation': generation,
        'event': 'world_load_decoded', 'session_id': f'S{generation}', 'shutdown': shutdown})
def checkpoint():
    with socket.socket(socket.AF_UNIX) as peer:
        peer.connect('control.sock')
        assert peer.recv(1) == b'G'
if mode == 'delay_ready':
    bootstrap()
    checkpoint()
    ready()
else:
    ready()
    bootstrap()
if mode == 'defer_read':
    checkpoint()
while True:
    encoded = os.read(3, 4096)
    if not encoded:
        break
    if encoded == b'c_shutdown()\n':
        control(True)
        os.write(5, b'DST_Stopping\nDST_Shutdown\n')
        if mode == 'slow_shutdown':
            time.sleep(60)
        time.sleep(0.15)
        if mode == 'abort_shutdown':
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.abort()
        if mode == 'error_shutdown':
            os._exit(7)
        break
    request = json.loads(encoded.removeprefix(b'DST_RPC|'))
    assert request.keys() == {'nonce', 'id', 'generation', 'method', 'arguments'}
    method = request['method']
    header = {key: request[key] for key in ('nonce', 'id', 'generation')}
    if method == 'no_context':
        continue
    if method == 'busy' and not busy:
        busy = True
        os.write(4, b'DST_LuaBusy\n')
        continue
    emit(4, b'DST_RPC|', {**header, 'accepted': True})
    if method == 'accepted_exit':
        os._exit(0)
    if method == 'bad_nonce':
        header['nonce'] = '01J00000000000000000000001'
    if method == 'bad_id':
        del header['id']
    if method == 'bad_control':
        emit(4, b'DST_CONTROL|', {'nonce': nonce, 'generation': generation})
    if method == 'bad_readiness':
        emit(4, b'DST_DRIVER|', {'nonce': nonce, 'health': {'generation': generation, 'control_ready': 'yes'}})
    if method == 'bad_bootstrap':
        emit(4, b'DST_DRIVER|', {'nonce': nonce, 'generation': generation, 'error': 'private-marker'})
    if method == 'slow':
        count += 1
        checkpoint()
    if method == 'block':
        time.sleep(60)
    if method == 'reload':
        generation += 1
        bootstrap()
        ready()
        time.sleep(0.1)
    if method == 'flood':
        for _ in range(5000):
            os.write(1, b'x' * 1023 + b'\n')
        control()
    if method == 'telemetry':
        os.write(4, b'DST_OTEL|{invalid\n')
        for sequence in (1, 3):
            emit(4, b'DST_OTEL|', {'nonce': nonce, 'generation': generation,
                'session_id': f'S{generation}', 'seq': sequence, 'event': 'dst.world.state_changed',
                'tick': sequence, 'monotonic_ms': sequence, 'cycle': None, 'data': {'name': 'cycles', 'value': sequence}})
    if method == 'telemetry_oversized':
        os.write(4, b'DST_OTEL|' + b'x' * 65527 + b'\n')
    if method == 'native_diagnostics':
        os.write(1, b'CURL ERROR: (login.kleientertainment.com) [6]Could not resolve host: login.kleientertainment.com\n[0] Account Failed (6): ""\nLUA ERROR stack traceback:\n')
    data = {'session_id': f'S{generation}', 'snapshot': 6, 'shard_id': '1', 'is_master_shard': True} if method == 'runtime' else count
    if method == 'presence':
        data = {'observation_epoch': request['arguments'].get('epoch', 0), 'session_id': f'S{generation}'}
    emit(4, b'DST_RPC|', {**header, 'result': {'ok': True, 'data': data}})
    if method.startswith('bad_'):
        time.sleep(60)
    os.write(4, b'DST_RemoteCommandDone\n')
"#;

fn fixture(mode: &str) -> Driver {
    fixture_in(mode, "World", std::env::temp_dir(), None)
}

fn observed(mode: &str, shard: &str) -> (Driver, DriverEvents) {
    let (sender, events) = DriverEvents::channel();
    let driver = fixture_in(mode, shard, std::env::temp_dir(), Some(sender));
    (driver, events)
}

fn fixture_in(
    mode: &str,
    shard: &str,
    directory: PathBuf,
    events: Option<DriverEventSender>,
) -> Driver {
    Driver::attach(
        ProcessSpec {
            program: "python3".into(),
            args: vec![
                "-u".into(),
                "-c".into(),
                FIXTURE.into(),
                NONCE.into(),
                mode.into(),
            ],
            cwd: directory,
        },
        NONCE.into(),
        shard.into(),
        events,
    )
    .unwrap()
}

async fn until(driver: &Driver, condition: impl Fn(&DriverState) -> bool) -> DriverState {
    let mut state = driver.watch();
    timeout(Duration::from_secs(5), async {
        loop {
            let snapshot = state.borrow_and_update().clone();
            if condition(&snapshot) {
                return snapshot;
            }
            state.changed().await.unwrap();
        }
    })
    .await
    .expect("driver state condition")
}

#[tokio::test]
async fn pre_world_login_failure_retains_native_cause_and_allows_current_save_retry() {
    let (sender, mut events) = DriverEvents::channel();
    let driver = Driver::attach(
        ProcessSpec {
            program: "python3".into(),
            args: vec![
                "-u".into(),
                "-c".into(),
                r#"import os
os.write(1, b'[0:00:41]: CURL ERROR: (login.kleientertainment.com) [6]Could not resolve host: login.kleientertainment.com\n[0:00:41]: [0] Account Failed (6): ""\n')
os._exit(7)
"#.into(),
            ],
            cwd: std::env::temp_dir(),
        },
        NONCE.into(),
        "Master".into(),
        Some(sender),
    )
    .unwrap();
    let report = timeout(Duration::from_secs(3), driver.wait())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(report.returncode(), Some(7));
    assert!(report.output_drained && !report.forced);
    let state = driver.snapshot();
    assert!(state.generation.is_none() && !state.native_ready && state.runtime.is_none());
    assert!(state.login_retry_permitted());
    assert_eq!(state.exit.as_ref(), Some(&(&report).into()));
    let state = serde_json::to_value(state).unwrap();
    assert_eq!(state["native_diagnostics"]["login_transport"]["count"], 1);
    let mut exits = Vec::new();
    while let Some(event) = events.recv().await {
        if let DriverEventKind::Lifecycle(data) = event.kind
            && data["event"] == "process_exited"
        {
            exits.push(data);
        }
    }
    assert_eq!(exits.len(), 1);
    assert_eq!(exits[0]["exit"], state["exit"]);
    assert!(exits[0]["control_error"].is_null());
    assert!(!driver.kill().await.unwrap().forced);
}

#[tokio::test]
async fn native_diagnostics_do_not_stop_a_healthy_world_or_survive_as_login_permission() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    for generation in [1, 2] {
        driver
            .request("native_diagnostics", json!({}), Duration::from_secs(1))
            .await
            .unwrap();
        timeout(Duration::from_secs(2), async {
            while driver
                .snapshot()
                .native_diagnostics
                .login_transport
                .as_ref()
                .is_none_or(|entry| entry.count < generation)
            {
                sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let state = driver.snapshot();
        assert!(state.running && state.ready && state.failure.is_none());
        assert!(!state.login_retry_permitted());
        assert_eq!(
            state.native_diagnostics.lua_error.as_ref().unwrap().count,
            generation
        );
        if generation == 1 {
            let _ = driver
                .request("reload", json!({}), Duration::from_secs(2))
                .await;
            until(&driver, |state| state.ready && state.generation == Some(2)).await;
        }
    }
    assert_eq!(
        driver
            .stop(Duration::from_secs(2))
            .await
            .unwrap()
            .returncode(),
        Some(0)
    );
}

#[tokio::test]
async fn shared_nonce_keeps_distinct_ulids_across_shards() {
    let drivers = [observed("normal", "Master"), observed("normal", "Caves")];
    let mut ids = std::collections::BTreeSet::new();
    for (driver, mut events) in drivers {
        driver.wait_ready(Duration::from_secs(3)).await.unwrap();
        let process_id = driver.snapshot().process_id;
        assert_eq!(
            process_id.parse::<ulid::Ulid>().unwrap().to_string(),
            process_id
        );
        assert!(ids.insert(process_id));
        driver
            .request("telemetry", json!({}), Duration::from_secs(1))
            .await
            .unwrap();
        timeout(Duration::from_secs(3), async {
            let mut sequences = Vec::new();
            while sequences.len() < 2 {
                let event = events.recv().await.unwrap();
                assert_eq!(
                    event.id.parse::<ulid::Ulid>().unwrap().to_string(),
                    event.id
                );
                let uid = event.id.clone();
                let observed_at = event.observed_timestamp_ns;
                let shard = event.shard.clone();
                if let DriverEventKind::Telemetry(game) = &event.kind {
                    assert_eq!(event.nonce, NONCE);
                    assert_eq!(event.generation, Some(1));
                    sequences.push(game.seq.0);
                }
                let log = LogEvent::from_driver(event, "cluster").unwrap();
                assert_eq!(log.attributes["log.record.uid"], uid);
                assert_eq!(log.observed_timestamp_ns, observed_at);
                assert_eq!(log.attributes["dst.shard.name"], shard);
                assert!(ids.insert(uid));
            }
            assert_eq!(sequences, [1, 3]);
        })
        .await
        .unwrap();
        driver.kill().await.unwrap();
    }
    let (restarted, _) = observed("normal", "Master");
    assert!(ids.insert(restarted.snapshot().process_id));
    restarted.kill().await.unwrap();
}

#[tokio::test]
async fn presence_ulid_tracks_observation_epochs_and_world_reloads() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    let process_id = driver.snapshot().process_id;
    let first = driver
        .request("presence", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    let id = first["observation"].as_str().unwrap();
    assert_eq!(id.parse::<ulid::Ulid>().unwrap().to_string(), id);
    assert!(first.get("observation_epoch").is_none());
    assert_eq!(
        driver
            .request("presence", json!({}), Duration::from_secs(1))
            .await
            .unwrap(),
        first
    );
    let next = driver
        .request("presence", json!({"epoch": 1}), Duration::from_secs(1))
        .await
        .unwrap();
    assert_ne!(next["observation"], first["observation"]);
    assert_eq!(
        driver
            .request("presence", json!({"epoch": 1}), Duration::from_secs(1))
            .await
            .unwrap(),
        next
    );
    assert!(
        driver
            .request("reload", json!({}), Duration::from_secs(1))
            .await
            .is_err()
    );
    until(&driver, |state| state.ready && state.generation == Some(2)).await;
    assert_eq!(driver.snapshot().process_id, process_id);
    let reloaded = driver
        .request("presence", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    assert_ne!(reloaded["observation"], first["observation"]);
    assert_ne!(reloaded["observation"], next["observation"]);
    assert!(
        driver
            .request("presence", json!({"epoch": -1}), Duration::from_secs(1))
            .await
            .is_err()
    );
    assert!(
        driver
            .snapshot()
            .failure
            .unwrap()
            .message
            .contains("invalid presence observation epoch")
    );
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn readiness_needs_native_evidence_but_optional_telemetry_can_fail() {
    let directory = tempfile::tempdir().unwrap();
    let control = UnixListener::bind(directory.path().join("control.sock")).unwrap();
    let driver = fixture_in("delay_ready", "World", directory.path().to_owned(), None);
    let snapshot = until(&driver, |state| state.health.is_some()).await;
    assert!(!snapshot.ready);
    assert_eq!(
        driver
            .request("runtime", json!({}), Duration::from_secs(1))
            .await
            .unwrap_err()
            .code,
        DriverErrorCode::NotReady
    );
    let (mut ready, _) = timeout(Duration::from_secs(3), control.accept())
        .await
        .unwrap()
        .unwrap();
    ready.write_all(b"G").await.unwrap();
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    let runtime = driver
        .request("runtime", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    assert_eq!(runtime["session_id"], "S1");
    assert_eq!(driver.snapshot().runtime, Some(runtime));
    assert!(driver.kill().await.unwrap().output_drained);
}

#[tokio::test]
async fn cancellation_leaves_one_accepted_mutation_running() {
    let directory = tempfile::tempdir().unwrap();
    let control = UnixListener::bind(directory.path().join("control.sock")).unwrap();
    let driver = fixture_in("normal", "World", directory.path().to_owned(), None);
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    let mut pending = Box::pin(driver.request("slow", json!({}), Duration::from_secs(5)));
    assert!(futures::poll!(&mut pending).is_pending());
    let (mut accepted, _) = timeout(Duration::from_secs(3), control.accept())
        .await
        .unwrap()
        .unwrap();
    until(&driver, |state| {
        state
            .recent_request
            .as_ref()
            .is_some_and(|request| request.accepted)
    })
    .await;
    drop(pending);
    accepted.write_all(b"G").await.unwrap();
    let snapshot = until(&driver, |state| {
        state
            .recent_request
            .as_ref()
            .is_some_and(|request| request.completed)
    })
    .await;
    let request = snapshot.recent_request.unwrap();
    assert!(request.accepted && request.written);
    assert!(request.completed && request.error.is_none());
    assert_eq!(
        driver
            .request("counter", json!({}), Duration::from_secs(1))
            .await
            .unwrap(),
        1
    );
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn only_unaccepted_busy_retries_and_unknown_requests_are_never_replayed() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    assert_eq!(
        driver
            .request("busy", json!({}), Duration::from_secs(2))
            .await
            .unwrap(),
        0
    );
    let unknown = driver
        .request("no_context", json!({}), Duration::from_millis(80))
        .await
        .unwrap_err();
    assert_eq!(unknown.code, DriverErrorCode::Unknown);
    assert!(unknown.written && !unknown.accepted);
    assert_eq!(
        driver
            .request("counter", json!({}), Duration::from_secs(1))
            .await
            .unwrap(),
        0
    );
    let unknown = driver
        .request("accepted_exit", json!({}), Duration::from_secs(1))
        .await
        .unwrap_err();
    assert_eq!(unknown.code, DriverErrorCode::Unknown);
    assert!(unknown.written && unknown.accepted);
    driver.wait().await.unwrap();
}

#[tokio::test]
async fn cancelling_while_fd3_holds_an_earlier_command_never_writes_the_next_one() {
    let directory = tempfile::tempdir().unwrap();
    let control = UnixListener::bind(directory.path().join("control.sock")).unwrap();
    let driver = fixture_in("defer_read", "World", directory.path().into(), None);
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    let (mut gate, _) = control.accept().await.unwrap();
    let first = driver
        .request("counter", json!({}), Duration::from_millis(100))
        .await
        .unwrap_err();
    assert_eq!(first.code, DriverErrorCode::Unknown);
    assert!(first.written);
    let mut cancelled = Box::pin(driver.request("slow", json!({}), Duration::from_secs(3)));
    assert!(futures::poll!(&mut cancelled).is_pending());
    tokio::task::yield_now().await;
    drop(cancelled);
    gate.write_all(b"G").await.unwrap();
    let count = driver
        .request("counter", json!({}), Duration::from_secs(2))
        .await
        .unwrap();
    assert_eq!(count, 0);
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn generation_change_invalidates_pending_work_and_refreshes_runtime() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    driver
        .request("runtime", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    let error = driver
        .request("reload", json!({}), Duration::from_secs(1))
        .await
        .unwrap_err();
    assert_eq!(error.code, DriverErrorCode::Unknown);
    let state = until(&driver, |state| state.ready && state.generation == Some(2)).await;
    assert!(state.runtime.is_none());
    let runtime = driver
        .request("runtime", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    assert_eq!(runtime["session_id"], "S2");
    assert_eq!(driver.snapshot().generation, Some(2));
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn relay_pressure_preserves_control_evidence() {
    let (driver, events) = observed("normal", "World");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    driver
        .request("flood", json!({}), Duration::from_secs(5))
        .await
        .unwrap();
    let state = driver.snapshot();
    assert!(state.running && state.ready);
    assert_eq!(
        state.last_control.as_ref().unwrap()["event"],
        "world_load_decoded"
    );
    assert_eq!(state.control_count, 1);
    timeout(Duration::from_secs(3), async {
        while events.dropped() == 0 {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("queued game logs reach the shard relay limit");
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn malformed_optional_telemetry_is_counted_without_stopping_control() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    driver
        .request("telemetry_oversized", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    let state = until(&driver, |state| state.invalid_telemetry == 1).await;
    let rejection = serde_json::to_value(state.last_telemetry_rejection.unwrap()).unwrap();
    assert_eq!(
        rejection,
        json!({"source":"reply", "bytes":null, "reason":"length", "position":null})
    );
    driver
        .request("telemetry", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    let state = until(&driver, |state| state.telemetry_sequence == 3).await;
    assert!(state.running && state.ready);
    assert_eq!(state.invalid_telemetry, 2);
    assert_eq!(state.telemetry_gaps, 1);
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn cancelled_native_stop_completes_and_drains_control_evidence() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    assert!(
        timeout(
            Duration::from_millis(20),
            driver.stop(Duration::from_secs(2))
        )
        .await
        .is_err()
    );
    let report = timeout(Duration::from_secs(3), driver.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.status.success() && !report.forced && report.output_drained);
    assert_eq!(report.returncode(), Some(0));
    let state = driver.snapshot();
    assert!(!state.running && !state.ready);
    assert_eq!(state.last_control.unwrap()["shutdown"], true);
}

#[tokio::test]
async fn repeated_stops_only_advance_the_existing_native_deadline() {
    for (first, second) in [(2000, 40), (80, 2000)] {
        let driver = fixture("slow_shutdown");
        driver.wait_ready(Duration::from_secs(3)).await.unwrap();
        let begin = tokio::time::Instant::now();
        let copy = driver.clone();
        let initial = tokio::spawn(async move { copy.stop(Duration::from_millis(first)).await });
        until(&driver, |state| {
            state
                .last_control
                .as_ref()
                .is_some_and(|record| record["shutdown"] == true)
        })
        .await;
        let final_report = timeout(
            Duration::from_millis(500),
            driver.stop(Duration::from_millis(second)),
        )
        .await
        .unwrap()
        .unwrap();
        let initial_report = initial.await.unwrap().unwrap();
        assert!(begin.elapsed() < Duration::from_millis(600));
        assert_eq!(initial_report.returncode(), final_report.returncode());
        assert_eq!(final_report.returncode(), Some(0));
        assert!(final_report.forced && final_report.output_drained);
    }
}

#[tokio::test]
async fn native_stop_retains_abnormal_exit_codes_and_control_evidence() {
    for (mode, code) in [("abort_shutdown", -libc::SIGABRT), ("error_shutdown", 7)] {
        let driver = fixture(mode);
        driver.wait_ready(Duration::from_secs(3)).await.unwrap();
        let report = driver.stop(Duration::from_secs(2)).await.unwrap();
        assert!(!report.status.success() && !report.forced && report.output_drained);
        assert_eq!(report.returncode(), Some(code));
        let state = driver.snapshot();
        assert!(!state.running);
        assert_eq!(
            state.exit.as_ref().and_then(|exit| exit.returncode),
            Some(code)
        );
        assert_eq!(state.last_control.unwrap()["shutdown"], true);
    }
}

#[tokio::test]
async fn malformed_mandatory_frames_fail_the_driver() {
    for (method, reason) in [
        ("bad_nonce", ControlFailureReason::Identity),
        ("bad_id", ControlFailureReason::Rpc),
        ("bad_control", ControlFailureReason::Control),
        ("bad_readiness", ControlFailureReason::Bootstrap),
        ("bad_bootstrap", ControlFailureReason::Bootstrap),
    ] {
        let driver = fixture("normal");
        driver.wait_ready(Duration::from_secs(3)).await.unwrap();
        let error = driver
            .request(method, json!({}), Duration::from_secs(1))
            .await
            .unwrap_err();
        assert_eq!(error.code, DriverErrorCode::Unknown, "{method}");
        let report = driver.wait().await.unwrap();
        assert!(report.forced && report.protocol_error.is_none());
        assert_eq!(report.returncode(), Some(-libc::SIGKILL));
        let state = driver.snapshot();
        assert_eq!(state.exit.unwrap().returncode, report.returncode());
        let failure = state.failure.unwrap();
        assert_eq!(failure.reason, Some(reason), "{method}");
        assert!(!failure.message.contains("private-marker"));
    }
}

#[tokio::test]
async fn shard_limits_pending_requests_and_rejects_oversized_input_before_dispatch() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    assert_eq!(
        driver
            .request(
                "runtime",
                json!({"value": "x".repeat(4096)}),
                Duration::from_secs(1)
            )
            .await
            .unwrap_err()
            .code,
        DriverErrorCode::InvalidRequest
    );
    let mut requests = Vec::new();
    for _ in 0..64 {
        let mut request = Box::pin(driver.request("block", json!({}), Duration::from_secs(5)));
        assert!(futures::poll!(&mut request).is_pending());
        requests.push(request);
    }
    assert_eq!(
        driver
            .request("counter", json!({}), Duration::from_secs(1))
            .await
            .unwrap_err()
            .code,
        DriverErrorCode::Busy
    );
    driver.kill().await.unwrap();
    timeout(Duration::from_secs(2), async {
        for request in requests {
            assert!(request.await.is_err());
        }
    })
    .await
    .unwrap();
}
