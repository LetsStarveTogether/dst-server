//! Native snapshot inspection and fixed-target recovery while the whole room is stopped.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::oneshot;
use tokio::time::{Instant, timeout_at};

use crate::configuration::{self, Cluster};
use crate::driver::{Driver, DriverOptions, DriverState};
use crate::files::RoomLock;
use crate::model::{Error, ErrorCode, Result};
use crate::recovery::{
    self, Catalog, Catalogs, Clock, Failure, FixedTarget, Snapshot, TargetPhase,
};
use crate::room::{Room, StoppedOperation};

/// A cancelled caller stops waiting; native children are killed, reaped and drained
/// before the background owner releases the room's mutation permit.
pub async fn catalog(room: Arc<Room>, duration: Duration) -> Result<Catalogs> {
    let stopped = room.while_stopped().await?;
    let deadline = deadline(duration)?;
    let (cancel, mut cancelled) = oneshot::channel();
    let (reply, receive) = oneshot::channel();
    tokio::spawn(async move {
        let result = catalog_stopped(&stopped, deadline, &mut cancelled).await;
        drop(stopped);
        if let Err(Err(error)) = reply.send(result) {
            eprintln!("native catalog cleanup: {error}");
        }
    });
    let _cancel = cancel;
    receive
        .await
        .map_err(|_| Error::new(ErrorCode::Internal, "native catalog worker stopped"))?
}

/// Every mutation uses the already-durable target; each verified native receipt
/// is persisted before the next shard is allowed to change its snapshots.
pub async fn apply(room: Arc<Room>, target: FixedTarget, duration: Duration) -> Result<()> {
    let stopped = room.while_stopped().await?;
    let deadline = deadline(duration)?;
    let (cancel, mut cancelled) = oneshot::channel();
    let (reply, receive) = oneshot::channel();
    tokio::spawn(async move {
        let result = apply_stopped(&stopped, target, deadline, &mut cancelled).await;
        drop(stopped);
        if let Err(Err(error)) = reply.send(result) {
            eprintln!("native recovery cleanup: {error}");
        }
    });
    let _cancel = cancel;
    receive
        .await
        .map_err(|_| Error::new(ErrorCode::Internal, "native recovery worker stopped"))?
}

fn deadline(duration: Duration) -> Result<Instant> {
    if duration.is_zero() {
        return Err(Error::invalid(
            "timeout",
            "preloader timeout must be positive",
        ));
    }
    Instant::now()
        .checked_add(duration)
        .ok_or_else(|| Error::invalid("timeout", "preloader timeout is too large"))
}

async fn catalog_stopped(
    stopped: &StoppedOperation,
    deadline: Instant,
    cancelled: &mut oneshot::Receiver<()>,
) -> Result<Catalogs> {
    let cluster = configuration::discover(stopped.directory()).map_err(internal)?;
    let mut catalogs = Catalogs::new();
    for shard in &cluster.shards {
        let record = native(
            stopped,
            &cluster,
            &shard.name,
            json!({"mode":"catalog"}),
            deadline,
            cancelled,
        )
        .await?;
        let catalog = stopped.with_lock(|lock| parse_catalog(lock, &shard.name, &record))?;
        catalogs.insert(shard.name.clone(), catalog);
    }
    Ok(catalogs)
}

async fn apply_stopped(
    stopped: &StoppedOperation,
    target: FixedTarget,
    deadline: Instant,
    cancelled: &mut oneshot::Receiver<()>,
) -> Result<()> {
    let cluster = configuration::discover(stopped.directory()).map_err(internal)?;
    stopped.with_lock(|lock| {
        let state = recovery::load(lock)?;
        ensure!(state.closed.is_none(), "recovery incident is closed");
        ensure!(
            target.phase == TargetPhase::Preparing && state.target.as_ref() == Some(&target),
            "requested recovery target differs from the persisted target"
        );
        let expected: BTreeSet<_> = cluster
            .shards
            .iter()
            .map(|shard| shard.name.as_str())
            .collect();
        ensure!(
            expected == target.shards.keys().map(String::as_str).collect(),
            "persisted recovery target differs from native shard topology"
        );
        Ok(())
    })?;
    for shard in &cluster.shards {
        let saved = &target.shards[&shard.name];
        // Repeating an already-applied shard is safe: native recovery verifies the
        // exact target and reports changed=false, even after an Agent crash.
        let record = native(
            stopped,
            &cluster,
            &shard.name,
            json!({
                "mode":"apply", "session_id":saved.session_id,
                "snapshot_id":saved.snapshot_id, "world_file":saved.world_file,
            }),
            deadline,
            cancelled,
        )
        .await?;
        if record["event"] != "recovery_applied"
            || record["session_id"].as_str() != Some(&saved.session_id)
            || record["snapshot_id"].as_u64() != Some(saved.snapshot_id)
            || record["world_file"].as_str() != Some(&saved.world_file)
            || !record["changed"].is_boolean()
        {
            return Err(Error::new(
                ErrorCode::Protocol,
                "native recovery receipt differs from the persisted target",
            )
            .with_details(json!({"shard":shard.name})));
        }
        stopped.with_lock(|lock| {
            recovery::mark_applied(
                lock,
                &shard.name,
                &saved.session_id,
                saved.snapshot_id,
                &saved.world_file,
            )
        })?;
    }
    Ok(())
}

async fn native(
    stopped: &StoppedOperation,
    cluster: &Cluster,
    shard: &str,
    request: Value,
    deadline: Instant,
    cancelled: &mut oneshot::Receiver<()>,
) -> Result<Value> {
    if !matches!(
        cancelled.try_recv(),
        Err(oneshot::error::TryRecvError::Empty)
    ) {
        return Err(Error::new(
            ErrorCode::Unknown,
            "native preloader was cancelled",
        ));
    }
    if Instant::now() >= deadline {
        return Err(Error::new(
            ErrorCode::Timeout,
            "native preloader deadline expired",
        ));
    }
    let driver = Driver::spawn(
        cluster,
        stopped.executable(),
        shard,
        DriverOptions {
            profile: "off".into(),
            actions: Vec::new(),
            control: json!({"observe_saves":true,"recovery":request}),
            extra_args: vec!["-skip_update_server_mods".into(), "-offline".into()],
        },
    )
    .map_err(internal)?;
    let outcome = tokio::select! {
        result = timeout_at(deadline, driver.wait()) => match result {
            Ok(Ok(exit)) => {
                if exit.status.success() && !exit.forced && exit.output_drained && exit.protocol_error.is_none() {
                    validate_result(&driver.snapshot())
                } else {
                    Err(Error::new(ErrorCode::Transport, "native preloader did not exit cleanly").with_details(json!({"shard":shard,"returncode":exit.returncode(),"forced":exit.forced,"output_drained":exit.output_drained})))
                }
            },
            Ok(Err(error)) => Err(Error::new(ErrorCode::Transport, error.to_string())),
            Err(_) => Err(Error::new(ErrorCode::Timeout, "native preloader deadline expired")),
        },
        _ = cancelled => Err(Error::new(ErrorCode::Unknown, "native preloader was cancelled")),
    };
    // Driver owns its process supervisor. Always await it here before releasing the
    // stopped-operation guard, including on timeout or cancellation.
    let cleanup = driver.kill().await;
    match (outcome, cleanup) {
        (result, Ok(_)) => result,
        (Ok(_), Err(cleanup)) => Err(Error::new(
            ErrorCode::Transport,
            format!("native preloader cleanup failed: {cleanup}"),
        )),
        (Err(error), Err(cleanup)) => Err(Error::new(
            error.code,
            format!(
                "{}; native preloader cleanup failed: {cleanup}",
                error.message
            ),
        )
        .with_details(error.details)),
    }
}

fn validate_result(state: &DriverState) -> Result<Value> {
    if state.failure.is_some()
        || state.native_ready
        || state.ready
        || state.health.is_some()
        || state.last_native_save.is_some()
        || state.generation_changes != 0
        || state.control_records.len() != 1
    {
        return Err(Error::new(
            ErrorCode::Protocol,
            "native preloader loaded a world, changed generation, or produced invalid evidence",
        )
        .with_details(json!({"shard":state.shard})));
    }
    let record = &state.control_records[0];
    if record["event"] == "recovery_failed" {
        return Err(
            Error::new(ErrorCode::Conflict, "native recovery refused the request")
                .with_details(json!({"shard":state.shard,"reason":record["error"]})),
        );
    }
    if !matches!(
        record["event"].as_str(),
        Some("recovery_catalog" | "recovery_applied")
    ) || record["nonce"] != state.nonce
        || record["generation"].as_u64() != state.generation
    {
        return Err(Error::new(
            ErrorCode::Protocol,
            "native preloader returned invalid control evidence",
        ));
    }
    Ok(record.clone())
}

#[derive(Deserialize)]
struct NativeSnapshot {
    snapshot_id: u64,
    world_file: Option<String>,
}

fn parse_catalog(lock: &RoomLock, shard: &str, record: &Value) -> anyhow::Result<Catalog> {
    ensure!(
        record["event"] == "recovery_catalog" && record["has_more"].is_boolean(),
        "native preloader did not return a snapshot catalog"
    );
    let session = record["session_id"]
        .as_str()
        .filter(|value| valid_text(value, 128))
        .context("native catalog has no valid session")?;
    let latest = record["latest_world_file"]
        .as_str()
        .context("native catalog has no latest world file")?;
    native_path(shard, session, latest)?;
    let snapshots: Vec<NativeSnapshot> = serde_json::from_value(record["snapshots"].clone())
        .context("invalid native snapshot entries")?;
    let mut ids = BTreeSet::new();
    let mut paths = BTreeSet::new();
    let mut values = Vec::with_capacity(snapshots.len());
    for snapshot in snapshots {
        ensure!(
            snapshot.snapshot_id > 0
                && snapshot.snapshot_id <= crate::lua::MAX_SAFE_INTEGER as u64
                && ids.insert(snapshot.snapshot_id),
            "invalid or duplicate native snapshot identity"
        );
        let clock = if let Some(file) = &snapshot.world_file {
            ensure!(paths.insert(file.clone()), "duplicate native world file");
            let path = native_path(shard, session, file)?;
            // Validate the native world's file type and every parent without following links.
            lock.open_regular(&path)
                .context("open native world snapshot")?;
            let metadata = PathBuf::from(format!(
                "{}.meta",
                path.to_str().context("invalid native world path")?
            ));
            lock.read_text(metadata)
                .ok()
                .and_then(|source| Clock::from_metadata(&source).ok())
        } else {
            None
        };
        values.push(Snapshot {
            snapshot_id: snapshot.snapshot_id,
            world_file: snapshot.world_file,
            clock,
        });
    }
    ensure!(
        paths.contains(latest),
        "native latest world file is absent from its catalog"
    );
    Ok(Catalog {
        session_id: session.into(),
        latest_world_file: latest.into(),
        snapshots: values,
    })
}

fn native_path(shard: &str, session: &str, file: &str) -> anyhow::Result<PathBuf> {
    ensure!(
        valid_text(file, 4096) && !file.contains('\\'),
        "invalid native world path"
    );
    let relative = file.strip_prefix("save/").unwrap_or(file);
    let parts: Vec<_> = relative.split('/').collect();
    ensure!(
        parts.len() == 3
            && parts[0] == "session"
            && parts[1] == session
            && parts
                .iter()
                .all(|part| !part.is_empty() && !matches!(*part, "." | "..")),
        "native world path does not belong to its session"
    );
    let path = Path::new(relative);
    Ok(Path::new(shard).join("save").join(path))
}

fn valid_text(value: &str, limit: usize) -> bool {
    !value.is_empty() && value.len() <= limit && !value.chars().any(char::is_control)
}

/// Only structured native decoding evidence permits automatic rollback. Generic
/// log messages, read failures and failures inside world/Mod callbacks stay unknown.
pub fn classify(states: &[DriverState]) -> Failure {
    if states.is_empty()
        || states
            .iter()
            .any(|state| state.running || !state.output_drained || state.failure.is_some())
    {
        return Failure::Unknown;
    }
    let mut load_failure = None;
    for state in states {
        let Some(record) = &state.load_failure else {
            continue;
        };
        let session = record["session_id"]
            .as_str()
            .filter(|value| valid_text(value, 128));
        let file = record["world_file"]
            .as_str()
            .filter(|value| valid_text(value, 4096));
        if record["event"] != "world_load_failed"
            || record["v"] != 1
            || record["nonce"] != state.nonce
            || record["generation"].as_u64() != state.generation
            || record["phase"] != "decode"
            || record["read_succeeded"] != true
            || record["callback_entered"] != false
            || record["decoder_unchanged"] != true
            || !record["source_bytes"]
                .as_u64()
                .is_some_and(|bytes| bytes > 0)
            || !matches!(
                record["reason"].as_str(),
                Some("parse_failed" | "decoded_nil" | "decoded_empty")
            )
            || session.is_none()
            || file.is_none()
        {
            return Failure::Unknown;
        }
        let (session, file) = (session.unwrap(), file.unwrap());
        if native_path(&state.shard, session, file).is_err() {
            return Failure::Unknown;
        }
        if load_failure.is_none() {
            load_failure = Some(Failure::LoadFailure {
                shard: state.shard.clone(),
                session_id: session.into(),
                world_file: file.into(),
            });
        }
    }
    load_failure.unwrap_or_else(|| {
        if states
            .iter()
            .any(|state| state.native_ready && state.runtime.is_some())
        {
            Failure::Retryable
        } else {
            Failure::Unknown
        }
    })
}

pub fn classify_io(error: &std::io::Error) -> Failure {
    match error.raw_os_error() {
        Some(libc::EACCES | libc::EPERM) => Failure::Permission,
        Some(libc::ENOSPC | libc::EDQUOT | libc::EIO | libc::EROFS | libc::ENODEV) => Failure::Disk,
        _ => Failure::Unknown,
    }
}

fn internal(error: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Internal, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        recovery::{RecoveryState, SavedShard},
        rpc::EventHub,
    };
    use std::{
        collections::BTreeMap,
        fs,
        os::unix::fs::{PermissionsExt, symlink},
    };

    const META: &str = "return { clock={ cycles=2, segs={day=10,dusk=4,night=2}, phase=\"day\", totaltimeinphase=300, remainingtimeinphase=150 } }\0";
    const FAKE: &str = r#"#!/usr/bin/env python3
import json, os, pathlib, sys, time
def argument(key): return sys.argv[sys.argv.index(key) + 1]
root = pathlib.Path(argument('-persistent_storage_root')) / argument('-cluster')
shard = argument('-shard')
directory = root / shard
config = json.loads((directory / 'dst_server_driver.json').read_text())
request = config['control']['recovery']
scenario = (directory / 'scenario').read_text()
(directory / 'pid').write_text(str(os.getpid()))
with (directory / 'calls').open('a') as output: output.write(request['mode'] + '\n')
if scenario == 'sleep':
    time.sleep(30)
    sys.exit(0)
if scenario == 'empty': sys.exit(0)
if scenario == 'refuse': record = {'event':'recovery_failed', 'error':'fixture refusal'}
elif request['mode'] == 'catalog':
    record = json.loads((directory / 'catalog').read_text())
else:
    changed = not (directory / 'applied').exists()
    (directory / 'applied').write_text(str(request['snapshot_id']))
    record = dict(request, event='recovery_applied', changed=changed)
record.update(v=1, nonce=config['nonce'], generation=1)
def emit(record): os.write(4, ('DST_CONTROL|' + json.dumps(record) + '\n').encode())
emit(record)
if scenario == 'duplicate': emit(record)
if scenario == 'generation':
    record['generation'] = 2
    emit(record)
if scenario == 'ready': os.write(5, b'DST_Master_Ready\n')
"#;

    struct Fixture {
        _temp: tempfile::TempDir,
        root: PathBuf,
        room: Arc<Room>,
    }

    impl Fixture {
        fn new(names: &[&str]) -> Self {
            let temp = tempfile::tempdir().unwrap();
            let root = temp.path().join("cluster");
            fs::create_dir(&root).unwrap();
            fs::write(
                root.join("cluster.ini"),
                "[SHARD]\nshard_enabled=true\ncluster_key=fixture\nmaster_ip=127.0.0.1\n",
            )
            .unwrap();
            for (index, name) in names.iter().enumerate() {
                let path = root.join(name);
                fs::create_dir(&path).unwrap();
                fs::write(path.join("server.ini"), format!("[SHARD]\nis_master={}\nid={}\nname={}\n[NETWORK]\nserver_port={}\n[STEAM]\nmaster_server_port={}\n", index == 0, index + 1, name, 11000 + index, 28000 + index)).unwrap();
                let session = format!("SESSION{index}");
                let directory = path.join("save/session").join(&session);
                fs::create_dir_all(&directory).unwrap();
                for id in [1, 2] {
                    fs::write(directory.join(format!("{id:010}")), b"native world").unwrap();
                    fs::write(directory.join(format!("{id:010}.meta")), META).unwrap();
                }
                let record = json!({"event":"recovery_catalog", "session_id":session,
                    "latest_world_file":format!("session/{session}/0000000002"), "has_more":false,
                    "snapshots":[{"snapshot_id":1,"world_file":format!("session/{session}/0000000001")},{"snapshot_id":2,"world_file":format!("session/{session}/0000000002")}]});
                fs::write(path.join("catalog"), record.to_string()).unwrap();
                fs::write(path.join("scenario"), "ok").unwrap();
            }
            let executable = temp.path().join("native");
            fs::write(&executable, FAKE).unwrap();
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
            let room = Room::open(
                &root,
                executable,
                DriverOptions::default(),
                EventHub::default(),
            )
            .unwrap();
            Self {
                _temp: temp,
                root,
                room,
            }
        }

        fn scenario(&self, name: &str, scenario: &str) {
            fs::write(self.root.join(name).join("scenario"), scenario).unwrap();
        }

        fn fix_target(&self, catalogs: &Catalogs) -> FixedTarget {
            let target = FixedTarget {
                snapshot_id: 1,
                shards: catalogs
                    .iter()
                    .map(|(name, catalog)| {
                        let snapshot = &catalog.snapshots[0];
                        (
                            name.clone(),
                            SavedShard {
                                session_id: catalog.session_id.clone(),
                                snapshot_id: snapshot.snapshot_id,
                                world_file: snapshot.world_file.clone().unwrap(),
                                clock: snapshot.clock.clone().unwrap(),
                            },
                        )
                    })
                    .collect(),
                applied: BTreeSet::new(),
                phase: TargetPhase::Preparing,
                skipped_current_retries: false,
            };
            self.room
                .with_lock(|lock| {
                    lock.update_control(|state| {
                        state.insert(
                            "recovery".into(),
                            serde_json::to_value(RecoveryState {
                                target: Some(target.clone()),
                                ..Default::default()
                            })?,
                        );
                        state.insert("policy_fixture".into(), json!({"retained":true}));
                        Ok(())
                    })
                })
                .unwrap();
            target
        }
    }

    #[tokio::test]
    async fn reads_native_catalog_with_nul_metadata_and_rejects_unsafe_evidence() {
        let fixture = Fixture::new(&["Master", "Caves"]);
        fs::remove_file(
            fixture
                .root
                .join("Caves/save/session/SESSION1/0000000001.meta"),
        )
        .unwrap();
        let catalogs = catalog(fixture.room.clone(), Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(catalogs.len(), 2);
        assert_eq!(
            catalogs["Master"].snapshots[0]
                .clock
                .as_ref()
                .unwrap()
                .fraction()
                .unwrap(),
            0.3125
        );
        assert!(catalogs["Caves"].snapshots[0].clock.is_none());
        for scenario in ["empty", "duplicate", "generation", "ready", "refuse"] {
            fixture.scenario("Master", scenario);
            assert!(
                catalog(fixture.room.clone(), Duration::from_secs(5))
                    .await
                    .is_err(),
                "{scenario}"
            );
        }
        fixture.scenario("Master", "ok");
        let world = fixture.root.join("Master/save/session/SESSION0/0000000001");
        fs::remove_file(&world).unwrap();
        symlink("0000000002", &world).unwrap();
        assert!(
            catalog(fixture.room.clone(), Duration::from_secs(5))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn persists_each_apply_receipt_and_replays_the_fixed_target() {
        let fixture = Fixture::new(&["Master", "Caves"]);
        let catalogs = catalog(fixture.room.clone(), Duration::from_secs(5))
            .await
            .unwrap();
        let target = fixture.fix_target(&catalogs);
        fixture.scenario("Caves", "refuse");
        assert!(
            apply(fixture.room.clone(), target.clone(), Duration::from_secs(5))
                .await
                .is_err()
        );
        let state = fixture.room.with_lock(|lock| recovery::load(lock)).unwrap();
        let partial = state.target.unwrap();
        assert_eq!(partial.applied, BTreeSet::from(["Master".into()]));
        assert_eq!(partial.shards, target.shards);
        assert!(
            apply(fixture.room.clone(), target, Duration::from_secs(5))
                .await
                .is_err()
        );
        fixture.scenario("Caves", "ok");
        apply(
            fixture.room.clone(),
            partial.clone(),
            Duration::from_secs(5),
        )
        .await
        .unwrap();
        let completed = fixture
            .room
            .with_lock(|lock| recovery::load(lock))
            .unwrap()
            .target
            .unwrap();
        assert_eq!(completed.applied.len(), 2);
        assert_eq!(completed.shards, partial.shards);
        assert_eq!(
            fs::read_to_string(fixture.root.join("Master/calls")).unwrap(),
            "catalog\napply\napply\n"
        );
        fixture
            .room
            .with_lock(|lock| {
                assert_eq!(lock.read_control()?["policy_fixture"]["retained"], true);
                recovery::begin_recovered_room(lock)
            })
            .unwrap();
    }

    #[tokio::test]
    async fn cancellation_and_timeout_reap_native_before_releasing_room() {
        let fixture = Fixture::new(&["Master"]);
        fixture.scenario("Master", "sleep");
        let room = fixture.room.clone();
        let task = tokio::spawn(async move { catalog(room, Duration::from_secs(20)).await });
        let pid_path = fixture.root.join("Master/pid");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !pid_path.exists() {
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let pid: u32 = fs::read_to_string(&pid_path).unwrap().parse().unwrap();
        assert!(matches!(
            fixture.room.while_stopped().await,
            Err(Error {
                code: ErrorCode::Busy,
                ..
            })
        ));
        task.abort();
        let _ = task.await;
        loop {
            if let Ok(guard) = fixture.room.while_stopped().await {
                assert!(!Path::new(&format!("/proc/{pid}")).exists());
                drop(guard);
                break;
            }
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let error = catalog(fixture.room.clone(), Duration::from_millis(100))
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Timeout);
        let pid: u32 = fs::read_to_string(pid_path).unwrap().parse().unwrap();
        assert!(!Path::new(&format!("/proc/{pid}")).exists());
        fixture.room.while_stopped().await.unwrap();
    }

    #[test]
    fn only_verified_native_decode_failures_allow_rollback() {
        let record = json!({"v":1,"nonce":"fixture","generation":1,"event":"world_load_failed",
            "session_id":"SESSION","world_file":"session/SESSION/0000000002","phase":"decode",
            "reason":"parse_failed","read_succeeded":true,"callback_entered":false,"source_bytes":4,"decoder_unchanged":true});
        let state = DriverState {
            shard: "Master".into(),
            pid: 1,
            nonce: "fixture".into(),
            generation: Some(1),
            generation_changes: 0,
            native_ready: false,
            ready: false,
            running: false,
            stopping: false,
            returncode: Some(1),
            forced: false,
            output_drained: true,
            load_failure: Some(record.clone()),
            session_id: None,
            health: None,
            runtime: None,
            last_native_save: None,
            last_control: None,
            control_records: Default::default(),
            failure: None,
            telemetry_sequence: 0,
            telemetry_gaps: 0,
            invalid_telemetry: 0,
            stale_telemetry: 0,
            unmatched_replies: 0,
            recent_request: None,
        };
        assert!(
            matches!(classify(std::slice::from_ref(&state)), Failure::LoadFailure {world_file, ..} if world_file == "session/SESSION/0000000002")
        );
        for (field, value) in BTreeMap::from([
            ("phase", json!("read")),
            ("read_succeeded", json!(false)),
            ("callback_entered", json!(true)),
            ("source_bytes", json!(0)),
            ("reason", json!("native_error")),
            ("decoder_unchanged", json!(false)),
            ("nonce", json!("stale")),
            ("generation", json!(2)),
            ("world_file", json!("session/SESSION/../outside")),
        ]) {
            let mut changed = state.clone();
            changed.load_failure.as_mut().unwrap()[field] = value;
            assert!(matches!(classify(&[changed]), Failure::Unknown), "{field}");
        }
        let mut ready = state.clone();
        ready.load_failure = None;
        ready.native_ready = true;
        ready.runtime = Some(json!({}));
        assert!(matches!(
            classify(std::slice::from_ref(&ready)),
            Failure::Retryable
        ));
        ready.output_drained = false;
        assert!(matches!(classify(&[ready]), Failure::Unknown));
        assert!(matches!(
            classify_io(&std::io::Error::from_raw_os_error(libc::EACCES)),
            Failure::Permission
        ));
        assert!(matches!(
            classify_io(&std::io::Error::from_raw_os_error(libc::ENOSPC)),
            Failure::Disk
        ));
        assert!(matches!(
            classify_io(&std::io::Error::from_raw_os_error(libc::EINVAL)),
            Failure::Unknown
        ));
    }
}
