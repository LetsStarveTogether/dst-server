//! Host lifecycle and configuration access through one room Agent and one service.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::process::Command;
use tokio::time::{Instant, sleep, timeout, timeout_at};

use crate::{
    deployment::{ContainerUnit, PortAllocation, validate_real_directory},
    files::{self, PermissionFiles, RoomLock},
    logs::{JournalLogs, JournalQuery, JournalResult, JournalStream},
    model::{Envelope, Request, Target},
    policy::Policy,
    rooms::{Room, RoomStore},
    rpc::Client,
    settings::ClusterConfig,
};

pub const DEFAULT_LIFECYCLE_TIMEOUT: Duration = Duration::from_secs(900);
pub const AGENT_SOCKET: &str = ".dst-agent.sock";

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct UnitStatus {
    pub name: String,
    pub load: String,
    pub active: String,
    pub sub: String,
    pub job_id: u32,
    pub result: String,
}
impl UnitStatus {
    pub fn running(&self) -> bool {
        self.job_id != 0 || !matches!(self.active.as_str(), "inactive" | "failed")
    }
}

#[derive(Clone, Debug)]
pub struct Systemd {
    pub executable: PathBuf,
    pub user: bool,
    pub command_timeout: Duration,
}
impl Default for Systemd {
    fn default() -> Self {
        Self {
            executable: "systemctl".into(),
            user: false,
            command_timeout: Duration::from_secs(30),
        }
    }
}
impl Systemd {
    async fn command(&self, arguments: &[&str]) -> Result<std::process::Output> {
        ensure!(
            !self.command_timeout.is_zero(),
            "systemd command timeout must be positive"
        );
        let mut command = Command::new(&self.executable);
        command
            .kill_on_drop(true)
            .stdin(Stdio::null())
            .env("LC_ALL", "C")
            .arg("--no-pager");
        if self.user {
            command.arg("--user");
        }
        let output = timeout(self.command_timeout, command.args(arguments).output())
            .await
            .context("systemd command timed out")??;
        ensure!(
            output.stdout.len() + output.stderr.len() <= 1024 * 1024,
            "systemd output exceeded limit"
        );
        Ok(output)
    }
    async fn checked(&self, arguments: &[&str]) -> Result<()> {
        let output = self.command(arguments).await?;
        ensure!(
            output.status.success(),
            "systemd command failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
        Ok(())
    }
    pub async fn status(&self, unit: &str) -> Result<UnitStatus> {
        validate_service(unit)?;
        let output = self
            .command(&[
                "show",
                "--property=Id,LoadState,ActiveState,SubState,Job,Result",
                "--",
                unit,
            ])
            .await?;
        let text = std::str::from_utf8(&output.stdout).context("invalid systemd output")?;
        let fields: BTreeMap<_, _> = text
            .lines()
            .filter_map(|line| line.split_once('='))
            .collect();
        ensure!(
            output.status.success() || fields.get("LoadState") == Some(&"not-found"),
            "systemd status failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
        let load = fields
            .get("LoadState")
            .context("systemd omitted LoadState")?
            .to_string();
        let active = fields
            .get("ActiveState")
            .context("systemd omitted ActiveState")?
            .to_string();
        let sub = fields.get("SubState").unwrap_or(&"dead").to_string();
        let job_id = fields
            .get("Job")
            .and_then(|value| value.split_whitespace().next())
            .unwrap_or("0")
            .parse()
            .context("invalid systemd Job")?;
        Ok(UnitStatus {
            name: fields
                .get("Id")
                .copied()
                .filter(|name| !name.is_empty())
                .unwrap_or(unit)
                .into(),
            load,
            active,
            sub,
            job_id,
            result: fields.get("Result").unwrap_or(&"").to_string(),
        })
    }
    pub async fn list_units(&self, units: &[String]) -> Result<BTreeMap<String, UnitStatus>> {
        let mut result = BTreeMap::new();
        for unit in units {
            result.insert(unit.clone(), self.status(unit).await?);
        }
        Ok(result)
    }
    pub async fn reload(&self) -> Result<()> {
        self.checked(&["daemon-reload"]).await
    }
    pub async fn reset_failed(&self, unit: &str) -> Result<()> {
        validate_service(unit)?;
        self.checked(&["reset-failed", "--", unit]).await
    }
    pub async fn start(&self, unit: &str) -> Result<()> {
        self.submit("start", unit).await
    }
    pub async fn stop(&self, unit: &str) -> Result<()> {
        self.submit("stop", unit).await
    }
    pub async fn restart(&self, unit: &str) -> Result<()> {
        self.submit("restart", unit).await
    }
    async fn submit(&self, action: &str, unit: &str) -> Result<()> {
        validate_service(unit)?;
        self.checked(&["--no-block", action, "--", unit]).await
    }
    pub async fn wait_idle(
        &self,
        units: &[String],
        duration: Duration,
    ) -> Result<BTreeMap<String, UnitStatus>> {
        ensure!(!duration.is_zero(), "systemd wait timeout must be positive");
        timeout(duration, async {
            loop {
                let states = self.list_units(units).await?;
                if states.values().all(|state| {
                    state.job_id == 0
                        && !matches!(
                            state.active.as_str(),
                            "activating" | "deactivating" | "reloading"
                        )
                }) {
                    return Ok(states);
                }
                sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .context("systemd job did not finish before timeout")?
    }
}
fn validate_service(unit: &str) -> Result<()> {
    ensure!(
        unit.ends_with(".service")
            && !unit.starts_with('-')
            && unit
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._:@\\-".contains(&byte)),
        "invalid systemd service name"
    );
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum PermissionKind {
    Admin,
    Whitelist,
    Ban,
}
impl PermissionKind {
    fn file(self) -> &'static str {
        match self {
            Self::Admin => "adminlist.txt",
            Self::Whitelist => "whitelist.txt",
            Self::Ban => "blocklist.txt",
        }
    }
    fn field(self) -> &'static str {
        match self {
            Self::Admin => "adminlist",
            Self::Whitelist => "whitelist",
            Self::Ban => "blocklist",
        }
    }
}

pub struct Host {
    pub rooms: RoomStore,
    pub quadlet_dir: PathBuf,
    pub systemd: Systemd,
    pub journals: JournalLogs,
    pub port_pool: std::ops::RangeInclusive<u16>,
}
impl Host {
    pub fn new(root: impl Into<PathBuf>, quadlet_dir: impl Into<PathBuf>) -> Self {
        let quadlet_dir = quadlet_dir.into();
        Self {
            rooms: RoomStore::new(root).with_quadlet_dir(&quadlet_dir),
            quadlet_dir,
            systemd: Systemd::default(),
            journals: JournalLogs::default(),
            port_pool: 30000..=65535,
        }
    }
    pub fn unit(number: u16) -> String {
        format!("dst-{number:03}.service")
    }
    pub fn units(&self, number: u16) -> Result<Vec<String>> {
        self.rooms.path(number)?;
        Ok(vec![Self::unit(number)])
    }
    pub async fn connect(&self, number: u16) -> Result<Client> {
        Ok(Client::connect(self.rooms.path(number)?.join(AGENT_SOCKET)).await?)
    }
    pub async fn call(&self, number: u16, request: Request) -> Result<Value> {
        let client = self.connect(number).await?;
        let result = client.call(Envelope::new(Target::Room, request)?).await;
        let closed = client.close().await;
        let value = result?;
        closed?;
        Ok(value)
    }
    pub async fn load(&self, number: u16) -> Result<Room> {
        let state = self.systemd.status(&Self::unit(number)).await?;
        if !state.running() {
            return self.rooms.load(number);
        }
        let configuration = self.call(number, Request::ReadConfiguration {}).await?;
        let mut room = Room::new(
            number,
            ClusterConfig::from_value(
                configuration
                    .get("cluster")
                    .cloned()
                    .unwrap_or_else(|| configuration.clone()),
            )?,
        )?;
        if let Some(policy) = configuration.get("policy") {
            room.policy = serde_json::from_value(policy.clone())?;
        }
        room.template = configuration
            .get("template")
            .and_then(Value::as_str)
            .map(str::to_owned);
        room.deployment =
            ContainerUnit::load(self.quadlet_dir.join(format!("dst-{number:03}.container")))?
                .options;
        room.validate()?;
        Ok(room)
    }
    pub async fn status(&self, number: u16, game: bool) -> Result<Value> {
        let directory = self.rooms.path(number)?;
        let state = self.systemd.status(&Self::unit(number)).await?;
        let mut error = None;
        let mut configuration_error = None;
        let mut desired = None;
        if !state.running() {
            match self.rooms.load(number) {
                Ok(room) => desired = Some(room.policy.schedule_at(chrono::Utc::now())?.open),
                Err(_) => {
                    configuration_error = Some("room configuration could not be loaded".to_owned())
                }
            }
        }
        let mut agent = Value::Null;
        if game && state.running() {
            match timeout(
                Duration::from_secs(5),
                self.call(number, Request::Status {}),
            )
            .await
            {
                Ok(Ok(value)) => agent = value,
                Ok(Err(failure)) => error = Some(failure.to_string()),
                Err(_) => error = Some("Agent status timed out".into()),
            }
        }
        // Native configuration failures do not hide service state or diagnostic access.
        if !directory.is_dir() {
            configuration_error = Some("room directory is missing".into());
        }
        Ok(
            json!({"number":number,"desired":desired,"running":state.running(),"load":state.load,"active":state.active,"sub":state.sub,"job_id":state.job_id,"units":{Self::unit(number):state},"game":agent,"error":error.or_else(||configuration_error.clone()),"configuration_error":configuration_error}),
        )
    }
    pub async fn list(&self) -> Result<Vec<Value>> {
        let mut result = Vec::new();
        for number in self.rooms.numbers()? {
            result.push(self.status(number, false).await?);
        }
        Ok(result)
    }
    pub async fn journal(&self, numbers: &[u16], query: &JournalQuery) -> Result<JournalResult> {
        let units = numbers
            .iter()
            .map(|number| self.units(*number))
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        self.journals
            .query(Some(&units), query, Duration::from_secs(30))
            .await
    }
    pub async fn follow_journal(
        &self,
        numbers: &[u16],
        query: &JournalQuery,
    ) -> Result<JournalStream> {
        let units = numbers
            .iter()
            .map(|number| self.units(*number))
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        self.journals.follow(Some(&units), query).await
    }
    pub async fn diagnose(&self, number: u16) -> Result<Value> {
        let mut status = self.status(number, true).await?;
        let query = JournalQuery {
            limit: 50,
            ..Default::default()
        };
        match self.journal(&[number], &query).await {
            Ok(logs) => status["logs"] = serde_json::to_value(logs)?,
            Err(error) => status["logs_error"] = Value::String(error.to_string()),
        }
        Ok(status)
    }
    pub async fn create(&self, definition: &Room) -> Result<Room> {
        definition.validate()?;
        let directory = self.rooms.path(definition.number)?;
        files::create_directory(self.rooms.root())?;
        files::create_directory(&self.quadlet_dir)?;
        files::create_directory(&directory)?;
        let mut lock = RoomLock::try_acquire(&directory)?;
        ensure!(
            std::fs::read_dir(&directory)?
                .all(|entry| entry.is_ok_and(|entry| entry.file_name() == files::ROOM_LOCK_FILE)),
            "room already exists"
        );
        ensure!(
            !self
                .quadlet_dir
                .join(format!("dst-{:03}.container", definition.number))
                .try_exists()?,
            "deployment already exists"
        );
        self.require_inactive(definition.number).await?;
        self.save_locked(definition, &mut lock)?;
        drop(lock);
        self.systemd.reload().await?;
        self.rooms.load(definition.number)
    }
    pub async fn edit(&self, definition: &Room) -> Result<Room> {
        definition.validate()?;
        let number = definition.number;
        let state = self.systemd.status(&Self::unit(number)).await?;
        if state.running() {
            let current = self.load(number).await?;
            ensure!(
                current.deployment == definition.deployment,
                "deployment changes require an inactive room service"
            );
            ensure!(
                current.template == definition.template,
                "template changes require an inactive room service"
            );
            if current.cluster.as_value() != definition.cluster.as_value() {
                self.call(
                    number,
                    Request::Configure {
                        configuration: definition.cluster.as_value().clone(),
                        replace_permissions: false,
                    },
                )
                .await?;
            }
            if current.policy != definition.policy {
                self.call(
                    number,
                    Request::SetPolicy {
                        policy: serde_json::to_value(&definition.policy)?,
                    },
                )
                .await?;
            }

            return self.load(number).await;
        }
        let mut lock = RoomLock::try_acquire(self.rooms.path(number)?)?;
        self.require_inactive(number).await?;
        ensure!(
            lock.read_optional_text("cluster.ini")?.is_some(),
            "room does not exist"
        );
        self.save_locked(definition, &mut lock)?;
        drop(lock);
        self.systemd.reload().await?;
        self.rooms.load(number)
    }
    fn save_locked(&self, definition: &Room, lock: &mut RoomLock) -> Result<()> {
        validate_real_directory(&self.quadlet_dir)?;
        // Preflight the complete native unit before changing the game files.
        let allocation = PortAllocation::reserve(
            self.rooms.root(),
            &self.quadlet_dir,
            definition.number,
            &definition.cluster,
            &definition.deployment.ports,
            self.port_pool.clone(),
        )?;
        let mut options = definition.deployment.clone();
        options.ports = allocation.mappings.clone();
        let unit = ContainerUnit::for_cluster(
            &format!("dst-{:03}", definition.number),
            lock.directory(),
            &definition.cluster,
            options,
        )?;
        unit.validate_update(&self.quadlet_dir)?;
        lock.read_control()?;
        {
            let mut stopped = lock.while_stopped();
            stopped.recover()?;
            definition
                .cluster
                .save(&mut stopped, PermissionFiles::Preserve)?;
        }
        lock.update_control(|control| {
            control.insert(
                "template".into(),
                serde_json::to_value(&definition.template)?,
            );
            control.insert("policy".into(), serde_json::to_value(&definition.policy)?);
            Ok(())
        })?;
        unit.save(&self.quadlet_dir)?;
        allocation.commit()
    }
    async fn require_inactive(&self, number: u16) -> Result<()> {
        ensure!(
            !self.systemd.status(&Self::unit(number)).await?.running(),
            "offline changes require an inactive room service"
        );
        Ok(())
    }
    pub async fn start(&self, number: u16, wait: bool, duration: Duration) -> Result<Value> {
        self.transition(number, "start", wait, duration).await
    }
    pub async fn stop(&self, number: u16, wait: bool, duration: Duration) -> Result<Value> {
        self.transition(number, "stop", wait, duration).await
    }
    pub async fn restart(&self, number: u16, wait: bool, duration: Duration) -> Result<Value> {
        self.transition(number, "restart", wait, duration).await
    }
    async fn transition(
        &self,
        number: u16,
        action: &str,
        wait: bool,
        duration: Duration,
    ) -> Result<Value> {
        self.rooms.path(number)?;
        ensure!(!duration.is_zero(), "lifecycle timeout must be positive");
        let deadline = Instant::now()
            .checked_add(duration)
            .context("lifecycle timeout is out of range")?;
        timeout_at(deadline, async {
            let unit = Self::unit(number);
            if action != "stop" {
                ContainerUnit::load(self.quadlet_dir.join(format!("dst-{number:03}.container")))?;
                self.systemd.reload().await?;
                if self.systemd.status(&unit).await?.active == "failed" {
                    self.systemd.reset_failed(&unit).await?;
                }
            }
            match action {
                "start" => self.systemd.start(&unit).await?,
                "stop" => self.systemd.stop(&unit).await?,
                "restart" => self.systemd.restart(&unit).await?,
                _ => unreachable!(),
            }
            if !wait {
                return Ok(json!({"number":number,"action":action,"waiting":true}));
            }
            let states = self
                .systemd
                .wait_idle(
                    std::slice::from_ref(&unit),
                    deadline.saturating_duration_since(Instant::now()),
                )
                .await?;
            let state = &states[&unit];
            if action == "stop" {
                ensure!(!state.running(), "room service did not stop");
                return self.status(number, false).await;
            }
            ensure!(
                state.active == "active",
                "room service failed to start: {}",
                state.result
            );
            self.wait_ready(number, deadline.saturating_duration_since(Instant::now()))
                .await
        })
        .await
        .context(
            "room lifecycle operation timed out; the submitted systemd job may still complete",
        )?
    }
    pub async fn wait_ready(&self, number: u16, duration: Duration) -> Result<Value> {
        ensure!(!duration.is_zero(), "readiness timeout must be positive");
        timeout(duration, async {
            loop {
                let status = self.status(number, true).await?;
                ensure!(status["active"] != "failed", "room service failed");
                if !status["game"].is_null() {
                    let game = &status["game"];
                    ensure!(game["phase"] != "failed", "room games failed to start");
                    let shards: Vec<crate::model::ShardStatus> =
                        serde_json::from_value(game["shards"].clone())?;
                    if !shards.is_empty() && shards.iter().all(|shard| shard.readiness.ready()) {
                        return Ok(status);
                    }
                    if game["phase"] == "stopped" {
                        let room = self.load(number).await?;
                        if !room.policy.schedule_at(chrono::Utc::now())?.open {
                            return Ok(status);
                        }
                    }
                }
                sleep(Duration::from_millis(200)).await;
            }
        })
        .await
        .context("room Agent did not become available before timeout")?
    }
    pub async fn announce(
        &self,
        number: u16,
        message: String,
        count: u64,
        interval: f64,
    ) -> Result<Value> {
        self.call(
            number,
            Request::Announce {
                message,
                count,
                interval,
            },
        )
        .await
    }
    pub async fn update_mods(
        &self,
        number: u16,
        restart: bool,
        notice: Option<crate::model::Countdown>,
    ) -> Result<Value> {
        self.call(number, Request::UpdateMods { restart, notice })
            .await
    }
    pub async fn set_policy(&self, number: u16, policy: &Policy) -> Result<()> {
        policy.validate()?;
        if self.systemd.status(&Self::unit(number)).await?.running() {
            self.call(
                number,
                Request::SetPolicy {
                    policy: serde_json::to_value(policy)?,
                },
            )
            .await?;
        } else {
            let mut lock = RoomLock::try_acquire(self.rooms.path(number)?)?;
            self.require_inactive(number).await?;
            policy.save(&mut lock)?;
        }
        Ok(())
    }
    pub async fn permission(
        &self,
        number: u16,
        kind: PermissionKind,
        userid: Option<&str>,
        remove: bool,
    ) -> Result<Value> {
        if let Some(userid) = userid {
            Request::IsAdmin {
                userid: userid.to_owned(),
            }
            .validate()?;
        }
        if self.systemd.status(&Self::unit(number)).await?.running() {
            if let Some(userid) = userid {
                let userid = userid.to_owned();
                let request = match (kind, remove) {
                    (PermissionKind::Ban, false) => Request::Ban {
                        userid: userid.clone(),
                        seconds: None,
                    },
                    (PermissionKind::Ban, true) => Request::Unban {
                        userid: userid.clone(),
                    },
                    (PermissionKind::Whitelist, false) => Request::Whitelist {
                        userid: userid.clone(),
                    },
                    (PermissionKind::Whitelist, true) => Request::Unwhitelist {
                        userid: userid.clone(),
                    },
                    (PermissionKind::Admin, _) => Request::SetAdmin { userid, remove },
                };
                self.call(number, request).await?;
            }
            if kind == PermissionKind::Ban {
                return self.call(number, Request::Blocklist {}).await;
            }
            let permissions = self.call(number, Request::ReadPermissions {}).await?;
            return Ok(permissions[kind.field()].clone());
        }
        let mut lock = RoomLock::try_acquire(self.rooms.path(number)?)?;
        self.require_inactive(number).await?;
        let mut stopped = lock.while_stopped();
        stopped.recover()?;
        let source = stopped
            .read_optional_bytes(kind.file())?
            .unwrap_or_default();
        if let Some(userid) = userid {
            let updated = files::edit_permission(&source, userid, remove);
            if updated != source {
                stopped.replace_permission(kind.file(), &updated)?;
            }
            return Ok(json!(files::permission_users(&updated)));
        }
        Ok(json!(files::permission_users(&source)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[tokio::test]
    async fn native_permission_metadata_survives_configuration_and_targeted_edits() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("rooms");
        let script = temporary.path().join("systemctl");
        std::fs::write(&script, "#!/bin/sh\nprintf 'Id=dst-001.service\\nLoadState=not-found\\nActiveState=inactive\\nSubState=dead\\nJob=0\\nResult=success\\n'\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut host = Host::new(&root, temporary.path().join("units"));
        host.systemd.executable = script;
        let cluster =
            crate::rooms::build_template("pure_survival", 1, "token", None, None).unwrap();
        host.create(&Room::new(1, cluster).unwrap()).await.unwrap();
        let room = root.join("001");
        let first = b"KU_first\xba\xba1790989763\xba\xbaRoom \xff\xba\n";
        let second = b"KU_second\xbaName\xba1790989764\xba\xbaOther room\xba\n";
        let native = [first.as_slice(), second.as_slice()].concat();
        std::fs::write(room.join("blocklist.txt"), &native).unwrap();
        std::fs::write(room.join("adminlist.txt"), b"KU_admin\r\n").unwrap();
        std::fs::write(room.join("whitelist.txt"), b"KU_guest\n").unwrap();
        let loaded = host.load(1).await.unwrap();
        let changed = loaded
            .edit("/cluster/settings/max_players", json!(12), false)
            .unwrap();
        host.edit(&changed).await.unwrap();
        assert_eq!(std::fs::read(room.join("blocklist.txt")).unwrap(), native);
        assert_eq!(
            host.permission(1, PermissionKind::Ban, None, false)
                .await
                .unwrap(),
            json!(["KU_first", "KU_second"])
        );
        host.permission(1, PermissionKind::Ban, Some("KU_first"), false)
            .await
            .unwrap();
        assert_eq!(std::fs::read(room.join("blocklist.txt")).unwrap(), native);
        host.permission(1, PermissionKind::Ban, Some("KU_new"), false)
            .await
            .unwrap();
        assert_eq!(
            std::fs::read(room.join("blocklist.txt")).unwrap(),
            [&native[..], b"KU_new\n"].concat()
        );
        host.permission(1, PermissionKind::Ban, Some("KU_first"), true)
            .await
            .unwrap();
        let remaining = [&second[..], b"KU_new\n"].concat();
        assert_eq!(
            std::fs::read(room.join("blocklist.txt")).unwrap(),
            remaining
        );
        host.permission(1, PermissionKind::Admin, Some("KU_newadmin"), false)
            .await
            .unwrap();
        assert_eq!(
            std::fs::read(room.join("adminlist.txt")).unwrap(),
            b"KU_admin\r\nKU_newadmin\n"
        );
        assert_eq!(
            std::fs::read(room.join("whitelist.txt")).unwrap(),
            b"KU_guest\n"
        );
        assert_eq!(
            std::fs::read(room.join("blocklist.txt")).unwrap(),
            remaining
        );
    }

    #[tokio::test]
    async fn deployment_preflight_and_room_lock_protect_native_edits() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("rooms");
        let units = temporary.path().join("units");
        let script = temporary.path().join("systemctl");
        std::fs::write(&script, "#!/bin/sh\ncase \"$*\" in *show*) printf 'Id=dst-001.service\\nLoadState=not-found\\nActiveState=inactive\\nSubState=dead\\nJob=0\\nResult=success\\n';; esac\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut host = Host::new(&root, &units);
        host.systemd.executable = script;
        host.port_pool = 45000..=45100;
        let cluster =
            crate::rooms::build_template("pure_survival", 1, "test-token", None, None).unwrap();
        let room = host.create(&Room::new(1, cluster).unwrap()).await.unwrap();
        assert_eq!(room.deployment.ports.len(), 4);
        let changed = room
            .edit("/cluster/settings/max_players", json!(12), false)
            .unwrap();
        let guard = RoomLock::try_acquire(root.join("001")).unwrap();
        assert!(host.edit(&changed).await.is_err());
        drop(guard);
        host.edit(&changed).await.unwrap();
        let native = std::fs::read(root.join("001/cluster.ini")).unwrap();
        let dropin = units.join("dst-001.container.d");
        std::fs::create_dir(&dropin).unwrap();
        std::fs::write(
            dropin.join("image.conf"),
            "[Container]\nImage=foreign/image\n",
        )
        .unwrap();
        let changed = changed
            .edit_many(
                &[
                    ("/deployment/image", json!("example/new:image")),
                    ("/cluster/settings/max_players", json!(13)),
                ],
                &[],
            )
            .unwrap();
        assert!(host.edit(&changed).await.is_err());
        assert_eq!(std::fs::read(root.join("001/cluster.ini")).unwrap(), native);
    }

    #[tokio::test]
    async fn corrupt_room_still_has_status_and_stop_uses_only_its_service() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("rooms");
        let units = temporary.path().join("units");
        files::create_directory(root.join("001")).unwrap();
        files::create_directory(&units).unwrap();
        std::fs::write(root.join("001/cluster.ini"), "broken").unwrap();
        std::fs::write(root.join("001/.dst-control.json"), "[bad").unwrap();
        let script = temporary.path().join("systemctl");
        let trace = temporary.path().join("calls");
        std::fs::write(&script, format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\ncase \"$*\" in *show*) printf 'Id=dst-001.service\\nLoadState=loaded\\nActiveState=inactive\\nSubState=dead\\nJob=0\\nResult=success\\n';; esac\n", trace.display())).unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut host = Host::new(&root, units);
        host.systemd.executable = script;
        let status = host.status(1, true).await.unwrap();
        assert!(status["configuration_error"].is_string());
        assert_eq!(status["active"], "inactive");
        host.stop(1, true, Duration::from_secs(2)).await.unwrap();
        let calls = std::fs::read_to_string(trace).unwrap();
        assert!(calls.contains("--no-block stop -- dst-001.service"));
        assert!(!calls.contains("daemon-reload"));
        assert!(!calls.contains("start --"));
    }
}
