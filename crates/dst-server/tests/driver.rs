//! Protocol fixtures exercise the real Rust process and persistent driver together.
use std::{path::PathBuf, time::Duration};

use dst_server::{
    driver::{Driver, DriverErrorCode, DriverEventKind, DriverState},
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
import json, os, resource, socket, sys, time
nonce = sys.argv[1]
mode = sys.argv[2]
generation = 1
count = 0
busy = False
def emit(fd, prefix, record):
    os.write(fd, prefix + json.dumps(record, separators=(',', ':')).encode() + b'\n')
def bootstrap():
    emit(1, b'DST_DRIVER|', {'nonce': nonce, 'generation': generation})
    emit(1, b'DST_DRIVER|', {'nonce': nonce, 'health': {'generation': generation,
        'protocol': 3, 'telemetry_status': 'failed', 'capabilities': {'players': 'active'}}})
def ready():
    os.write(5, f'DST_SessionId|S{generation}\nDST_Master_Ready\n'.encode())
def control(shutdown=False):
    emit(4, b'DST_CONTROL|', {'v': 1, 'nonce': nonce, 'generation': generation,
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
while True:
    encoded = os.read(3, 4096)
    if not encoded:
        break
    if encoded == b'c_shutdown()\n':
        control(True)
        os.write(5, b'DST_Stopping\nDST_Shutdown\n')
        time.sleep(0.15)
        if mode == 'abort_shutdown':
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.abort()
        if mode == 'error_shutdown':
            os._exit(7)
        break
    request = json.loads(encoded.removeprefix(b'DST_RPC|'))
    method = request['method']
    header = {key: request[key] for key in ('v', 'nonce', 'id', 'generation')}
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
        emit(4, b'DST_RPC|', {**header, 'nonce': '01J00000000000000000000001', 'result': {'ok': True, 'data': 0}})
        time.sleep(60)
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
        os.write(1, b'DST_OTEL|{invalid\n')
        for sequence in (1, 3):
            emit(1, b'DST_OTEL|', {'v': 3, 'nonce': nonce, 'generation': generation,
                'session_id': f'S{generation}', 'seq': sequence, 'event': 'dst.world.state_changed',
                'tick': sequence, 'monotonic_ms': sequence, 'cycle': None, 'data': {'name': 'cycles', 'value': sequence}})
    data = {'session_id': f'S{generation}', 'snapshot': 6, 'shard_id': '1', 'is_master_shard': True} if method == 'runtime' else count
    if method == 'presence':
        data = {'observation_epoch': request['arguments'].get('epoch', 0), 'session_id': f'S{generation}'}
    emit(4, b'DST_RPC|', {**header, 'result': {'ok': True, 'data': data}})
    os.write(4, b'DST_RemoteCommandDone\n')
"#;

fn fixture(mode: &str) -> Driver {
    fixture_on_shard(mode, "World")
}

fn fixture_on_shard(mode: &str, shard: &str) -> Driver {
    fixture_in(mode, shard, std::env::temp_dir())
}

fn fixture_in(mode: &str, shard: &str, directory: PathBuf) -> Driver {
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
async fn shared_nonce_keeps_distinct_ulids_across_shards_and_subscribers() {
    let drivers = [
        fixture_on_shard("normal", "Master"),
        fixture_on_shard("normal", "Caves"),
    ];
    let mut ids = std::collections::BTreeSet::new();
    for driver in &drivers {
        driver.wait_ready(Duration::from_secs(3)).await.unwrap();
        let process_id = driver.snapshot().process_id;
        assert_eq!(
            process_id.parse::<ulid::Ulid>().unwrap().to_string(),
            process_id
        );
        assert!(ids.insert(process_id));
        let mut first = driver.subscribe();
        let mut second = driver.subscribe();
        driver
            .request("telemetry", json!({}), Duration::from_secs(1))
            .await
            .unwrap();
        timeout(Duration::from_secs(3), async {
            let mut sequences = Vec::new();
            while sequences.len() < 2 {
                let event = first.recv().await.unwrap();
                let duplicate = second.recv().await.unwrap();
                assert_eq!(event.id, duplicate.id);
                assert_eq!(
                    event.id.parse::<ulid::Ulid>().unwrap().to_string(),
                    event.id
                );
                assert!(ids.insert(event.id.clone()));
                let log = LogEvent::from_driver(&event, "cluster").unwrap();
                assert_eq!(log.attributes["log.record.uid"], event.id);
                assert_eq!(
                    log.attributes["log.record.uid"],
                    LogEvent::from_driver(&duplicate, "cluster")
                        .unwrap()
                        .attributes["log.record.uid"]
                );
                if matches!(event.kind, DriverEventKind::Telemetry) {
                    assert_eq!(event.nonce, NONCE);
                    assert_eq!(event.generation, Some(1));
                    sequences.push(event.data["seq"].as_u64().unwrap());
                }
            }
            assert_eq!(sequences, [1, 3]);
        })
        .await
        .unwrap();
        driver.kill().await.unwrap();
    }
    let restarted = fixture_on_shard("normal", "Master");
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
            .contains("invalid presence observation epoch")
    );
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn readiness_needs_native_evidence_but_optional_telemetry_can_fail() {
    let directory = tempfile::tempdir().unwrap();
    let control = UnixListener::bind(directory.path().join("control.sock")).unwrap();
    let driver = fixture_in("delay_ready", "World", directory.path().to_owned());
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
    let driver = fixture_in("normal", "World", directory.path().to_owned());
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
    assert_eq!(request.result, Some(json!(1)));
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
async fn subscriber_pressure_preserves_control_evidence() {
    let driver = fixture("normal");
    let subscriber = driver.subscribe();
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
    assert_eq!(state.control_records.len(), 1);
    timeout(Duration::from_secs(3), async {
        while subscriber.dropped() == 0 {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("queued game logs reach the bounded subscriber");
    driver.kill().await.unwrap();
}

#[tokio::test]
async fn malformed_optional_telemetry_is_counted_without_stopping_control() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    driver
        .request("telemetry", json!({}), Duration::from_secs(1))
        .await
        .unwrap();
    let state = until(&driver, |state| state.telemetry_sequence == 3).await;
    assert!(state.running && state.ready);
    assert_eq!(state.invalid_telemetry, 1);
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
async fn native_stop_retains_abnormal_exit_codes_and_control_evidence() {
    for (mode, code) in [("abort_shutdown", -libc::SIGABRT), ("error_shutdown", 7)] {
        let driver = fixture(mode);
        driver.wait_ready(Duration::from_secs(3)).await.unwrap();
        let report = driver.stop(Duration::from_secs(2)).await.unwrap();
        assert!(!report.status.success() && !report.forced && report.output_drained);
        assert_eq!(report.returncode(), Some(code));
        let state = driver.snapshot();
        assert!(!state.running);
        assert_eq!(state.returncode, Some(code));
        assert_eq!(state.last_control.unwrap()["shutdown"], true);
    }
}

#[tokio::test]
async fn mandatory_nonce_mismatch_fails_the_driver() {
    let driver = fixture("normal");
    driver.wait_ready(Duration::from_secs(3)).await.unwrap();
    assert_eq!(
        driver
            .request("bad_nonce", json!({}), Duration::from_secs(1))
            .await
            .unwrap_err()
            .code,
        DriverErrorCode::Unknown
    );
    let report = driver.wait().await.unwrap();
    assert!(report.forced);
    assert_eq!(report.returncode(), Some(-libc::SIGKILL));
    assert_eq!(driver.snapshot().returncode, Some(-libc::SIGKILL));
    assert!(driver.snapshot().failure.unwrap().contains("nonce"));
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
