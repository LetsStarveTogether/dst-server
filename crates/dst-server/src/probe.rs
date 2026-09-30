//! Destructive integration checks restricted to explicitly marked disposable rooms.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::ffi::OsString;
use std::fs::{self, OpenOptions};
use std::future::{Future, poll_fn};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::task::Poll;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use tokio::time::{Instant, timeout_at};
use ulid::Ulid;

use crate::configuration::{self, Cluster};
use crate::process::{EventKind, GameProcess, ProcessSpec, StreamKind, native_message};

struct Shard {
    name: String,
    process: GameProcess,
    nonce: String,
    generation: Option<u64>,
    ready: bool,
    native_ready: bool,
    ended: bool,
    pending: Option<String>,
    accepted: bool,
    native_done: bool,
    busy: bool,
    result: Option<Value>,
    saves: VecDeque<Value>,
    tail: VecDeque<String>,
}

struct Room {
    shards: Vec<Shard>,
    cursor: usize,
    recovering: bool,
    player: Option<PlayerProbe>,
}

#[derive(Clone, Copy)]
enum PlayerProbe {
    Write(u32),
    Read,
}

fn deadline(timeout: u64) -> Result<Instant> {
    Instant::now()
        .checked_add(Duration::from_secs(timeout))
        .context("probe timeout is too large")
}

/// Run native load, control, coordinated save, and graceful exit in one container.
/// Each round uses the preceding round's saves and fresh driver nonces.
pub async fn run(cluster: &Path, executable: &Path, timeout: u64, rounds: usize) -> Result<Value> {
    ensure!(
        timeout > 0 && rounds > 0,
        "timeout and rounds must be positive"
    );
    let cluster = disposable(cluster)?;
    let executable = executable
        .canonicalize()
        .context("resolve game executable")?;
    let mut reports = Vec::new();
    let mut previous_sessions: Option<Vec<Value>> = None;
    for round in 1..=rounds {
        let mut room = Room {
            shards: Vec::new(),
            cursor: 0,
            recovering: false,
            player: None,
        };
        let deadline = deadline(timeout)?;
        let result = timeout_at(deadline, async {
            room.launch(&cluster, &executable, None)?;
            room.exercise().await
        })
        .await;
        let result = match result {
            Ok(result) => result,
            Err(_) => Err(anyhow::anyhow!(
                "round exceeded its {timeout}s total deadline"
            )),
        };
        // Await every supervisor even when spawn, parsing, or another child failed.
        let mut cleanup_errors = Vec::new();
        for shard in &room.shards {
            if let Err(error) = shard.process.kill().await {
                cleanup_errors.push(format!("{}: {error}", shard.name));
            }
        }
        let report = result.with_context(|| {
            format!(
                "native probe round {round} failed; cleanup errors: {cleanup_errors:?}\n{}",
                room.diagnostics()
            )
        })?;
        ensure!(
            cleanup_errors.is_empty(),
            "probe cleanup failed: {cleanup_errors:?}"
        );
        let sessions: Vec<_> = report["shards"]
            .as_array()
            .unwrap()
            .iter()
            .map(|shard| shard["runtime"]["session_id"].clone())
            .collect();
        if let Some(previous) = &previous_sessions {
            ensure!(
                *previous == sessions,
                "room sessions changed during ordinary restart"
            );
        }
        previous_sessions = Some(sessions);
        reports.push(report);
    }
    Ok(
        json!({"cluster": cluster.directory, "shard_count": cluster.shards.len(),
        "rounds": reports, "passed": true}),
    )
}

/// Enumerate native snapshots before loading worlds, or apply exact catalog targets.
/// Target values contain session_id, snapshot_id, world_file and optionally mode="apply".
pub async fn recovery(
    cluster: &Path,
    executable: &Path,
    timeout: u64,
    targets: Option<&BTreeMap<String, Value>>,
) -> Result<Value> {
    ensure!(timeout > 0, "timeout must be positive");
    let cluster = disposable(cluster)?;
    let requests = recovery_requests(&cluster, targets)?;
    let executable = executable
        .canonicalize()
        .context("resolve game executable")?;
    let mut room = Room {
        shards: Vec::new(),
        cursor: 0,
        recovering: true,
        player: None,
    };
    let result = timeout_at(deadline(timeout)?, async {
        room.launch(&cluster, &executable, Some(&requests))?;
        while room.shards.iter().any(|shard| !shard.ended) {
            room.pump(true).await?;
        }
        let mut records = BTreeMap::new();
        for shard in &room.shards {
            let exit = shard.process.wait().await?;
            ensure!(
                exit.status.success()
                    && !exit.forced
                    && exit.output_drained
                    && exit.protocol_error.is_none(),
                "{} recovery process did not exit cleanly: {exit:?}",
                shard.name
            );
            ensure!(
                shard.saves.len() == 1,
                "{} recovery did not publish exactly one result",
                shard.name
            );
            let record = &shard.saves[0];
            let request = &requests[&shard.name];
            if targets.is_some() {
                ensure!(
                    record["event"] == "recovery_applied"
                        && record["session_id"] == request["session_id"]
                        && record["snapshot_id"] == request["snapshot_id"]
                        && record["world_file"] == request["world_file"]
                        && record["changed"].is_boolean(),
                    "{} recovery applied the wrong target",
                    shard.name
                );
            } else {
                ensure!(
                    record["event"] == "recovery_catalog"
                        && record["session_id"].is_string()
                        && record["snapshots"].is_array()
                        && record["has_more"].is_boolean(),
                    "{} recovery catalog is invalid",
                    shard.name
                );
            }
            records.insert(shard.name.clone(), record.clone());
        }
        Ok(json!({"passed": true, "worlds_loaded": false, "shards": records}))
    })
    .await
    .unwrap_or_else(|_| {
        Err(anyhow::anyhow!(
            "recovery exceeded its {timeout}s total deadline"
        ))
    });
    let mut cleanup_errors = Vec::new();
    for shard in &room.shards {
        if let Err(error) = shard.process.kill().await {
            cleanup_errors.push(format!("{}: {error}", shard.name));
        }
    }
    let report = result.with_context(|| {
        format!(
            "native recovery probe failed; cleanup errors: {cleanup_errors:?}\n{}",
            room.diagnostics()
        )
    })?;
    ensure!(
        cleanup_errors.is_empty(),
        "probe cleanup failed: {cleanup_errors:?}"
    );
    Ok(report)
}

/// Write or inspect one synthetic player using the game's own session APIs.
/// This proves saved-player handling; it does not represent a connected client.
pub async fn player_fixture(
    cluster: &Path,
    executable: &Path,
    timeout: u64,
    health: Option<u32>,
) -> Result<Value> {
    ensure!(
        timeout > 0 && health.is_none_or(|health| (1..=150).contains(&health)),
        "invalid timeout or Wilson health"
    );
    let cluster = disposable(cluster)?;
    let executable = executable
        .canonicalize()
        .context("resolve game executable")?;
    let mut room = Room {
        shards: Vec::new(),
        cursor: 0,
        recovering: false,
        player: Some(health.map_or(PlayerProbe::Read, PlayerProbe::Write)),
    };
    let result = timeout_at(deadline(timeout)?, async {
        room.launch(&cluster, &executable, None)?;
        room.exercise().await
    })
    .await
    .unwrap_or_else(|_| {
        Err(anyhow::anyhow!(
            "player probe exceeded its {timeout}s total deadline"
        ))
    });
    let mut cleanup_errors = Vec::new();
    for shard in &room.shards {
        if let Err(error) = shard.process.kill().await {
            cleanup_errors.push(format!("{}: {error}", shard.name));
        }
    }
    let report = result.with_context(|| {
        format!(
            "native player probe failed; cleanup errors: {cleanup_errors:?}\n{}",
            room.diagnostics()
        )
    })?;
    ensure!(
        cleanup_errors.is_empty(),
        "probe cleanup failed: {cleanup_errors:?}"
    );
    Ok(report)
}

fn disposable(path: &Path) -> Result<Cluster> {
    let cluster = configuration::discover(path)?;
    ensure!(
        cluster.directory.parent().is_some(),
        "cannot probe a filesystem root"
    );
    let marker = fs::symlink_metadata(cluster.directory.join(".dst-rust-probe"))
        .context("probe requires a .dst-rust-probe marker in a disposable room")?;
    ensure!(
        marker.file_type().is_file(),
        "probe marker must be a regular file"
    );
    Ok(cluster)
}

fn recovery_requests(
    cluster: &Cluster,
    targets: Option<&BTreeMap<String, Value>>,
) -> Result<BTreeMap<String, Value>> {
    let mut requests = BTreeMap::new();
    if let Some(targets) = targets {
        let expected: BTreeSet<_> = cluster
            .shards
            .iter()
            .map(|shard| shard.name.as_str())
            .collect();
        ensure!(
            expected == targets.keys().map(String::as_str).collect(),
            "recovery targets must contain exactly the configured shards"
        );
        for (name, target) in targets {
            let object = target
                .as_object()
                .context("recovery target must be an object")?;
            ensure!(
                object.keys().all(|key| matches!(
                    key.as_str(),
                    "mode" | "session_id" | "snapshot_id" | "world_file"
                )),
                "unknown recovery target field"
            );
            ensure!(
                object.get("mode").is_none_or(|value| value == "apply"),
                "recovery target mode must be apply"
            );
            for (key, limit) in [("session_id", 128), ("world_file", 4096)] {
                ensure!(
                    target[key].as_str().is_some_and(|value| !value.is_empty()
                        && value.len() <= limit
                        && !value.contains(['\0', '\r', '\n'])),
                    "invalid recovery {key}"
                );
            }
            ensure!(
                target["snapshot_id"]
                    .as_u64()
                    .is_some_and(|id| (1..=9_007_199_254_740_991).contains(&id)),
                "recovery snapshot_id must be a positive Lua-safe integer"
            );
            let mut target = target.clone();
            target["mode"] = json!("apply");
            requests.insert(name.clone(), target);
        }
    } else {
        for shard in &cluster.shards {
            requests.insert(shard.name.clone(), json!({"mode": "catalog"}));
        }
    }
    Ok(requests)
}

impl Room {
    fn launch(
        &mut self,
        cluster: &Cluster,
        executable: &Path,
        recovery: Option<&BTreeMap<String, Value>>,
    ) -> Result<()> {
        for shard in &cluster.shards {
            let nonce = Ulid::new().to_string();
            let options_path = cluster
                .directory
                .join(&shard.name)
                .join("dst_server_driver.json");
            let mut options = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(options_path)
                .context("write disposable driver options")?;
            let mut configuration = json!({"nonce": nonce,
                "profile": "history", "actions": ["ATTACK", "MIGRATE"],
                "control": {"observe_saves": true}});
            if let Some(requests) = recovery {
                configuration["control"]["recovery"] = requests[&shard.name].clone();
            }
            serde_json::to_writer(&mut options, &configuration)?;
            options.write_all(b"\n")?;
            options.sync_all()?;
            let args: Vec<OsString> = vec![
                "-persistent_storage_root".into(),
                cluster.directory.parent().unwrap().into(),
                "-conf_dir".into(),
                ".".into(),
                "-cluster".into(),
                cluster.directory.file_name().unwrap().into(),
                "-shard".into(),
                shard.name.clone().into(),
                "-ugc_directory".into(),
                cluster.directory.join("mods/ugc").into(),
                "-monitor_parent_process".into(),
                std::process::id().to_string().into(),
                "-skip_update_server_mods".into(),
                "-offline".into(),
                "-cloudserver".into(),
            ];
            let process = GameProcess::spawn(ProcessSpec {
                program: executable.to_owned(),
                args,
                cwd: executable.parent().unwrap().to_owned(),
            })
            .with_context(|| format!("spawn {}", shard.name))?;
            self.shards.push(Shard {
                name: shard.name.clone(),
                process,
                nonce,
                generation: None,
                ready: false,
                native_ready: false,
                ended: false,
                pending: None,
                accepted: false,
                native_done: false,
                busy: false,
                result: None,
                saves: VecDeque::new(),
                tail: VecDeque::new(),
            });
            eprintln!(
                "probe: spawned {} pid={}",
                shard.name,
                self.shards.last().unwrap().process.pid()
            );
        }
        Ok(())
    }

    async fn exercise(&mut self) -> Result<Value> {
        while self
            .shards
            .iter()
            .any(|shard| !shard.ready || !shard.native_ready)
        {
            self.pump(false).await?;
        }
        eprintln!("probe: all {} worlds ready", self.shards.len());
        let mut runtimes = Vec::new();
        for index in 0..self.shards.len() {
            let runtime = self.rpc(index, "runtime", json!({})).await?;
            ensure!(
                runtime["is_master_shard"] == (index == 0),
                "incorrect master identity"
            );
            ensure!(
                runtime["session_id"]
                    .as_str()
                    .is_some_and(|id| !id.is_empty()),
                "missing native session identity"
            );
            runtimes.push(runtime);
        }
        let ids: BTreeSet<_> = runtimes
            .iter()
            .map(|r| r["shard_id"].as_str().context("missing native shard ID"))
            .collect::<Result<_>>()?;
        ensure!(
            ids.len() == self.shards.len(),
            "native shard IDs are not unique"
        );
        let connected = loop {
            let connected = self
                .rpc(
                    0,
                    "connected_shards",
                    json!({"current_name": self.shards[0].name}),
                )
                .await?;
            let records = connected
                .as_array()
                .context("connected_shards did not return an array")?;
            let actual: BTreeSet<_> = records
                .iter()
                .filter(|r| r["ready"] == true)
                .filter_map(|r| r["id"].as_str())
                .collect();
            if actual == ids && records.len() == ids.len() {
                break connected;
            }
            let delay = tokio::time::sleep(Duration::from_millis(250));
            tokio::pin!(delay);
            loop {
                tokio::select! {
                    _ = &mut delay => break,
                    event = self.pump(false) => event?,
                }
            }
        };
        let player = if let Some(mode) = self.player {
            self.player_step(mode).await?
        } else {
            Value::Null
        };
        // Refresh after shard synchronization; startup can advance slave snapshots.
        for (index, runtime) in runtimes.iter_mut().enumerate() {
            *runtime = self.rpc(index, "runtime", json!({})).await?;
        }
        let target = runtimes[0]["snapshot"]
            .as_u64()
            .context("invalid native snapshot")?;
        self.rpc(0, "pause", json!({"paused": false})).await?;
        ensure!(
            self.rpc(0, "save", json!({})).await? == true,
            "save was not accepted"
        );
        eprintln!("probe: waiting for coordinated snapshot {target}");
        let completed = loop {
            let matches: Vec<_> = self
                .shards
                .iter()
                .zip(&runtimes)
                .map(|(shard, runtime)| {
                    shard
                        .saves
                        .iter()
                        .find(|save| {
                            save["event"] == "save_complete"
                                && save["snapshot_id"] == target
                                && save["session_id"] == runtime["session_id"]
                                && save["shutdown"] == false
                        })
                        .cloned()
                })
                .collect();
            if matches.iter().all(Option::is_some) {
                break matches.into_iter().map(Option::unwrap).collect::<Vec<_>>();
            }
            self.pump(false).await?;
        };
        let mut records = Vec::new();
        for index in 0..self.shards.len() {
            let runtime = self.rpc(index, "runtime", json!({})).await?;
            ensure!(
                runtime["session_id"] == runtimes[index]["session_id"]
                    && runtime["snapshot"].as_u64() == target.checked_add(1),
                "{} did not advance to the coordinated snapshot",
                self.shards[index].name
            );
            let catalog = self
                .rpc(index, "list_snapshots", json!({"limit": 10}))
                .await?;
            ensure!(
                catalog["session_id"] == runtime["session_id"]
                    && catalog["snapshots"]
                        .as_array()
                        .is_some_and(|snapshots| snapshots
                            .iter()
                            .any(|snapshot| snapshot["snapshot_id"] == target)),
                "{} has no native catalog entry for completed snapshot",
                self.shards[index].name
            );
            records.push(
                json!({"name": self.shards[index].name, "pid": self.shards[index].process.pid(),
                "generation": self.shards[index].generation, "runtime": runtime,
                "save_completion": completed[index], "catalog": catalog}),
            );
        }
        // Every shard owns a process; request native saving before waiting on any exit.
        for shard in &self.shards {
            shard.process.send(b"c_shutdown()\n").await?;
        }
        eprintln!("probe: graceful shutdown requested for all worlds");
        while self.shards.iter().any(|shard| !shard.ended) {
            self.pump(true).await?;
        }
        for (index, shard) in self.shards.iter().enumerate() {
            let exit = shard.process.wait().await?;
            ensure!(
                exit.status.success()
                    && !exit.forced
                    && exit.output_drained
                    && exit.protocol_error.is_none(),
                "{} did not exit cleanly: {exit:?}",
                shard.name
            );
            records[index]["exit"] = json!({"code": exit.returncode(), "forced": exit.forced,
                "output_drained": exit.output_drained});
        }
        Ok(
            json!({"connected_shards": connected, "saved_snapshot": target, "shards": records,
            "synthetic_player": player}),
        )
    }

    async fn player_step(&mut self, mode: PlayerProbe) -> Result<Value> {
        const USER: &str = "KU_1234567_";
        if let PlayerProbe::Write(health) = mode {
            let source = format!(
                "local p=SpawnPrefab('wilson');p.userid='{USER}';p.Physics:Teleport(4,0,5);p.components.health:SetCurrentHealth({health});p.components.hunger:SetCurrent(71);local item=SpawnPrefab('goldnugget');item.components.stackable:SetStackSize(7);p.components.inventory:GiveItem(item);SerializeUserSession(p,true);return true"
            );
            ensure!(
                self.rpc(0, "execute_json", json!({"source": source}))
                    .await?
                    == true,
                "synthetic player serialization failed"
            );
        }
        loop {
            let source = format!(
                "DST_RUST_PLAYER=nil;local f=TheNet:GetUserSessionFile(TheWorld.meta.session_identifier,'{USER}');if f==nil then return false end;TheNet:DeserializeUserSession(f,function(ok,s) if not ok or s==nil then DST_RUST_PLAYER={{error='unreadable'}};return end;local d,p=ParseUserSessionData(s);if d==nil or p=='' then DST_RUST_PLAYER={{error='invalid'}};return end;DST_RUST_PLAYER={{file=f,prefab=p,health=d.data.health.health,x=d.x,z=d.z,encoded=TheNet:GetDefaultEncodeUserPath()}} end);return true"
            );
            if self
                .rpc(0, "execute_json", json!({"source": source}))
                .await?
                == true
            {
                break;
            }
            self.idle(Duration::from_millis(100)).await?;
        }
        loop {
            let value = self
                .rpc(
                    0,
                    "execute_json",
                    json!({"source": "return DST_RUST_PLAYER"}),
                )
                .await?;
            if !value.is_null() {
                ensure!(
                    value.get("error").is_none()
                        && value["prefab"] == "wilson"
                        && value["health"].is_number()
                        && value["file"].is_string(),
                    "native player session could not be read: {value}"
                );
                if let PlayerProbe::Write(health) = mode {
                    ensure!(
                        value["health"] == health,
                        "serialized player health differs"
                    );
                }
                return Ok(value);
            }
            self.idle(Duration::from_millis(100)).await?;
        }
    }

    async fn idle(&mut self, duration: Duration) -> Result<()> {
        let delay = tokio::time::sleep(duration);
        tokio::pin!(delay);
        loop {
            tokio::select! {
                _ = &mut delay => return Ok(()),
                event = self.pump(false) => event?,
            }
        }
    }

    async fn rpc(&mut self, index: usize, method: &str, arguments: Value) -> Result<Value> {
        loop {
            let outcome = self.rpc_once(index, method, &arguments).await?;
            if let Some(result) = outcome {
                return Ok(result);
            }
            eprintln!(
                "probe: {} native Lua busy before acceptance; retrying {method}",
                self.shards[index].name
            );
            self.idle(Duration::from_millis(100)).await?;
        }
    }

    async fn rpc_once(
        &mut self,
        index: usize,
        method: &str,
        arguments: &Value,
    ) -> Result<Option<Value>> {
        let shard = &mut self.shards[index];
        let id = Ulid::new().to_string();
        let frame = format!(
            "DST_RPC|{}\n",
            json!({"v": 1, "nonce": shard.nonce,
            "id": id, "generation": shard.generation, "method": method, "arguments": arguments})
        )
        .into_bytes();
        ensure!(frame.len() <= 4096, "probe RPC exceeds atomic write limit");
        shard.pending = Some(id);
        shard.accepted = false;
        shard.native_done = false;
        shard.busy = false;
        shard.result = None;
        eprintln!("probe: {} {method} requested", shard.name);
        shard.process.send(&frame).await?;
        loop {
            if self.shards[index].busy {
                self.shards[index].pending = None;
                return Ok(None);
            }
            if self.shards[index].native_done {
                let result = self.shards[index]
                    .result
                    .take()
                    .context("native command completed without a structured result")?;
                self.shards[index].pending = None;
                ensure!(
                    result["ok"] == true,
                    "{} {method} failed: {result}",
                    self.shards[index].name
                );
                eprintln!("probe: {} {method} completed", self.shards[index].name);
                return Ok(Some(result["data"].clone()));
            }
            self.pump(false).await?;
        }
    }

    async fn pump(&mut self, allow_exit: bool) -> Result<()> {
        let (index, event) = poll_fn(|cx| {
            for offset in 0..self.shards.len() {
                let index = (self.cursor + offset) % self.shards.len();
                if self.shards[index].ended {
                    continue;
                }
                let next = self.shards[index].process.next_event();
                tokio::pin!(next);
                if let Poll::Ready(event) = next.poll(cx) {
                    return Poll::Ready((index, event));
                }
            }
            Poll::Pending
        })
        .await;
        self.cursor = (index + 1) % self.shards.len();
        let shard = &mut self.shards[index];
        let Some(event) = event else {
            shard.ended = true;
            ensure!(
                allow_exit,
                "{} exited before the probe completed",
                shard.name
            );
            return Ok(());
        };
        let line = match event.kind {
            EventKind::Line(line) => line,
            EventKind::Oversized => {
                bail!("{} {:?} exceeded the line limit", shard.name, event.stream)
            }
            EventKind::ReadError(error) => bail!("{} {:?}: {error}", shard.name, event.stream),
        };
        let text = String::from_utf8_lossy(&line);
        if shard.tail.len() == 16 {
            shard.tail.pop_front();
        }
        shard.tail.push_back(format!(
            "{:?}: {}",
            event.stream,
            text.chars().take(1024).collect::<String>()
        ));
        let text = std::str::from_utf8(native_message(&line));
        let Ok(text) = text else {
            return Ok(());
        };
        if matches!(event.stream, StreamKind::Lifecycle) {
            if text.starts_with("DST_Master_Ready")
                || text
                    .strip_prefix("DST_SessionId|")
                    .is_some_and(|session| !session.trim().is_empty())
            {
                shard.native_ready = true;
                eprintln!("probe: {} native ready: {}", shard.name, text.trim_end());
            } else if !text.starts_with("DST_Stats|") {
                eprintln!(
                    "probe: {} native lifecycle: {}",
                    shard.name,
                    text.trim_end()
                );
            }
        }
        if matches!(event.stream, StreamKind::Reply) {
            match text.trim_end_matches(['\r', '\n']) {
                "DST_LuaBusy" => {
                    ensure!(
                        shard.pending.is_some() && !shard.accepted && shard.result.is_none(),
                        "{} native busy cannot be attributed to an unaccepted command",
                        shard.name
                    );
                    shard.busy = true;
                    return Ok(());
                }
                "DST_RemoteCommandDone" => {
                    if shard.pending.is_some() {
                        shard.native_done = true;
                    }
                    return Ok(());
                }
                _ => {}
            }
        }
        if self.recovering && matches!(event.stream, StreamKind::Lifecycle) {
            ensure!(
                !text.starts_with("DST_Master_Ready") && !text.starts_with("DST_Saved"),
                "{} native world loaded or saved during recovery",
                shard.name
            );
        }
        let (kind, payload) = if matches!(event.stream, StreamKind::Reply) {
            if let Some(payload) = text.strip_prefix("DST_RPC|") {
                ("rpc", Some(payload))
            } else {
                ("control", text.strip_prefix("DST_CONTROL|"))
            }
        } else if matches!(event.stream, StreamKind::Stdout) {
            if let Some(payload) = text.strip_prefix("DST_DRIVER|") {
                ("driver", Some(payload))
            } else {
                ("control", text.strip_prefix("DST_CONTROL|"))
            }
        } else {
            return Ok(());
        };
        let Some(payload) = payload else {
            return Ok(());
        };
        let record: Value =
            serde_json::from_str(payload).context("malformed mandatory driver frame")?;
        ensure!(
            record["nonce"] == shard.nonce,
            "{} driver nonce mismatch",
            shard.name
        );
        if kind == "driver" {
            ensure!(
                record.get("error").is_none(),
                "{} bootstrap failed: {record}",
                shard.name
            );
            let generation = record.get("health").unwrap_or(&record)["generation"]
                .as_u64()
                .context("invalid bootstrap generation")?;
            ensure!(
                shard
                    .generation
                    .is_none_or(|previous| previous == generation),
                "{} unexpectedly changed generation",
                shard.name
            );
            shard.generation = Some(generation);
            if record.get("health").is_some() {
                ensure!(
                    !self.recovering,
                    "{} driver became ready during recovery",
                    shard.name
                );
                shard.ready = true;
                eprintln!("probe: {} driver ready generation={generation}", shard.name);
            }
        } else {
            if shard.generation.is_none()
                && (self.recovering
                    || (kind == "control"
                        && matches!(
                            record["event"].as_str(),
                            Some("world_load_started" | "world_load_decoded" | "world_load_failed")
                        )))
            {
                // Native load observation precedes the gameplay driver's ready announcement.
                shard.generation = record["generation"].as_u64();
            }
            ensure!(
                record["v"] == 1
                    && shard.generation.is_some()
                    && record["generation"].as_u64() == shard.generation,
                "{} mandatory frame protocol/generation mismatch",
                shard.name
            );
            if kind == "rpc" {
                ensure!(
                    record["id"].as_str() == shard.pending.as_deref(),
                    "unexpected RPC response"
                );
                if let Some(result) = record.get("result") {
                    shard.result = Some(result.clone());
                } else {
                    ensure!(record["accepted"] == true, "invalid RPC acceptance frame");
                    shard.accepted = true;
                }
            } else {
                ensure!(
                    !self.recovering
                        || matches!(
                            record["event"].as_str(),
                            Some("recovery_catalog" | "recovery_applied" | "recovery_failed")
                        ),
                    "{} saved or published unexpected control during recovery: {record}",
                    shard.name
                );
                ensure!(
                    !matches!(
                        record["event"].as_str(),
                        Some("save_failed" | "save_unconfirmed" | "recovery_failed")
                    ),
                    "{} native save could not be confirmed: {record}",
                    shard.name
                );
                ensure!(
                    shard.saves.len() < 128,
                    "{} exceeded probe control evidence limit",
                    shard.name
                );
                eprintln!("probe: {} control {}", shard.name, record);
                shard.saves.push_back(record);
            }
        }
        Ok(())
    }

    fn diagnostics(&self) -> String {
        self.shards
            .iter()
            .map(|shard| {
                format!(
                    "{} recent output:\n{}",
                    shard.name,
                    shard.tail.iter().cloned().collect::<Vec<_>>().join("")
                )
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn probe_inputs_are_validated_for_the_entire_room_before_launch() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("cluster.ini"),
            "[SHARD]\nshard_enabled=false\n",
        )
        .unwrap();
        fs::create_dir(dir.path().join("Surface")).unwrap();
        fs::write(
            dir.path().join("Surface/server.ini"),
            "[NETWORK]\nserver_port=11000\n",
        )
        .unwrap();
        let cluster = configuration::discover(dir.path()).unwrap();
        assert!(disposable(dir.path()).is_err());
        fs::write(dir.path().join(".dst-rust-probe"), "disposable").unwrap();
        assert!(disposable(dir.path()).is_ok());
        for result in [
            run(dir.path(), Path::new("/bin/true"), u64::MAX, 1).await,
            recovery(dir.path(), Path::new("/bin/true"), u64::MAX, None).await,
            player_fixture(dir.path(), Path::new("/bin/true"), u64::MAX, None).await,
        ] {
            assert!(result.unwrap_err().to_string().contains("timeout"));
        }
        let mut targets = BTreeMap::from([(
            "Surface".to_owned(),
            json!({"session_id": "session", "snapshot_id": 2, "world_file": "session/0000000002"}),
        )]);
        assert!(recovery_requests(&cluster, Some(&targets)).is_ok());
        targets.get_mut("Surface").unwrap()["snapshot_id"] = json!(0);
        assert!(recovery_requests(&cluster, Some(&targets)).is_err());
        targets.get_mut("Surface").unwrap()["snapshot_id"] = json!(2);
        targets.insert("Unexpected".to_owned(), targets["Surface"].clone());
        assert!(recovery_requests(&cluster, Some(&targets)).is_err());
    }
}
