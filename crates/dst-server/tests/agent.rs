//! Exercise the actual binary, Unix socket, durable policy and child cleanup.
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Duration,
};

use chrono::{Timelike, Utc};
use dst_server::{
    model::{Envelope, ErrorCode, Request, Target},
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
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("cluster");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("cluster.ini"), "[SHARD]\nshard_enabled=true\ncluster_key=agent-fixture\nmaster_port=19000\nmaster_ip=127.0.0.1\n").unwrap();
        for (name, id, port) in [("Master", 1, 19100), ("Caves", 2, 19200)] {
            fs::create_dir(root.join(name)).unwrap();
            fs::write(root.join(name).join("server.ini"), format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n", id == 1, port + 1)).unwrap();
        }
        let mut control = json!({"policy":{"mod_auto_update":mods,"timezone":"UTC"}});
        if closed {
            let now = Utc::now();
            let minute = now.hour() * 60 + now.minute();
            let start = (minute + 180) % 1440;
            let end = (start + 60) % 1440;
            control["policy"]["schedule"] = json!([{"start":format!("{:02}:{:02}", start / 60, start % 60),"end":format!("{:02}:{:02}", end / 60, end % 60)}]);
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
        let log = fs::File::create(directory.path().join("agent.log")).unwrap();
        let child = Command::new(env!("CARGO_BIN_EXE_dst-server"))
            .args(["agent", "--cluster"])
            .arg(&root)
            .arg("--executable")
            .arg(game)
            .env("DST_SERVER_CLUSTER_NAME", "dst-fixture")
            .env("OTEL_LOGS_EXPORTER", "none")
            .env("OTEL_METRICS_EXPORTER", "none")
            .env("OTEL_TRACES_EXPORTER", "none")
            .stdin(Stdio::null())
            .stdout(log.try_clone().unwrap())
            .stderr(log)
            .spawn()
            .unwrap();
        Self {
            directory,
            root,
            child,
        }
    }

    async fn client(&mut self) -> Client {
        timeout(Duration::from_secs(8), async {
            loop {
                assert!(
                    self.child.try_wait().unwrap().is_none(),
                    "Agent exited: {}",
                    self.log()
                );
                if let Ok(client) = Client::connect(self.root.join(".dst-agent.sock")).await {
                    if call(&client, Request::Status {}).await["agent"]["ready"] == true {
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
    timeout(Duration::from_secs(40), async {
        loop {
            let status = call(&client, Request::Status {}).await;
            assert!(
                !status["agent"]["requires_container_restart"]
                    .as_bool()
                    .unwrap(),
                "{}",
                agent.log()
            );
            if status["phase"] == "running" {
                break;
            }
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("download and automatic startup deadline");
    let prepared = agent.root.join("mods/prepared");
    assert_eq!(fs::read_to_string(&prepared).unwrap(), "download\n");
    call(&client, Request::Stop { notice: None }).await;
    call(&client, Request::Start {}).await;
    assert_eq!(fs::read_to_string(&prepared).unwrap(), "download\n");
    call(&client, Request::Stop { notice: None }).await;
    client.close().await.unwrap();
    assert_eq!(
        unsafe { libc::kill(agent.child.id() as i32, libc::SIGTERM) },
        0
    );
    agent.wait(0).await;
}

#[tokio::test]
async fn closed_agent_serves_queries_and_accepted_work_survives_disconnect() {
    let mut agent = Agent::spawn(true, false);
    let client = agent.client().await;
    let status = call(&client, Request::Status {}).await;
    assert_eq!(status["phase"], "stopped");
    assert_eq!(status["agent"]["schedule"]["open"], false);
    call(&client, Request::Start {}).await;
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
    call(&client, Request::Save {}).await;
    call(&client, Request::Stop { notice: None }).await;
    let control: Value =
        serde_json::from_slice(&fs::read(agent.root.join(".dst-control.json")).unwrap()).unwrap();
    assert_eq!(control["agent_run"]["active"], false);
    for pid in pids {
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
    }
    client.close().await.unwrap();
    assert_eq!(
        unsafe { libc::kill(agent.child.id() as i32, libc::SIGTERM) },
        0
    );
    agent.wait(0).await;
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
    let mut agent = Agent::spawn(true, false);
    let client = agent.client().await;
    call(&client, Request::Start {}).await;
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
    let control: Value =
        serde_json::from_slice(&fs::read(agent.root.join(".dst-control.json")).unwrap()).unwrap();
    assert_eq!(control["agent_run"]["active"], false);
    assert!(control["agent_run"]["failure"]["kind"].is_string());
    assert!(!agent.root.join(".dst-agent.sock").exists());
}

#[tokio::test]
async fn kill_cancels_a_reserved_start_before_its_retry_delay_elapses() {
    let mut agent = Agent::spawn(true, false);
    let client = agent.client().await;
    let path = agent.root.join(".dst-control.json");
    let mut control: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    // Inject a durable retry only after boot; the native catalog path must never
    // run because Kill interrupts this accepted start during its 30-second wait.
    let latest = ["Master", "Caves"].into_iter().map(|name| {
        (name.to_owned(), json!({"session_id":format!("S_{name}"),"snapshot_id":5,"world_file":format!("save/session/S_{name}/0000000005"),"clock":{"cycles":2,"segs":{"day":16,"dusk":0,"night":0},"phase":"day","totaltimeinphase":480.0,"remainingtimeinphase":240.0}}))
    }).collect::<serde_json::Map<_,_>>();
    control["recovery"] = json!({"version":1,"restarts_used":1,"last_open_window_ms":null,"closed":null,"latest":latest,"retry":{"not_before_ms":0,"started":false},"target":null});
    fs::write(&path, serde_json::to_vec(&control).unwrap()).unwrap();
    let copy = client.clone();
    let start = tokio::spawn(async move {
        copy.call(Envelope::new(Target::Room, Request::Start {}).unwrap())
            .await
    });
    timeout(Duration::from_secs(3), async {
        while call(&client, Request::Status {}).await["operations"]["current"]["method"] != "start"
        {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let _ = client
        .call(Envelope::new(Target::Room, Request::Kill {}).unwrap())
        .await;
    let failure = start.await.unwrap().unwrap_err();
    assert_eq!(failure.code, ErrorCode::Unknown);
    agent.wait(75).await;
    for name in ["Master", "Caves"] {
        assert!(
            !agent
                .root
                .join(name)
                .join("dst_server_driver.json")
                .exists()
        );
    }
    let control: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(control["recovery"]["retry"]["started"], false);
    assert_eq!(control["recovery"]["restarts_used"], 1);
}
