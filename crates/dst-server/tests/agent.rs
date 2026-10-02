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
    rpc::Client,
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
        let mods = control["policy"]["mod_auto_update"] == true;
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("cluster");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("cluster.ini"), "[SHARD]\nshard_enabled=true\ncluster_key=agent-fixture\nmaster_port=19000\nmaster_ip=127.0.0.1\n").unwrap();
        for (name, id, port) in [("Master", 1, 19100), ("Caves", 2, 19200)] {
            fs::create_dir(root.join(name)).unwrap();
            fs::write(root.join(name).join("server.ini"), format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n", id == 1, port + 1)).unwrap();
        }
        fs::write(
            root.join(".dst-control.json"),
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
        let child = Self::launch(directory.path(), &root);
        Self {
            directory,
            root,
            child,
        }
    }

    fn launch(directory: &Path, root: &Path) -> Child {
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
            .env("DST_SERVER_CLUSTER_NAME", "dst-fixture")
            .env("OTEL_LOGS_EXPORTER", "none")
            .env("OTEL_METRICS_EXPORTER", "none")
            .env("OTEL_TRACES_EXPORTER", "none")
            .stdin(Stdio::null())
            .stdout(log.try_clone().unwrap())
            .stderr(log)
            .spawn()
            .unwrap()
    }

    fn relaunch(&mut self) {
        assert!(self.child.try_wait().unwrap().is_some());
        self.child = Self::launch(self.directory.path(), &self.root);
    }

    async fn client(&mut self) -> Client {
        timeout(Duration::from_secs(40), async {
            loop {
                assert!(
                    self.child.try_wait().unwrap().is_none(),
                    "Agent exited: {}",
                    self.log()
                );
                if let Ok(client) = Client::connect(self.root.join(".dst-agent.sock")).await {
                    let status = call(&client, Request::Status {}).await;
                    if status["agent"]["ready"] == true && status["phase"] == "running" {
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

    fn control(&self) -> Value {
        serde_json::from_slice(&fs::read(self.root.join(".dst-control.json")).unwrap()).unwrap()
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
            assert!(!self.root.join(name).join("dst_server_driver.json").exists());
            assert!(!self.root.join(name).join("native-lifecycle").exists());
        }
    }
    async fn wait(&mut self, code: i32) {
        let status = timeout(Duration::from_secs(8), async {
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
async fn closed_start_exits_and_releases_the_room_without_starting_games() {
    let mut agent = Agent::spawn(true, false);
    agent.wait(0).await;
    agent.assert_no_games();
    assert!(!agent.root.join(".dst-agent.sock").exists());
    RoomLock::try_acquire(&agent.root).unwrap();

    let mut control = agent.control();
    control["agent_run"] = json!({"active":true,"failure":{"kind":"retryable"}});
    fs::write(
        agent.root.join(".dst-control.json"),
        serde_json::to_vec(&control).unwrap(),
    )
    .unwrap();
    agent.relaunch();
    agent.wait(0).await;
    agent.assert_no_games();
    assert_eq!(agent.control()["agent_run"], control["agent_run"]);
}

#[tokio::test]
async fn exhausted_recovery_exits_and_preserves_the_offline_reason() {
    let recovery = RecoveryState {
        closed: Some(ClosedReason::CurrentRetriesExhausted),
        restarts_used: 2,
        ..Default::default()
    };
    let mut agent = Agent::with_control(json!({
        "policy":{"mod_auto_update":false},
        "recovery":recovery,
    }));
    agent.wait(0).await;
    agent.assert_no_games();
    assert_eq!(agent.control()["recovery"], json!(recovery));
    assert!(!agent.root.join(".dst-agent.sock").exists());
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
                assert_eq!(status["operations"]["last"]["result"]["status"], "success");
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
                assert_eq!(status["operations"]["last"]["result"]["status"], "success");
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
    let records: Vec<Value> = agent
        .log()
        .lines()
        .filter_map(|line| line.strip_prefix("DST_RECORD|"))
        .map(|record| serde_json::from_str(record).unwrap())
        .collect();
    assert!(!records.is_empty());
    for record in records {
        assert_eq!(record["attributes"]["dst.cluster.name"], "dst-fixture");
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
    agent.wait(75).await;
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
    agent.wait(75).await;
    agent.assert_native_lifecycle("start\n");
    assert!(agent.control()["agent_run"]["failure"]["kind"].is_string());
}

#[tokio::test]
async fn restart_replies_then_exits_without_spawning_replacement_games() {
    let mut agent = Agent::spawn(false, false);
    let client = agent.client().await;
    let restarted = call(&client, Request::Restart { notice: None }).await;
    assert_eq!(restarted["restart"], true);
    agent.wait(76).await;
    agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
    assert_eq!(agent.control()["agent_run"]["active"], false);
    assert!(agent.control()["agent_run"]["failure"].is_null());
    client.close().await.unwrap();

    agent.relaunch();
    let client = agent.client().await;
    call(&client, Request::Stop { notice: None }).await;
    agent.wait(0).await;
    agent.assert_native_lifecycle(
        "start\nshutdown\nshutdown_save\nstart\nshutdown\nshutdown_save\n",
    );
    client.close().await.unwrap();
}

#[tokio::test]
async fn termination_uses_native_shutdown_and_preserves_abnormal_exit_as_failure() {
    for abnormal in [false, true] {
        let mut agent = Agent::spawn(false, false);
        let client = agent.client().await;
        if abnormal {
            fs::write(agent.root.join("Caves/shutdown-mode"), "signal").unwrap();
        }
        assert_eq!(
            unsafe { libc::kill(agent.child.id() as i32, libc::SIGTERM) },
            0
        );
        agent.wait(if abnormal { 75 } else { 0 }).await;
        agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
        assert_eq!(agent.control()["agent_run"]["active"], false);
        assert_eq!(
            agent.control()["agent_run"]["failure"].is_object(),
            abnormal
        );
        assert!(!agent.root.join(".dst-agent.sock").exists());
        client.close().await.unwrap();
    }
}

#[tokio::test]
async fn manual_mod_updates_run_in_a_new_process_and_honor_reopening() {
    for restart in [false, true] {
        let mut agent = Agent::spawn(false, true);
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
        agent.wait(76).await;
        agent.assert_native_lifecycle("start\nshutdown\nshutdown_save\n");
        assert_eq!(
            fs::read_to_string(agent.root.join("mods/prepared")).unwrap(),
            "download\n"
        );
        client.close().await.unwrap();

        agent.relaunch();
        if restart {
            let client = agent.client().await;
            call(&client, Request::Stop { notice: None }).await;
            client.close().await.unwrap();
        }
        agent.wait(0).await;
        assert_eq!(
            fs::read_to_string(agent.root.join("mods/prepared")).unwrap(),
            "download\ndownload\n"
        );
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
