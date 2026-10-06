//! Exercise the actual binary, Unix socket, durable policy and child cleanup.
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

use chrono::{Timelike, Utc};
use dst_server::{
    files::RoomLock,
    model::{Envelope, ErrorCode, Request, Target},
    recovery::{ClosedReason, RecoveryState},
    rpc::{Client, StreamKind},
};
use serde_json::{Value, json};
use tokio::time::{sleep, timeout};

struct Agent {
    directory: tempfile::TempDir,
    root: PathBuf,
    child: Child,
}

impl Agent {
    fn spawn(closed: bool, mods: bool) -> Self {
        let mut control = json!({"policy":{"mod_auto_update":mods,"timezone":"UTC"}});
        if closed {
            let now = Utc::now();
            let minute = now.hour() * 60 + now.minute();
            let start = (minute + 180) % 1440;
            let end = (start + 60) % 1440;
            control["policy"]["schedule"] = json!([{"start":format!("{:02}:{:02}", start / 60, start % 60),"end":format!("{:02}:{:02}", end / 60, end % 60)}]);
        }
        Self::with_control(control)
    }

    fn with_control(control: Value) -> Self {
        Self::with_environment(control, &[])
    }

    fn with_environment(control: Value, environment: &[(&str, &str)]) -> Self {
        let mods = control["policy"]["mod_auto_update"] == true;
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("001");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("cluster.ini"), "[SHARD]\nshard_enabled=true\ncluster_key=agent-fixture\nmaster_port=19000\nmaster_ip=127.0.0.1\n").unwrap();
        for (name, id, port) in [("Master", 1, 19100), ("Caves", 2, 19200)] {
            fs::create_dir(root.join(name)).unwrap();
            fs::write(root.join(name).join("server.ini"), format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n", id == 1, port + 1)).unwrap();
        }
        fs::write(
            root.join(".dst-server.json"),
            serde_json::to_vec(&control).unwrap(),
        )
        .unwrap();
        let game = directory.path().join("install/bin64/game.py");
        fs::create_dir_all(game.parent().unwrap()).unwrap();
        let mut source = String::new();
        if mods {
            fs::create_dir(root.join("mods")).unwrap();
            fs::write(
                root.join("mods/dedicated_server_mods_setup.lua"),
                "ServerModSetup(\"123\")\n",
            )
            .unwrap();
            std::os::unix::fs::symlink(root.join("mods"), directory.path().join("install/mods"))
                .unwrap();
            source.push_str(
                r#"#!/usr/bin/env python3
import pathlib, sys
prepared = pathlib.Path(__file__).resolve().parents[1] / 'mods/prepared'
if '-only_update_server_mods' in sys.argv:
    with prepared.open('a') as output:
        output.write('download\n')
    print('FinishDownloadingServerMods Complete! Process trying to quit nicely..', flush=True)
    raise SystemExit(0)
if not prepared.exists():
    raise SystemExit(23)
"#,
            );
        }
        source.push_str(include_str!("fixtures/game.py"));
        fs::write(&game, source).unwrap();
        fs::set_permissions(&game, fs::Permissions::from_mode(0o700)).unwrap();
        let child = Self::launch(directory.path(), &root, environment);
        Self {
            directory,
            root,
            child,
        }
    }

    fn launch(directory: &Path, root: &Path, environment: &[(&str, &str)]) -> Child {
        let log = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(directory.join("agent.log"))
            .unwrap();
        Command::new(env!("CARGO_BIN_EXE_dst-server"))
            .args(["agent", "--cluster"])
            .arg(root)
            .arg("--executable")
            .arg(directory.join("install/bin64/game.py"))
            .env("PODMAN_SYSTEMD_UNIT", "dst-fixture.service")
            .env_remove("DST_SERVER_TELEMETRY_PROFILE")
            .env_remove("OTEL_RESOURCE_ATTRIBUTES")
            .env("OTEL_LOGS_EXPORTER", "none")
            .env("OTEL_METRICS_EXPORTER", "none")
            .env("OTEL_TRACES_EXPORTER", "none")
            .envs(environment.iter().copied())
            .stdin(Stdio::null())
            .stdout(log.try_clone().unwrap())
            .stderr(log)
            .spawn()
            .unwrap()
    }

    fn relaunch(&mut self) {
        assert!(self.child.try_wait().unwrap().is_some());
        self.child = Self::launch(self.directory.path(), &self.root, &[]);
    }

    async fn client(&mut self) -> Client {
        self.client_in_phase("running").await
    }

    async fn client_in_phase(&mut self, phase: &str) -> Client {
        timeout(Duration::from_secs(40), async {
            loop {
                assert!(
                    self.child.try_wait().unwrap().is_none(),
                    "Agent exited: {}",
                    self.log()
                );
                if let Ok(client) = Client::connect(self.root.join(".dst-agent.sock")).await {
                    let status = call(&client, Request::Status {}).await;
                    if status["agent"]["ready"] == true && status["phase"] == phase {
                        return client;
                    }
                    client.close().await.unwrap();
                }
                sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("Agent startup deadline")
    }

    fn log(&self) -> String {
        fs::read_to_string(self.directory.path().join("agent.log")).unwrap()
    }

    fn records(&self) -> Vec<Value> {
        self.log()
            .lines()
            .filter_map(|line| line.strip_prefix("DST_RECORD|"))
            .map(|record| serde_json::from_str(record).unwrap())
            .collect()
    }

    fn control(&self) -> Value {
        serde_json::from_slice(&fs::read(self.root.join(".dst-server.json")).unwrap()).unwrap()
    }

    fn assert_native_lifecycle(&self, expected: &str) {
        for name in ["Master", "Caves"] {
            assert_eq!(
                fs::read_to_string(self.root.join(name).join("native-lifecycle")).unwrap(),
                expected,
                "{name}"
            );
        }
    }

    fn assert_no_games(&self) {
        for name in ["Master", "Caves"] {
            let shard = self.root.join(name);
            assert!(!shard.join("dst_server_driver.json").exists());
            assert!(!shard.join("native-lifecycle").exists());
        }
    }
    async fn wait(&mut self, code: i32) {
        self.wait_for(code, Duration::from_secs(8)).await;
    }

    async fn wait_for(&mut self, code: i32, duration: Duration) {
        let status = timeout(duration, async {
            loop {
                if let Some(status) = self.child.try_wait().unwrap() {
                    return status;
                }
                sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("Agent did not exit: {}", self.log()));
        assert_eq!(status.code(), Some(code), "{}", self.log());
    }
}

impl Drop for Agent {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

async fn call(client: &Client, request: Request) -> Value {
    client
        .call(Envelope::new(Target::Room, request).unwrap())
        .await
        .unwrap()
}

#[tokio::test]
async fn expired_and_disconnected_queries_do_not_reach_a_busy_shard() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let master = agent.root.join("Master");
    let shard = |request| Envelope::new(Target::Shard("Master".into()), request).unwrap();
    fs::write(master.join("hold-world"), "").unwrap();
    fs::write(master.join("hold-list_players"), "").unwrap();
    fs::write(agent.root.join("vote-response.json"), "true").unwrap();
    let mut active = Box::pin(client.call(shard(Request::World {})));
    assert!(futures::poll!(&mut active).is_pending());
    timeout(Duration::from_secs(3), async {
        while !master.join("world-waiting").exists() {
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();

    let remote = agent.client().await;
    let mut abandoned =
        Box::pin(remote.call(Envelope::new(Target::Room, Request::GetVote {}).unwrap()));
    assert!(futures::poll!(&mut abandoned).is_pending());
    call(&remote, Request::Status {}).await;
    remote.close().await.unwrap();
    assert_eq!(abandoned.await.unwrap_err().code, ErrorCode::Transport);

    let mut list = shard(Request::ListPlayers {});
    list.timeout = Some(0.1);
    assert_eq!(
        client.call(list).await.unwrap_err().code,
        ErrorCode::Timeout
    );
    fs::remove_file(master.join("hold-world")).unwrap();
    active.await.unwrap();
    let runtime = timeout(
        Duration::from_secs(3),
        client.call(shard(Request::Runtime {})),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(runtime["session_id"], "S_Master");
    assert!(!master.join("list_players-waiting").exists());
    assert!(!agent.root.join("Master.vote-call").exists());
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
}

#[tokio::test]
async fn cli_reports_failing_shards_and_lua_errors_with_their_output() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let run = |args: &[&str], code| {
        let output = Command::new(env!("CARGO_BIN_EXE_dst-server"))
            .args(args)
            .args(["--json", "--socket"])
            .arg(agent.root.join(".dst-agent.sock"))
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(code), "{output:?}");
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let all = run(&["call", "list_players"], 0);
    assert_eq!(all[0]["result"]["ok"], true);
    fs::write(
        agent.root.join("Caves/list_players-response.json"),
        r#"{"ok":false,"error":"lua_error"}"#,
    )
    .unwrap();
    let all = run(&["call", "list_players"], 1);
    let error = &all[0]["result"]["error"];
    assert_eq!(error["code"], "partial_failure");
    let shards = error["details"]["shards"].as_array().unwrap();
    assert_eq!(shards.len(), 2);
    assert!(
        shards
            .iter()
            .any(|row| row["shard"] == "Caves" && row["result"]["status"] == "failure")
    );
    assert!(
        shards
            .iter()
            .any(|row| row["shard"] == "Master" && row["result"]["status"] == "success")
    );

    let source = "print('before'); error('example')";
    let file = agent.directory.path().join("script.lua");
    fs::write(&file, "return +").unwrap();
    for (flag, argument, kind, printed) in [
        ("--source", source, "runtime", "before"),
        ("--file", file.to_str().unwrap(), "compile", ""),
    ] {
        let evaluated = json!({"output":printed,"error":{"kind":kind,"message":"example"},"values":[],"truncated":false});
        fs::write(
            agent.root.join("Caves/evaluate-response.json"),
            json!({"ok":true,"data":evaluated}).to_string(),
        )
        .unwrap();
        let response = run(&["console", "--shard", "Caves", flag, argument], 1);
        assert_eq!(response["error"]["code"], "lua");
        assert_eq!(response["error"]["details"], evaluated);
    }
    let result = run(&["call", "execute_all", r#"{"source":"1"}"#], 1);
    let error = &result[0]["result"]["error"];
    assert_eq!(error["code"], "partial_failure");
    let shards = error["details"]["shards"].as_array().unwrap();
    assert!(
        shards
            .iter()
            .any(|row| row["shard"] == "Caves" && row["result"]["error"]["code"] == "lua")
    );
    assert!(
        shards
            .iter()
            .any(|row| row["shard"] == "Master" && row["result"]["value"] == "captured")
    );
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
}

async fn online_host() -> (Agent, Client, dst_server::host::Host) {
    use dst_server::{
        deployment::{ContainerUnit, DeploymentOptions, PortMapping},
        host::Host,
        settings::ClusterConfig,
    };
    let mut agent = Agent::spawn(true, false);
    let client = agent.client_in_phase("stopped").await;
    let configuration = call(&client, Request::ReadConfiguration {}).await;
    let units = agent.directory.path().join("units");
    fs::create_dir(&units).unwrap();
    let options = DeploymentOptions {
        ports: [19100, 19101, 19200, 19201]
            .into_iter()
            .map(|port| PortMapping {
                host: port,
                container: port,
            })
            .collect(),
        ..Default::default()
    };
    ContainerUnit::for_cluster(
        "dst-001",
        &agent.root,
        &ClusterConfig::from_value(configuration["cluster"].clone()).unwrap(),
        options,
    )
    .unwrap()
    .save(&units)
    .unwrap();
    fs::write(agent.directory.path().join("service-state"), "active").unwrap();
    let mut host = Host::new(agent.directory.path(), &units);
    let systemctl = agent.directory.path().join("systemctl");
    fs::write(&systemctl, include_str!("fixtures/edit_systemctl.py")).unwrap();
    fs::set_permissions(&systemctl, fs::Permissions::from_mode(0o700)).unwrap();
    host.systemd.executable = systemctl;
    (agent, client, host)
}

#[tokio::test]
async fn online_host_field_edits_preserve_concurrent_changes() {
    let (mut agent, client, host) = online_host().await;
    let first = [("/cluster/settings/cluster_name".into(), json!("renamed"))];
    let second = [("/cluster/settings/max_players".into(), json!(12))];
    let results = tokio::join!(
        host.edit_fields(1, &first, &[]),
        host.edit_fields(1, &second, &[])
    );
    for (result, changes) in [(results.0, first), (results.1, second)] {
        if let Err(error) = result {
            assert_eq!(
                error
                    .downcast_ref::<dst_server::model::Error>()
                    .unwrap()
                    .code,
                ErrorCode::Busy
            );
            host.edit_fields(1, &changes, &[]).await.unwrap();
        }
    }
    let saved = host.load(1).await.unwrap();
    assert_eq!(
        saved.get("/cluster/settings/cluster_name").unwrap(),
        "renamed"
    );
    assert_eq!(saved.get("/cluster/settings/max_players").unwrap(), 12);
    agent.assert_no_games();
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn online_edit_distinguishes_preflight_from_failed_readback() {
    let (mut agent, client, host) = online_host().await;
    let observed = host.wait_ready(1, Duration::from_secs(2)).await.unwrap();
    assert_eq!(observed["game"]["phase"], "stopped");
    assert_eq!(observed["desired"], false);
    let original = call(&client, Request::ReadConfiguration {}).await;
    let window = original["policy"]["schedule"][0].clone();
    let append = [("/policy/schedule/-".into(), window)];
    let unit = host.quadlet_dir.join("dst-001.container");
    let contents = fs::read(&unit).unwrap();
    fs::remove_file(&unit).unwrap();
    assert!(host.edit_fields(1, &append, &[]).await.is_err());
    assert_eq!(call(&client, Request::ReadConfiguration {}).await, original);
    fs::write(&unit, contents).unwrap();

    // The Agent can still edit policy when the native configuration is unreadable.
    let ini = agent.root.join("cluster.ini");
    let contents = fs::read(&ini).unwrap();
    fs::write(&ini, "[invalid-section]\nsecret=private\n").unwrap();
    let error = host.edit_fields(1, &append, &[]).await.unwrap_err();
    let error = error.downcast_ref::<dst_server::model::Error>().unwrap();
    assert_eq!(error.code, ErrorCode::PartialFailure);
    assert_eq!(
        error.details,
        json!({"applied":true,"number":1,"stage":"readback"})
    );
    assert!(!error.message.contains("private"));
    fs::write(ini, contents).unwrap();
    assert_eq!(host.load(1).await.unwrap().policy.schedule.len(), 2);
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn large_command_replies_stay_out_of_status_and_lifecycle_records() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let stream = client.subscribe(StreamKind::Lifecycle).await.unwrap();
    let payload = "x".repeat(256 * 1024);
    fs::write(
        agent.root.join("Master/execute_json-response.json"),
        json!({"ok":true, "data":payload}).to_string(),
    )
    .unwrap();
    let request = Envelope::new(
        Target::Shard("Master".into()),
        Request::ExecuteJson {
            source: "return 'x'".into(),
        },
    )
    .unwrap();
    assert_eq!(client.call(request).await.unwrap(), payload);
    let status = call(&client, Request::Status {}).await;
    let last = &status["operations"]["last"];
    assert_eq!(last["method"], "execute_json");
    assert!(last["completed_at_ns"].is_u64());
    assert!(last["error"].is_null());
    assert!(serde_json::to_vec(&status).unwrap().len() < 16 * 1024);
    timeout(Duration::from_secs(3), async {
        loop {
            let batch = stream.next(128).await.unwrap();
            assert_eq!(batch.dropped, 0);
            if batch.records.iter().any(|record| {
                let last = &record["status"]["operations"]["last"];
                last["method"] == "execute_json" && last["completed_at_ns"].is_u64()
            }) {
                assert!(
                    batch
                        .records
                        .iter()
                        .all(|record| serde_json::to_vec(record).unwrap().len() < 16 * 1024)
                );
                break;
            }
        }
    })
    .await
    .unwrap();
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn closed_agent_rejects_version_fifos_without_blocking_startup() {
    for linked in [false, true] {
        let mut agent = Agent::spawn(true, false);
        let client = agent.client_in_phase("stopped").await;
        agent.assert_no_games();
        call(&client, Request::Stop { notice: None }).await;
        agent.wait(0).await;
        client.close().await.unwrap();

        let version = agent.directory.path().join("install/version.txt");
        let fifo = if linked {
            version.with_file_name("version-source")
        } else {
            version.clone()
        };
        let fifo_c = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(fifo_c.as_ptr(), 0o600) }, 0);
        if linked {
            std::os::unix::fs::symlink(&fifo, &version).unwrap();
        }
        fs::write(agent.directory.path().join("agent.log"), "").unwrap();
        agent.child = Agent::launch(
            agent.directory.path(),
            &agent.root,
            &[("OTEL_SDK_DISABLED", "true")],
        );
        let client = match timeout(Duration::from_secs(5), agent.client_in_phase("stopped")).await {
            Ok(client) => client,
            Err(_) => {
                let pid = agent.child.id() as i32;
                drop(agent);
                let probe = unsafe { libc::kill(pid, 0) };
                let error = std::io::Error::last_os_error();
                assert_eq!((probe, error.raw_os_error()), (-1, Some(libc::ESRCH)));
                panic!(
                    "Agent startup blocked by a writerless version FIFO (linked={linked}); child {pid} was killed and reaped"
                );
            }
        };
        let status = call(&client, Request::Status {}).await;
        assert_eq!(status["agent"]["ready"], true);
        assert_eq!(status["phase"], "stopped");
        agent.assert_no_games();
        call(&client, Request::Stop { notice: None }).await;
        agent.wait(0).await;
        client.close().await.unwrap();
        agent.assert_no_games();
        let mut operations = agent.records();
        operations.retain(|record| record["event_name"] == "dst.server.operation");
        assert!(!operations.is_empty());
        assert!(
            operations
                .iter()
                .all(|record| record["resource"]["dst.game.version"] == "unknown")
        );
    }
}

#[tokio::test]
async fn failed_telemetry_drain_preserves_requested_exit() {
    let collector = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}", collector.local_addr().unwrap());
    for (request, code) in [(Request::Stop { notice: None }, 0), (Request::Kill {}, 1)] {
        let mut agent = Agent::with_environment(
            json!({"policy":{"mod_auto_update":false}}),
            &[
                ("OTEL_SDK_DISABLED", "false"),
                ("OTEL_METRICS_EXPORTER", "otlp"),
                ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", &endpoint),
                ("OTEL_EXPORTER_OTLP_METRICS_TIMEOUT", "100"),
                ("OTEL_METRIC_EXPORT_INTERVAL", "60000"),
            ],
        );
        let client = agent.client().await;
        let result = client
            .call(Envelope::new(Target::Room, request).unwrap())
            .await;
        if code == 0 {
            result.unwrap();
        }
        agent.wait(code).await;
        client.close().await.unwrap();
        agent.assert_native_lifecycle(if code == 0 {
            "start\nshutdown\nshutdown_save\n"
        } else {
            "start\n"
        });
        assert_eq!(agent.control()["agent_run"]["active"], false);
        assert_eq!(agent.control()["agent_run"]["failure"].is_null(), code == 0);
        let log = agent.log();
        assert!(log.contains("telemetry shutdown did not complete"), "{log}");
        assert!(log.contains("\"observability_error\":\""), "{log}");
    }
}

#[tokio::test]
async fn initial_mod_download_finishes_before_automatic_game_start() {
    let mut agent = Agent::spawn(false, true);
    let client = agent.client().await;
    let prepared = agent.root.join("mods/prepared");
    assert_eq!(fs::read_to_string(&prepared).unwrap(), "download\n");
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
    agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
}

#[tokio::test]
async fn closed_room_with_pending_automatic_mods_accepts_stop_and_manual_update() {
    let mut agent = Agent::spawn(true, true);
    let client = agent.client_in_phase("stopped").await;
    let automatic = agent.control()["policy_state"]["mods"].clone();
    assert_eq!(automatic["phase"]["state"], "pending");
    assert_eq!(automatic["origin"]["source"], "mod_auto");
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
    assert_eq!(agent.control()["policy_state"]["mods"], automatic);
    assert!(!agent.root.join("mods/prepared").exists());

    agent.relaunch();
    let client = agent.client_in_phase("stopped").await;
    let queued = call(
        &client,
        Request::UpdateMods {
            restart: false,
            notice: None,
        },
    )
    .await;
    assert_eq!(queued, json!({"queued":true,"restart":false}));
    agent.wait(0).await;
    client.close().await.unwrap();
    assert_eq!(
        fs::read_to_string(agent.root.join("mods/prepared")).unwrap(),
        "download\n"
    );
    agent.assert_no_games();
}

#[tokio::test]
async fn closed_start_keeps_the_agent_and_defers_recovery_without_starting_games() {
    let mut agent = Agent::spawn(true, false);
    let client = agent.client_in_phase("stopped").await;
    agent.assert_no_games();
    assert!(agent.root.join(".dst-agent.sock").exists());
    assert!(RoomLock::try_acquire(&agent.root).is_err());
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();

    let mut control = agent.control();
    control["agent_run"] = json!({"active":true,"attempt_id":"interrupted","failure":null});
    fs::write(
        agent.root.join(".dst-server.json"),
        serde_json::to_vec(&control).unwrap(),
    )
    .unwrap();
    agent.relaunch();
    let client = agent.client_in_phase("stopped").await;
    agent.assert_no_games();
    assert_eq!(agent.control()["agent_run"], control["agent_run"]);
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn policy_changes_open_and_close_games_repeatedly_in_the_same_agent() {
    let mut agent = Agent::spawn(true, false);
    let client = agent.client_in_phase("stopped").await;
    let mut host =
        dst_server::host::Host::new(agent.directory.path(), agent.directory.path().join("units"));
    host.systemd.executable = agent.directory.path().join("systemctl");
    fs::write(&host.systemd.executable, "#!/bin/sh\nprintf 'Id=dst-001.service\\nLoadState=loaded\\nActiveState=active\\nSubState=running\\nJob=0\\nResult=success\\n'\n").unwrap();
    fs::set_permissions(&host.systemd.executable, fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        host.wait_ready(1, Duration::from_secs(2)).await.unwrap()["desired"],
        false
    );
    assert_eq!(
        host.call(
            1,
            Request::Ban {
                userid: "KU_offline".into(),
                seconds: None
            }
        )
        .await
        .unwrap(),
        serde_json::Value::Null
    );
    let closed = agent.control()["policy"].clone();
    let open = json!({"timezone":"UTC", "mod_auto_update":false});
    for _ in 0..2 {
        call(
            &client,
            Request::SetPolicy {
                policy: open.clone(),
            },
        )
        .await;
        let running = agent.client().await;
        assert_eq!(
            host.wait_ready(1, Duration::from_secs(2)).await.unwrap()["desired"],
            true
        );
        let pids: Vec<_> = call(&running, Request::Status {}).await["shards"]
            .as_array()
            .unwrap()
            .iter()
            .map(|shard| shard["pid"].as_i64().unwrap())
            .collect();
        running.close().await.unwrap();
        call(
            &client,
            Request::SetPolicy {
                policy: closed.clone(),
            },
        )
        .await;
        let stopped = agent.client_in_phase("stopped").await;
        stopped.close().await.unwrap();
        assert!(agent.child.try_wait().unwrap().is_none());
        assert_eq!(agent.control()["agent_run"]["active"], false);
        assert!(RoomLock::try_acquire(&agent.root).is_err());
        for pid in pids {
            assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
        }
    }
    agent.assert_native_lifecycle(
        "start\nshutdown\nshutdown_save\nstart\nshutdown\nshutdown_save\n",
    );
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn wall_clock_opens_and_closes_games_without_an_external_trigger() {
    let now = Utc::now();
    let minute = now.hour() * 60 + now.minute();
    let start = (minute + if now.second() < 55 { 1 } else { 2 }) % 1440;
    let end = (start + 1) % 1440;
    let mut agent = Agent::with_control(json!({
        "policy": {"timezone":"UTC", "mod_auto_update":false,
            "schedule":[{"start":format!("{:02}:{:02}",start/60,start%60), "end":format!("{:02}:{:02}",end/60,end%60)}]}
    }));
    let client = agent.client_in_phase("stopped").await;
    agent.assert_no_games();
    timeout(Duration::from_secs(180), async {
        for phase in ["running", "stopped"] {
            while call(&client, Request::Status {}).await["phase"] != phase {
                assert!(agent.child.try_wait().unwrap().is_none(), "{}", agent.log());
                sleep(Duration::from_millis(100)).await;
            }
        }
    })
    .await
    .expect("the resident Agent must cross both daily boundaries");
    let status = call(&client, Request::Status {}).await;
    for shard in status["shards"].as_array().unwrap() {
        assert_eq!(shard["readiness"]["world_loaded"], false);
    }
    assert!(agent.child.try_wait().unwrap().is_none());
    agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn closed_agent_reports_invalid_mod_setup_and_accepts_its_repair() {
    let mut agent = Agent::spawn(true, false);
    let client = agent.client_in_phase("stopped").await;
    let mut configuration = call(&client, Request::ReadConfiguration {}).await["cluster"].take();
    configuration["downloads"] = json!({});
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();

    let mut control = agent.control();
    control["policy"]["mod_auto_update"] = true.into();
    fs::write(
        agent.root.join(".dst-server.json"),
        serde_json::to_vec(&control).unwrap(),
    )
    .unwrap();
    fs::create_dir_all(agent.root.join("mods")).unwrap();
    fs::write(
        agent.root.join("mods/dedicated_server_mods_setup.lua"),
        "ServerModSetup(",
    )
    .unwrap();
    agent.child = Agent::launch(
        agent.directory.path(),
        &agent.root,
        &[(
            "DST_SERVER_PUBLISHED_PORTS",
            r#"{"Master":19100,"Caves":19200}"#,
        )],
    );
    let client = agent.client_in_phase("stopped").await;
    let maintenance = &call(&client, Request::Status {}).await["agent"]["maintenance"];
    assert_eq!(maintenance["phase"]["state"], "pending");
    assert_eq!(maintenance["attempts"], 0);
    assert!(
        maintenance["error"]
            .as_str()
            .is_some_and(|error| !error.is_empty())
    );
    agent.assert_no_games();

    call(
        &client,
        Request::Configure {
            configuration,
            replace_permissions: false,
        },
    )
    .await;
    let repaired = &call(&client, Request::Status {}).await["agent"]["maintenance"];
    assert_eq!(repaired["phase"]["state"], "idle");
    assert!(repaired["error"].is_null());
    call(
        &client,
        Request::SetPolicy {
            policy: json!({"timezone":"UTC","mod_auto_update":true}),
        },
    )
    .await;
    let running = agent.client().await;
    call(&running, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    running.close().await.unwrap();
    client.close().await.unwrap();
}

#[tokio::test]
async fn exhausted_recovery_stays_available_and_explicit_start_resets_the_budget() {
    let recovery = RecoveryState {
        closed: Some(ClosedReason::CurrentRetriesExhausted),
        restarts_used: 2,
        ..Default::default()
    };
    let mut agent = Agent::with_control(json!({
        "policy":{"mod_auto_update":false},
        "recovery":recovery,
    }));
    let client = agent.client_in_phase("failed").await;
    agent.assert_no_games();
    assert_eq!(agent.control()["recovery"], json!(recovery));
    assert!(agent.root.join(".dst-agent.sock").exists());
    call(&client, Request::Start {}).await;
    client.close().await.unwrap();
    let client = agent.client().await;
    assert!(agent.control()["recovery"]["closed"].is_null());
    assert_eq!(agent.control()["recovery"]["restarts_used"], 0);
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    client.close().await.unwrap();
}

#[tokio::test]
async fn accepted_work_survives_disconnect_and_stop_replies_before_exit() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let running = call(&client, Request::Status {}).await;
    let pids: Vec<_> = running["shards"]
        .as_array()
        .unwrap()
        .iter()
        .map(|shard| shard["pid"].as_i64().unwrap())
        .collect();
    let copy = client.clone();
    let operation = tokio::spawn(async move {
        copy.call(
            Envelope::new(
                Target::Shard("Master".into()),
                Request::Execute {
                    source: "slow".into(),
                },
            )
            .unwrap(),
        )
        .await
    });
    timeout(Duration::from_secs(3), async {
        while !agent.root.join("executions").exists() {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    operation.abort();
    client.close().await.unwrap();
    let client = agent.client().await;
    timeout(Duration::from_secs(3), async {
        loop {
            let status = call(&client, Request::Status {}).await;
            if status["operations"]["current"].is_null() {
                assert!(status["operations"]["last"]["completed_at_ns"].is_u64());
                assert!(status["operations"]["last"]["error"].is_null());
                break;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        fs::read_to_string(agent.root.join("executions")).unwrap(),
        "1"
    );
    let mut short_wait = Envelope::new(
        Target::Shard("Master".into()),
        Request::Execute {
            source: "slow".into(),
        },
    )
    .unwrap();
    short_wait.timeout = Some(0.05);
    assert_eq!(
        client.call(short_wait).await.unwrap_err().code,
        ErrorCode::Unknown
    );
    let pending = call(&client, Request::Status {}).await;
    assert_eq!(pending["operations"]["current"]["method"], "execute");
    timeout(Duration::from_secs(3), async {
        loop {
            let status = call(&client, Request::Status {}).await;
            if status["operations"]["current"].is_null() {
                assert!(status["operations"]["last"]["completed_at_ns"].is_u64());
                assert!(status["operations"]["last"]["error"].is_null());
                break;
            }
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        fs::read_to_string(agent.root.join("executions")).unwrap(),
        "2"
    );
    let stopped = call(&client, Request::Stop { notice: None }).await;
    assert_eq!(stopped["shards"].as_array().unwrap().len(), 2);
    for shard in stopped["shards"].as_array().unwrap() {
        assert_eq!(shard["result"]["status"], "success");
        let stopped = &shard["result"]["value"];
        assert_eq!(stopped["returncode"], 0);
        assert_eq!(stopped["forced"], false);
        assert_eq!(stopped["output_drained"], true);
    }
    agent.wait(0).await;
    let control = agent.control();
    assert_eq!(control["agent_run"]["active"], false);
    for pid in pids {
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
    }
    client.close().await.unwrap();
    agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
    assert!(!agent.root.join(".dst-agent.sock").exists());
    let records = agent.records();
    assert!(!records.is_empty());
    for record in records {
        assert_eq!(record["attributes"]["dst.cluster.name"], "dst-fixture");
        assert_eq!(record["resource"]["dst.cluster.name"], "dst-fixture");
    }
}

#[tokio::test]
async fn unexpected_exit_persists_failure_before_requesting_container_rebuild() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let error = client
        .call(
            Envelope::new(
                Target::Shard("Master".into()),
                Request::Execute {
                    source: "crash".into(),
                },
            )
            .unwrap(),
        )
        .await
        .unwrap_err();
    assert!(matches!(
        error.code,
        ErrorCode::Unknown | ErrorCode::Transport
    ));
    agent.wait(1).await;
    let control = agent.control();
    assert_eq!(control["agent_run"]["active"], false);
    assert!(control["agent_run"]["failure"]["kind"].is_string());
    assert!(!agent.root.join(".dst-agent.sock").exists());
}

#[tokio::test]
async fn kill_interrupts_a_restart_countdown_and_requests_failure_recovery() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let copy = client.clone();
    let restart = tokio::spawn(async move {
        copy.call(
            Envelope::new(
                Target::Room,
                Request::Restart {
                    notice: Some(
                        serde_json::from_value(json!({
                            "template":"restart in {remaining}","delay":60,"interval":30
                        }))
                        .unwrap(),
                    ),
                },
            )
            .unwrap(),
        )
        .await
    });
    timeout(Duration::from_secs(3), async {
        while call(&client, Request::Status {}).await["operations"]["current"]["method"]
            != "restart"
        {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let _ = client
        .call(Envelope::new(Target::Room, Request::Kill {}).unwrap())
        .await;
    let failure = restart.await.unwrap().unwrap_err();
    assert_eq!(failure.code, ErrorCode::Unknown);
    agent.wait(1).await;
    agent.assert_native_lifecycle("start\n");
    assert!(agent.control()["agent_run"]["failure"]["kind"].is_string());
}

#[tokio::test]
async fn agent_opens_and_records_locally_with_a_missing_exporter_certificate() {
    let mut agent = Agent::with_environment(
        json!({"policy":{"mod_auto_update":false}}),
        &[
            ("OTEL_LOGS_EXPORTER", "otlp"),
            ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "https://localhost:4317"),
            (
                "OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE",
                "/fixture-private/missing.pem",
            ),
            ("OTEL_RESOURCE_ATTRIBUTES", "dst.cluster.name=local-room"),
        ],
    );
    let client = agent.client().await;
    call(&client, Request::Save {}).await;
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    let log = agent.log();
    assert_eq!(log.matches("OTLP LOGS disabled").count(), 1);
    assert!(!log.contains("fixture-private"));
    assert!(
        agent
            .records()
            .iter()
            .any(|r| r["resource"]["dst.cluster.name"] == "local-room")
    );
}

#[tokio::test]
async fn restart_replaces_games_in_the_same_agent() {
    let mut agent = Agent::with_environment(
        json!({"policy":{"mod_auto_update":false,"timezone":"UTC"}}),
        &[("DST_SERVER_TELEMETRY_PROFILE", "off")],
    );
    let client = agent.client().await;
    let restarted = call(&client, Request::Restart { notice: None }).await;
    assert_eq!(restarted["restart"], true);
    assert!(agent.child.try_wait().unwrap().is_none());
    agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\nstart\n");
    assert_eq!(agent.control()["agent_run"]["active"], true);
    assert!(agent.control()["agent_run"]["failure"].is_null());
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    agent.assert_native_lifecycle(
        "start\nshutdown\nshutdown_save\nstart\nshutdown\nshutdown_save\n",
    );
    let records = agent.records();
    for shard in ["Master", "Caves"] {
        let coverage: Vec<_> = records
            .iter()
            .filter(|record| {
                record["event_name"] == "dst.telemetry.coverage"
                    && record["attributes"]["dst.shard.name"] == shard
            })
            .collect();
        assert_eq!(coverage.len(), 2, "{shard} records coverage for each start");
        for record in &coverage {
            assert_eq!(record["attributes"]["dst.runtime.generation"], 1);
            assert_eq!(record["body"]["profile"], "off");
            assert_eq!(record["body"]["requested_profile"], "off");
            assert_eq!(record["body"]["telemetry_status"], "disabled");
        }
        let attempt = "dst.game.attempt.id";
        assert_ne!(
            coverage[0]["attributes"][attempt],
            coverage[1]["attributes"][attempt],
        );
    }
    client.close().await.unwrap();
}

#[tokio::test]
async fn termination_announces_for_sixty_seconds_before_native_shutdown() {
    let now = Utc::now();
    let minute = now.hour() * 60 + now.minute();
    let start = (minute + 1439) % 1440;
    let end = (minute + 480) % 1440;
    let mut agent = Agent::with_control(
        json!({"policy":{"mod_auto_update":false,"timezone":"UTC","schedule":[{"start":format!("{:02}:{:02}",start/60,start%60),"end":format!("{:02}:{:02}",end/60,end%60)}]}}),
    );
    let client = agent.client().await;
    assert_eq!(
        unsafe { libc::kill(agent.child.id() as i32, libc::SIGTERM) },
        0
    );
    timeout(Duration::from_secs(4), async {
        while !agent.root.join("announcements").exists() {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        client
            .call(Envelope::new(Target::Room, Request::Start {}).unwrap())
            .await
            .unwrap_err()
            .code,
        ErrorCode::NotReady
    );
    agent.wait_for(0, Duration::from_secs(75)).await;
    let notices: Vec<Value> = fs::read_to_string(agent.root.join("announcements"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(notices.len(), 3);
    let first = notices[0]["time"].as_f64().unwrap();
    for name in ["Master", "Caves"] {
        let events: Vec<Value> =
            fs::read_to_string(agent.root.join(name).join("native-times.jsonl"))
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
        let shutdown = events
            .iter()
            .find(|event| event["event"] == "shutdown")
            .unwrap()["time"]
            .as_f64()
            .unwrap();
        assert!(
            (60.0..70.0).contains(&(shutdown - first)),
            "{name}: {}",
            shutdown - first
        );
    }
    agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
    assert_eq!(agent.control()["agent_run"]["active"], false);
    let records = agent.records();
    assert!(
        records
            .iter()
            .any(|record| record["body"]["method"] == "agent_shutdown"
                && record["body"]["phase"] == "finished"
                && record["body"]["error_code"].is_null()
                && record["attributes"]["dst.operation.source"] == "signal")
    );
    assert!(
        !records
            .iter()
            .any(|record| record["body"]["stage"] == "failure_finalized"
                && record["severity_text"] == "ERROR")
    );
    client.close().await.unwrap();
}

#[tokio::test]
async fn later_signals_end_the_notice_and_preserve_abnormal_exit_as_failure() {
    for abnormal in [false, true] {
        let mut agent = Agent::spawn(false, false);
        let client = agent.client().await;
        if abnormal {
            fs::write(agent.root.join("Caves/shutdown-mode"), "signal").unwrap();
        }
        assert_eq!(
            unsafe { libc::kill(agent.child.id() as i32, libc::SIGINT) },
            0
        );
        timeout(Duration::from_secs(4), async {
            while !agent.root.join("announcements").exists() {
                sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        for signal in [libc::SIGTERM, libc::SIGINT, libc::SIGTERM] {
            assert_eq!(unsafe { libc::kill(agent.child.id() as i32, signal) }, 0);
            sleep(Duration::from_millis(10)).await;
        }
        agent.wait(if abnormal { 1 } else { 0 }).await;
        agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
        assert_eq!(agent.control()["agent_run"]["active"], false);
        assert_eq!(
            agent.control()["agent_run"]["failure"].is_object(),
            abnormal
        );
        assert!(!agent.root.join(".dst-agent.sock").exists());
        assert_eq!(
            fs::read_to_string(agent.root.join("announcements"))
                .unwrap()
                .lines()
                .count(),
            1
        );
        let records = agent.records();
        let final_event = records
            .iter()
            .find(|record| {
                record["body"]["method"] == "agent_shutdown"
                    && record["body"]["phase"] == "finished"
            })
            .unwrap();
        assert_eq!(final_event["body"]["error_code"].is_null(), !abnormal);
        assert_eq!(
            records
                .iter()
                .filter(|record| record["body"]["reason"] == "subsequent_signal")
                .count(),
            1
        );
        client.close().await.unwrap();
    }
}

#[tokio::test]
async fn manual_mod_updates_reuse_the_agent_and_honor_reopening() {
    for (restart, mods) in [(false, true), (true, true), (false, false)] {
        let mut agent = Agent::spawn(false, mods);
        let client = agent.client().await;
        let result = call(
            &client,
            Request::UpdateMods {
                restart,
                notice: None,
            },
        )
        .await;
        assert_eq!(result, json!({"queued":true,"restart":restart}));
        client.close().await.unwrap();
        if restart {
            let client = agent.client().await;
            call(&client, Request::Stop { notice: None }).await;
            client.close().await.unwrap();
        }
        agent.wait(0).await;
        let prepared = agent.root.join("mods/prepared");
        let (outcome, reason) = if mods {
            assert_eq!(
                fs::read_to_string(prepared).unwrap(),
                "download\ndownload\n"
            );
            ("download_completed", "completed")
        } else {
            assert!(!prepared.exists());
            ("download_skipped", "skipped")
        };
        let mut records = agent.records();
        records.retain(|record| record["attributes"]["dst.operation.source"] == "rpc");
        for layer in ["room", "policy"] {
            assert!(records.iter().any(|record| {
                record["event_name"] == "dst.server.operation"
                    && record["attributes"]["dst.operation.layer"] == layer
                    && record["body"]["method"] == "update_mods"
                    && record["body"]["outcome"] == outcome
            }));
        }
        assert!(records.iter().any(|record| {
            record["event_name"] == "dst.mod.maintenance" && record["body"]["reason"] == reason
        }));
        agent.assert_native_lifecycle(if restart {
            "start\nshutdown\nshutdown_save\nstart\nshutdown\nshutdown_save\n"
        } else {
            "start\nshutdown\nshutdown_save\n"
        });
        assert_eq!(
            agent.control()["policy_state"]["mods"]["phase"]["state"],
            "idle"
        );
    }
}
