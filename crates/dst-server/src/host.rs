//! Host lifecycle and configuration access through one room Agent and one service.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::process::Command;
use tokio::time::{Instant, sleep, timeout, timeout_at};

use crate::{
    deployment::{ContainerUnit, PortAllocation},
    files::{self, PermissionFiles, RoomLock},
    logs::{JournalLogs, JournalQuery, JournalResult, JournalStream, JournalUnits},
    model::{Envelope, Request, Target},
    policy::{ModPhase, Policy, PolicyState},
    recovery,
    rooms::{Room, RoomStore},
    rpc::Client,
    settings::ClusterConfig,
};

mod status;

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

pub(crate) fn applied_error(
    number: u16,
    stage: &str,
    cause: impl std::fmt::Display,
) -> anyhow::Error {
    crate::model::Error::new(
        crate::model::ErrorCode::PartialFailure,
        format!("room {number:03} change was applied, {stage} failed: {cause}"),
    )
    .with_details(json!({"number":number, "applied":true, "stage":stage}))
    .into()
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
    /// Discover managed services even when their native room files need repair.
    pub fn managed_numbers(&self) -> Result<Vec<u16>> {
        if !self.quadlet_dir.try_exists()? {
            return Ok(Vec::new());
        }
        crate::deployment::validate_real_directory(&self.quadlet_dir)?;
        let mut numbers = Vec::new();
        for entry in std::fs::read_dir(&self.quadlet_dir)? {
            let name = entry?.file_name();
            let Some(digits) = name
                .to_str()
                .and_then(|name| name.strip_prefix("dst-"))
                .and_then(|name| name.strip_suffix(".container"))
            else {
                continue;
            };
            if digits.len() != 3 || !digits.bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            let number: u16 = digits.parse()?;
            if number <= crate::rooms::MAX_ROOM_SLOT && self.managed_unit(number).is_ok() {
                numbers.push(number);
            }
        }
        numbers.sort_unstable();
        Ok(numbers)
    }

    pub fn units(&self, number: u16) -> Result<Vec<String>> {
        self.units_for(&[number])
    }
    pub async fn connect(&self, number: u16) -> Result<Client> {
        Ok(Client::connect(self.rooms.path(number)?.join(AGENT_SOCKET)).await?)
    }
    pub async fn call(&self, number: u16, request: Request) -> Result<Value> {
        request.validate()?;
        if crate::permissions::supports_offline(&request)
            && !self.systemd.status(&Self::unit(number)).await?.running()
        {
            let mut lock = self.lock_inactive(number).await?;
            return crate::permissions::apply(&mut lock.while_stopped(), &request);
        }
        if matches!(
            request,
            Request::Restart { .. } | Request::UpdateMods { .. }
        ) {
            self.managed_unit(number)?.check_native_ports()?;
        }
        let client = self.connect(number).await?;
        let mutating = request.mutating();
        let result = client.call(Envelope::new(Target::Room, request)?).await;
        let closed = client.close().await;
        let value = result?;
        if let Err(error) = closed {
            if mutating {
                return Err(applied_error(number, "connection_close", error));
            }
            return Err(error.into());
        }
        Ok(value)
    }
    pub async fn load(&self, number: u16) -> Result<Room> {
        let state = self.systemd.status(&Self::unit(number)).await?;
        if !state.running() {
            return self.rooms.load(number);
        }
        self.load_online(number, &self.managed_unit(number)?).await
    }

    fn managed_unit(&self, number: u16) -> Result<ContainerUnit> {
        let directory = self.rooms.path(number)?;
        let path = self.quadlet_dir.join(format!("dst-{number:03}.container"));
        ensure!(
            path.try_exists()?,
            "no managed Quadlet for room {number:03} in {}",
            self.quadlet_dir.display()
        );
        ContainerUnit::load_for_room(path, &directory)
    }

    async fn load_online(&self, number: u16, unit: &ContainerUnit) -> Result<Room> {
        let mut configuration = self.call(number, Request::ReadConfiguration {}).await?;
        let mut room = Room::new(
            number,
            ClusterConfig::from_value(configuration["cluster"].take())?,
        )?;
        room.policy = serde_json::from_value(
            configuration
                .get_mut("policy")
                .context("missing configuration.policy")?
                .take(),
        )
        .context("invalid configuration.policy")?;
        room.template = serde_json::from_value(configuration["template"].take())?;
        unit.check_ports(crate::deployment::configured_ports(&room.cluster)?)?;
        room.deployment = unit.options.clone();
        room.validate()?;
        Ok(room)
    }
    fn units_for(&self, numbers: &[u16]) -> Result<Vec<String>> {
        numbers
            .iter()
            .map(|&number| {
                self.rooms.path(number)?;
                Ok(Self::unit(number))
            })
            .collect()
    }
    fn journal_scope<'a>(&self, units: &'a [String]) -> JournalUnits<'a> {
        JournalUnits::for_manager(self.systemd.user, Some(units))
    }
    pub async fn journal(&self, numbers: &[u16], query: &JournalQuery) -> Result<JournalResult> {
        let units = self.units_for(numbers)?;
        self.journals
            .query(
                Some(self.journal_scope(&units)),
                query,
                Duration::from_secs(30),
            )
            .await
    }
    pub async fn follow_journal(
        &self,
        numbers: &[u16],
        query: &JournalQuery,
    ) -> Result<JournalStream> {
        let units = self.units_for(numbers)?;
        self.journals
            .follow(Some(self.journal_scope(&units)), query)
            .await
    }
    pub async fn diagnose(&self, number: u16) -> Result<Value> {
        let mut status = self.status(number, true).await?;
        let query = JournalQuery {
            limit: Some(50),
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
        self.create_files(definition, &BTreeSet::new()).await?;
        self.reload_after_write(definition.number).await?;
        self.rooms
            .load(definition.number)
            .map_err(|e| applied_error(definition.number, "readback", e))
    }
    pub(crate) async fn create_files(
        &self,
        definition: &Room,
        batch_ports: &BTreeSet<u16>,
    ) -> Result<()> {
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
        self.save_locked(
            definition,
            &mut lock,
            PermissionFiles::ReplaceOffline,
            batch_ports,
        )
    }
    pub async fn edit(&self, definition: &Room) -> Result<Room> {
        definition.validate()?;
        let number = definition.number;
        let state = self.systemd.status(&Self::unit(number)).await?;
        if state.running() {
            let unit = self.managed_unit(number)?;
            let current = self.load_online(number, &unit).await?;
            ensure!(
                current.deployment == definition.deployment,
                "deployment changes require an inactive room service"
            );
            ensure!(
                current.template == definition.template,
                "template changes require an inactive room service"
            );
            let mut changes = Vec::new();
            let cluster = crate::rooms::without_permissions(definition.cluster.as_value());
            if crate::rooms::without_permissions(current.cluster.as_value()) != cluster {
                changes.push(("/cluster".into(), cluster));
            }
            if current.policy != definition.policy {
                changes.push(("/policy".into(), serde_json::to_value(&definition.policy)?));
            }
            if changes.is_empty() {
                return Ok(current);
            }
            return self.edit_online(number, &unit, changes, Vec::new()).await;
        }
        let lock = self.lock_inactive(number).await?;
        ensure!(
            lock.read_optional_text("cluster.ini")?.is_some(),
            "room does not exist"
        );
        self.finish_offline_edit(definition.clone(), lock, false)
            .await
    }
    pub async fn edit_fields(
        &self,
        number: u16,
        changes: &[(String, Value)],
        unset: &[String],
    ) -> Result<Room> {
        self.rooms.path(number)?;
        if self.systemd.status(&Self::unit(number)).await?.running() {
            return self
                .edit_online(
                    number,
                    &self.managed_unit(number)?,
                    changes.to_vec(),
                    unset.to_vec(),
                )
                .await;
        }
        let lock = self.lock_inactive(number).await?;
        let definition = self
            .rooms
            .load_for_edit(number, &lock)?
            .edited(changes, unset)?;
        let explicit_ports = changes
            .iter()
            .map(|(path, _)| path)
            .chain(unset)
            .any(|path| {
                matches!(path.as_str(), "/deployment" | "/deployment/ports")
                    || path.starts_with("/deployment/ports/")
            });
        self.finish_offline_edit(definition, lock, explicit_ports)
            .await
    }

    async fn edit_online(
        &self,
        number: u16,
        unit: &ContainerUnit,
        changes: Vec<(String, Value)>,
        unset: Vec<String>,
    ) -> Result<Room> {
        self.call(number, Request::EditConfiguration { changes, unset })
            .await?;
        self.load_online(number, unit)
            .await
            .map_err(|e| applied_error(number, "readback", e))
    }

    async fn finish_offline_edit(
        &self,
        mut definition: Room,
        mut lock: RoomLock,
        explicit_ports: bool,
    ) -> Result<Room> {
        if !explicit_ports
            && let Some(unit) = self.rooms.deployment(definition.number, lock.directory())?
            && (definition.deployment.ports.is_empty()
                || definition.deployment.ports == unit.options.ports)
        {
            definition.deployment.ports = unit.retained_ports(&definition.cluster)?;
        }
        self.save_locked(
            &definition,
            &mut lock,
            PermissionFiles::Preserve,
            &BTreeSet::new(),
        )?;
        let saved = self
            .rooms
            .load_locked(definition.number, &lock)
            .map_err(|e| applied_error(definition.number, "readback", e));
        drop(lock);
        self.reload_after_write(definition.number).await?;
        saved
    }

    async fn reload_after_write(&self, number: u16) -> Result<()> {
        self.systemd
            .reload()
            .await
            .map_err(|e| applied_error(number, "daemon_reload", e))
    }

    fn save_locked(
        &self,
        definition: &Room,
        lock: &mut RoomLock,
        permissions: PermissionFiles,
        batch_ports: &BTreeSet<u16>,
    ) -> Result<()> {
        let allocation = PortAllocation::reserve_for_batch(
            &self.quadlet_dir,
            definition.number,
            &definition.cluster,
            &definition.deployment.ports,
            self.port_pool.clone(),
            batch_ports,
        )?;
        let mut options = definition.deployment.clone();
        options.ports = allocation.mappings.clone();
        let unit = ContainerUnit::for_cluster(
            &format!("dst-{:03}", definition.number),
            lock.directory(),
            &definition.cluster,
            options,
        )?;
        let prepared = unit.prepare_save(&self.quadlet_dir)?;
        lock.read_state()?;
        definition
            .cluster
            .save(&mut lock.while_stopped(), permissions)?;
        crate::rooms::save_definition_fields(lock, definition)?;
        prepared.publish().map_err(|error| {
            applied_error(
                definition.number,
                "quadlet_publish",
                format_args!("native configuration and policy are on disk. Check the Quadlet before starting the room. {error}"),
            )
        })?;
        drop(allocation);
        Ok(())
    }
    async fn lock_inactive(&self, number: u16) -> Result<RoomLock> {
        let lock = RoomLock::try_acquire(self.rooms.path(number)?)?;
        self.require_inactive(number).await?;
        Ok(lock)
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
            let deployment = self.managed_unit(number)?;
            if action != "stop" {
                deployment.check_native_ports()?;
                self.systemd.reload().await?;
                let state = self.systemd.status(&unit).await?;
                if action == "start" && !state.running() {
                    let mut lock = self.lock_inactive(number).await?;
                    let all_day = Policy::load(&lock)?
                        .schedule_at(chrono::Utc::now())?
                        .next_change
                        .is_none();
                    recovery::explicit_start(&mut lock, all_day)?;
                }
                if state.active == "failed" {
                    self.systemd.reset_failed(&unit).await?;
                }
                if action == "start" && state.running() {
                    let status = self.observe(number, true).await?;
                    if status.recovery_closed() {
                        self.call(number, Request::Start {}).await?;
                    }
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
                ensure!(
                    state.active == "inactive" && state.result == "success",
                    "{unit} did not stop cleanly: ActiveState={}, Result={}",
                    state.active,
                    state.result
                );
                return self.status(number, false).await;
            }
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
                let status = self.observe(number, true).await?;
                if status.ready()? {
                    return Ok(status.report());
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
        self.rooms.path(number)?;
        let request = Request::UpdateMods { restart, notice };
        request.validate()?;
        timeout(request.completion_timeout()?, async {
            let unit = Self::unit(number);
            let state = self.systemd.status(&unit).await?;
            if state.running() {
                self.call(number, request).await?;
            } else {
                self.managed_unit(number)?.check_native_ports()?;
                {
                    let mut lock = self.lock_inactive(number).await?;
                    let mut policy_state = PolicyState::load(&lock)?;
                    policy_state.queue_mod_update(restart)?;
                    policy_state.save(&mut lock)?;
                }
                self.reload_after_write(number).await?;
                if state.active == "failed" {
                    self.systemd
                        .reset_failed(&unit)
                        .await
                        .map_err(|e| applied_error(number, "reset_failed", e))?;
                }
                self.systemd
                    .start(&unit)
                    .await
                    .map_err(|e| applied_error(number, "start", e))?;
            }
            loop {
                let status = self.observe(number, true).await?;
                if let Some(result) = status.completed_mod_update(restart)? {
                    return Ok(result);
                }
                sleep(Duration::from_millis(200)).await;
            }
        })
        .await
        .context("Mod maintenance wait timed out; the accepted update may still complete")?
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
            let mut lock = self.lock_inactive(number).await?;
            policy.save(&mut lock)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Timelike;
    use std::os::unix::fs::PermissionsExt;

    async fn offline_host() -> (tempfile::TempDir, Host) {
        let temporary = tempfile::tempdir().unwrap();
        let script = temporary.path().join("systemctl");
        std::fs::write(&script, "#!/bin/sh\ncase \"$*\" in *show*) printf 'Id=dst-001.service\\nLoadState=loaded\\nActiveState=inactive\\nSubState=dead\\nJob=0\\nResult=success\\n';; esac\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut host = Host::new(
            temporary.path().join("rooms"),
            temporary.path().join("units"),
        );
        host.systemd.executable = script;
        let cluster =
            crate::rooms::build_template("pure_survival", 1, "token", None, None).unwrap();
        host.create(&Room::new(1, cluster).unwrap()).await.unwrap();
        (temporary, host)
    }

    fn closed_policy() -> Policy {
        let hour = chrono::Utc::now().hour();
        Policy {
            timezone: "UTC".into(),
            schedule: vec![crate::policy::DailyWindow {
                start: chrono::NaiveTime::from_hms_opt((hour + 6) % 24, 0, 0).unwrap(),
                end: chrono::NaiveTime::from_hms_opt((hour + 7) % 24, 0, 0).unwrap(),
            }],
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn offline_edits_keep_each_shards_game_and_query_host_ports() {
        use crate::deployment::PortMapping;

        let (_temporary, host) = offline_host().await;
        let directory = host.rooms.path(1).unwrap();
        let ini = directory.join("cluster.ini");
        let without_key = std::fs::read_to_string(&ini)
            .unwrap()
            .lines()
            .filter(|line| !line.starts_with("cluster_key ="))
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(&ini, without_key).unwrap();
        assert!(crate::configuration::discover(&directory).is_err());
        host.rooms.save(&host.load(1).await.unwrap()).unwrap();
        assert!(crate::configuration::discover(&directory).is_ok());

        let port = |room: &Room, container: u16| {
            room.deployment
                .ports
                .iter()
                .find(|port| port.container == container)
                .unwrap()
                .host
        };
        let mut room = host.load(1).await.unwrap();
        let fixed = std::net::UdpSocket::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        room.deployment.ports = vec![PortMapping {
            container: 10999,
            host: fixed,
        }];
        let room = host.edit(&room).await.unwrap();
        let query = port(&room, 27016);
        assert_eq!(port(&room, 10999), fixed);

        let game_field = "/cluster/shards/forest/settings/server_port";
        let query_field = "/cluster/shards/forest/settings/master_server_port";
        let swapped = room
            .edit_many(
                &[(game_field, json!(27016)), (query_field, json!(10999))],
                &[],
            )
            .unwrap();
        assert!(host.rooms.save(&swapped).is_err());
        let change_deployment = room
            .edit("/deployment/start_on_boot", json!(false), false)
            .unwrap();
        assert!(host.rooms.save(&change_deployment).is_err());
        assert_eq!(host.load(1).await.unwrap().get(game_field).unwrap(), 10999);

        let swapped = host.edit(&swapped).await.unwrap();
        assert_eq!(
            (port(&swapped, 27016), port(&swapped, 10999)),
            (fixed, query)
        );
        let edited = host
            .edit_fields(
                1,
                &[(game_field.into(), json!(19388))],
                &["/cluster/shards/cave".into()],
            )
            .await
            .unwrap();
        assert_eq!(edited.deployment.ports.len(), 2);
        assert_eq!((port(&edited, 19388), port(&edited, 10999)), (fixed, query));
        assert_eq!(
            host.managed_unit(1).unwrap().published_game_ports,
            [("forest".into(), fixed)].into()
        );
    }

    #[tokio::test]
    async fn explicit_field_edits_keep_the_requested_mapping_when_shard_ports_swap() {
        let (_temporary, host) = offline_host().await;
        let game = "/cluster/shards/forest/settings/server_port";
        let query = "/cluster/shards/forest/settings/master_server_port";
        let mut room = host.load(1).await.unwrap();
        let published = |room: &Room| {
            room.deployment
                .ports
                .iter()
                .map(|port| (port.container, port.host))
                .collect::<BTreeMap<_, _>>()
        };
        let requested = published(&room);
        for path in [
            "/deployment/ports",
            "/deployment/ports/0/host",
            "/deployment",
        ] {
            let changes = [
                (game.into(), room.get(query).unwrap()),
                (query.into(), room.get(game).unwrap()),
                (path.into(), room.get(path).unwrap()),
            ];
            room = host.edit_fields(1, &changes, &[]).await.unwrap();
            assert_eq!(published(&room), requested, "{path}");
        }
    }

    #[tokio::test]
    async fn native_port_drift_blocks_start_and_both_offline_edits_can_repair_it() {
        let (_temporary, host) = offline_host().await;
        let native = RoomStore::new(host.rooms.root());
        let field = "/cluster/shards/forest/settings/server_port";
        let initial = host.managed_unit(1).unwrap().published_game_ports;
        for (port, restore_definition) in [(19388, false), (19389, true)] {
            let before = host.load(1).await.unwrap();
            let changed = native
                .load(1)
                .unwrap()
                .edit(field, json!(port), false)
                .unwrap();
            native.save(&changed).unwrap();
            let status = host.status(1, false).await.unwrap();
            assert!(
                status["configuration_error"]
                    .as_str()
                    .unwrap()
                    .contains("host edit")
            );
            assert!(
                host.load(1)
                    .await
                    .unwrap_err()
                    .is::<crate::deployment::PublishedPortMismatch>()
            );
            assert!(
                host.start(1, false, Duration::from_secs(2))
                    .await
                    .unwrap_err()
                    .is::<crate::deployment::PublishedPortMismatch>()
            );
            if restore_definition {
                host.edit(&before).await.unwrap();
            } else {
                host.edit_fields(1, &[(field.into(), json!(port))], &[])
                    .await
                    .unwrap();
            }
            let room = host.load(1).await.unwrap();
            assert_eq!(room.get(field).unwrap(), 19388);
            assert_eq!(host.managed_unit(1).unwrap().published_game_ports, initial);
            host.start(1, false, Duration::from_secs(2)).await.unwrap();
        }
        let before = host.load(1).await.unwrap();
        let cave = "/cluster/shards/cave/settings/master_server_port";
        let forest = "/cluster/shards/forest/settings/master_server_port";
        let changes = [
            (cave.into(), before.get(forest).unwrap()),
            (forest.into(), before.get(cave).unwrap()),
        ];
        let changed = native
            .load(1)
            .unwrap()
            .edited(&changes, &[] as &[String])
            .unwrap();
        native.save(&changed).unwrap();
        assert!(host.status(1, false).await.unwrap()["configuration_error"].is_string());
        assert!(host.start(1, false, Duration::from_secs(2)).await.is_err());
        let after = host.edit_fields(1, &changes, &[]).await.unwrap();
        assert!(
            before
                .deployment
                .ports
                .iter()
                .map(|port| port.host)
                .eq(after.deployment.ports.iter().map(|port| port.host))
        );
        host.start(1, false, Duration::from_secs(2)).await.unwrap();
    }

    #[tokio::test]
    async fn offline_field_edits_do_not_overwrite_another_edit() {
        let (temporary, host) = offline_host().await;
        let paused = temporary.path().join("paused-systemctl");
        std::fs::write(&paused, include_str!("../tests/fixtures/edit_systemctl.py")).unwrap();
        std::fs::set_permissions(&paused, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(temporary.path().join("service-state"), "inactive").unwrap();
        let mut first = Host::new(host.rooms.root(), &host.quadlet_dir);
        first.systemd.executable = paused;
        let editing = tokio::spawn(async move {
            first
                .edit_fields(
                    1,
                    &[("/cluster/settings/cluster_name".into(), json!("renamed"))],
                    &[],
                )
                .await
        });
        timeout(Duration::from_secs(5), async {
            while !temporary.path().join("waiting").exists() {
                sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let changes = [("/cluster/settings/max_players".into(), json!(12))];
        let second = host.edit_fields(1, &changes, &[]).await;
        std::fs::write(temporary.path().join("release"), "").unwrap();
        editing.await.unwrap().unwrap();
        if second.is_err() {
            host.edit_fields(1, &changes, &[]).await.unwrap();
        }
        let saved = host.load(1).await.unwrap();
        assert_eq!(
            saved.get("/cluster/settings/cluster_name").unwrap(),
            "renamed"
        );
        assert_eq!(saved.get("/cluster/settings/max_players").unwrap(), 12);
    }

    #[tokio::test]
    async fn offline_edit_and_store_save_cancel_only_applied_mod_participants() {
        use crate::{mods::UpdateTarget, policy::PolicyState, settings::ClusterConfig};
        let (_temporary, host) = offline_host().await;
        let mut room = host.rooms.load(1).unwrap();
        let mut cluster = room.cluster.clone().into_value();
        let shards: Vec<_> = cluster["shards"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        for shard in &shards {
            cluster["shards"][shard]["mods"] = json!({"entries":{"workshop-42":{"enabled":true}}});
        }
        room.cluster = ClusterConfig::from_value(cluster).unwrap();
        host.edit(&room).await.unwrap();
        let directory = host.rooms.path(1).unwrap();
        let mut lock = RoomLock::try_acquire(&directory).unwrap();
        let mut state = PolicyState::default();
        state.mods.enter_pending();
        state.mods.phase = ModPhase::Verifying;
        state.mods.attempts = 1;
        state.mods.targets = [(
            42,
            UpdateTarget {
                version: "2".into(),
                shards: shards.iter().cloned().collect(),
            },
        )]
        .into();
        state.save(&mut lock).unwrap();
        drop(lock);
        for (index, shard) in shards.iter().enumerate() {
            room = host.rooms.load(1).unwrap();
            let mut cluster = room.cluster.into_value();
            cluster["shards"][shard]["mods"] = json!({});
            room.cluster = ClusterConfig::from_value(cluster).unwrap();
            if index == 0 {
                host.edit(&room).await.unwrap();
            } else {
                host.rooms.save(&room).unwrap();
            }
            let lock = RoomLock::try_acquire(&directory).unwrap();
            let remaining = PolicyState::load(&lock).unwrap();
            if index + 1 == shards.len() {
                assert_eq!(remaining.mods, Default::default());
            } else {
                assert_eq!(
                    remaining.mods.targets[&42].shards,
                    shards[index + 1..].iter().cloned().collect()
                );
            }
            assert_eq!(Policy::load(&lock).unwrap(), room.policy);
        }
    }

    #[tokio::test]
    async fn a_closed_schedule_still_requires_a_resident_agent() {
        let (_temporary, host) = offline_host().await;
        host.set_policy(1, &closed_policy()).await.unwrap();
        let error = host
            .start(1, true, Duration::from_secs(2))
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "room Agent service is stopped");
        let status = host.status(1, true).await.unwrap();
        assert_eq!(status["running"], false);
        assert_eq!(status["desired"], false);
        assert!(status["game"].is_null());
        assert_eq!(status["policy"]["timezone"], "UTC");
        assert_eq!(status["schedule"]["open"], false);
        assert_eq!(status["maintenance"]["phase"]["state"], "idle");
        assert_eq!(status["recovery"]["restarts_used"], 0);
        assert!(status["configuration_error"].is_null());
        assert!(host.wait_ready(1, Duration::from_secs(1)).await.is_err());
    }

    #[tokio::test]
    async fn mismatched_quadlet_cannot_edit_or_start_a_room_or_queue_maintenance() {
        let (_temporary, host) = offline_host().await;
        let edit = host
            .load(1)
            .await
            .unwrap()
            .edit("/cluster/settings/max_players", json!(12), false)
            .unwrap();
        let path = host.quadlet_dir.join("dst-001.container");
        let mut unit = ContainerUnit::load(&path).unwrap();
        unit.cluster = host.rooms.path(2).unwrap();
        let foreign = unit.render().unwrap();
        std::fs::write(&path, &foreign).unwrap();
        let directory = host.rooms.path(1).unwrap();
        let ini = std::fs::read(directory.join("cluster.ini")).unwrap();
        let before = std::fs::read(directory.join(crate::files::STATE_FILE)).unwrap();
        let expected = "Quadlet must mount the selected room directory";
        assert!(host.managed_numbers().unwrap().is_empty());
        assert_eq!(
            host.stop(1, false, Duration::from_secs(2))
                .await
                .unwrap_err()
                .to_string(),
            expected
        );
        assert_eq!(host.load(1).await.unwrap_err().to_string(), expected);
        assert_eq!(host.edit(&edit).await.unwrap_err().to_string(), expected);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), foreign);
        assert_eq!(std::fs::read(directory.join("cluster.ini")).unwrap(), ini);
        for (active, sub) in [("inactive", "dead"), ("active", "running")] {
            std::fs::write(&host.systemd.executable, format!(
                "#!/bin/sh\ncase \"$*\" in *show*) printf 'Id=dst-001.service\\nLoadState=loaded\\nActiveState={active}\\nSubState={sub}\\nJob=0\\nResult=success\\n';; *) exit 1;; esac\n"
            )).unwrap();
            assert_eq!(
                host.start(1, false, Duration::from_secs(2))
                    .await
                    .unwrap_err()
                    .to_string(),
                expected
            );
            assert_eq!(
                host.restart(1, false, Duration::from_secs(2))
                    .await
                    .unwrap_err()
                    .to_string(),
                expected
            );
            for restart in [false, true] {
                assert_eq!(
                    host.update_mods(1, restart, None)
                        .await
                        .unwrap_err()
                        .to_string(),
                    expected
                );
            }
        }
        for request in [
            Request::Restart { notice: None },
            Request::UpdateMods {
                restart: false,
                notice: None,
            },
            Request::UpdateMods {
                restart: true,
                notice: None,
            },
        ] {
            assert_eq!(
                host.operate(1, &crate::host_operations::HostOperation::Call { request })
                    .await
                    .unwrap_err()
                    .to_string(),
                expected
            );
        }
        assert_eq!(
            host.call(1, Request::Status {})
                .await
                .unwrap_err()
                .downcast::<crate::model::Error>()
                .unwrap()
                .code,
            crate::model::ErrorCode::Transport
        );
        assert_eq!(
            std::fs::read(directory.join(crate::files::STATE_FILE)).unwrap(),
            before
        );
    }

    #[tokio::test]
    async fn dropins_must_preserve_both_room_volumes() {
        let (_temporary, host) = offline_host().await;
        let room = host.load(1).await.unwrap();
        let directory = host.rooms.path(1).unwrap();
        let mounts = format!(
            "Volume={}:/cluster\nVolume={}/mods:/install/mods\n",
            directory.display(),
            directory.display()
        );
        let global = host.quadlet_dir.join("container.d");
        let local = host.quadlet_dir.join("dst-001.container.d");
        std::fs::create_dir(&global).unwrap();
        std::fs::create_dir(&local).unwrap();
        std::fs::write(global.join("10-reset.conf"), "[Container]\nVolume=\n").unwrap();
        let path = local.join("20-volumes.conf");
        for invalid in [
            mounts.replace(":/cluster", "-old:/cluster"),
            mounts.replace("/mods:/install/mods", "/old:/install/mods"),
            mounts.lines().next().unwrap().to_owned(),
            format!("{mounts}Volume=/cluster/"),
        ] {
            std::fs::write(&path, format!("[Container]\n{invalid}\n")).unwrap();
            assert!(host.load(1).await.is_err());
            assert!(host.edit(&room).await.is_err());
            assert!(host.start(1, false, Duration::from_secs(2)).await.is_err());
        }
        std::fs::write(
            path,
            format!("[Container]\n{mounts}Volume=/srv/extra:/extra:ro\n"),
        )
        .unwrap();
        host.edit(&room).await.unwrap();
        host.start(1, false, Duration::from_secs(2)).await.unwrap();
    }

    #[tokio::test]
    async fn starting_agent_lock_is_not_reported_as_corrupt_configuration() {
        let (temporary, host) = offline_host().await;
        let marker = temporary.path().join("starting");
        std::fs::write(
            &host.systemd.executable,
            format!(
                "#!/usr/bin/env python3\nimport pathlib\nmarker = pathlib.Path({})\nactive = 'active' if marker.exists() else 'inactive'\nmarker.touch()\nprint(f'Id=dst-001.service\\nLoadState=loaded\\nActiveState={{active}}\\nSubState=running\\nJob=0\\nResult=success')\n",
                serde_json::to_string(&marker).unwrap(),
            ),
        )
        .unwrap();
        let _agent = RoomLock::try_acquire(host.rooms.path(1).unwrap()).unwrap();
        let status = host.status(1, false).await.unwrap();
        assert_eq!(status["running"], true);
        assert!(status["configuration_error"].is_null());
        assert!(status["error"].is_null());
        host.start(1, false, Duration::from_secs(2)).await.unwrap();
    }

    #[tokio::test]
    async fn offline_failures_remain_visible_and_only_explicit_all_day_start_resets_recovery() {
        let (_temporary, host) = offline_host().await;
        let mut lock = RoomLock::try_acquire(host.rooms.path(1).unwrap()).unwrap();
        lock.update_state(|document| {
            document.insert("recovery".into(), json!({"restarts_used":2}));
            Ok(())
        })
        .unwrap();
        recovery::on_failure(
            &mut lock,
            recovery::Failure::Configuration,
            &Default::default(),
            &Default::default(),
            0,
        )
        .unwrap();
        drop(lock);
        let status = host.status(1, true).await.unwrap();
        assert_eq!(status["recovery"]["closed"], "configuration");
        assert_eq!(status["recovery"]["restarts_used"], 2);
        assert!(status["error"].as_str().unwrap().contains("configuration"));
        assert!(
            host.wait_ready(1, Duration::from_secs(1))
                .await
                .unwrap_err()
                .to_string()
                .contains("configuration")
        );
        host.restart(1, false, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(
            host.status(1, true).await.unwrap()["recovery"],
            status["recovery"]
        );
        host.start(1, false, Duration::from_secs(2)).await.unwrap();
        let restarted = host.status(1, true).await.unwrap();
        assert!(restarted["recovery"]["closed"].is_null());
        assert_eq!(restarted["recovery"]["restarts_used"], 0);
        host.set_policy(1, &closed_policy()).await.unwrap();
        let mut lock = RoomLock::try_acquire(host.rooms.path(1).unwrap()).unwrap();
        recovery::on_failure(
            &mut lock,
            recovery::Failure::Configuration,
            &Default::default(),
            &Default::default(),
            0,
        )
        .unwrap();
        drop(lock);
        host.start(1, false, Duration::from_secs(2)).await.unwrap();
        assert_eq!(
            host.status(1, true).await.unwrap()["recovery"]["closed"],
            "configuration"
        );
    }

    #[tokio::test]
    async fn failed_and_invalid_offline_maintenance_do_not_look_like_a_clean_closure() {
        let (_temporary, host) = offline_host().await;
        host.set_policy(1, &closed_policy()).await.unwrap();
        let mut lock = RoomLock::try_acquire(host.rooms.path(1).unwrap()).unwrap();
        let mut state = PolicyState::default();
        state.mods.enter_pending();
        state.mods.phase = ModPhase::Failed;
        state.mods.attempts = crate::policy::MOD_ATTEMPTS;
        state.save(&mut lock).unwrap();
        drop(lock);
        let status = host.status(1, true).await.unwrap();
        assert_eq!(status["maintenance"]["phase"]["state"], "failed");
        assert!(status["error"].as_str().unwrap().contains("exhausted"));
        assert!(
            host.wait_ready(1, Duration::from_secs(1))
                .await
                .unwrap_err()
                .to_string()
                .contains("exhausted three attempts")
        );
        let mut lock = RoomLock::try_acquire(host.rooms.path(1).unwrap()).unwrap();
        lock.update_state(|control| {
            control.get_mut("policy_state").unwrap()["mods"]["attempts"] = json!(4);
            Ok(())
        })
        .unwrap();
        drop(lock);
        let status = host.status(1, true).await.unwrap();
        assert!(status["configuration_error"].is_string());
        assert!(status["maintenance"].is_null());
        assert!(host.wait_ready(1, Duration::from_secs(1)).await.is_err());
    }

    #[tokio::test]
    async fn offline_mod_updates_persist_intent_and_require_the_requested_agent() {
        let (temporary, host) = offline_host().await;
        host.set_policy(1, &closed_policy()).await.unwrap();
        let control = host.rooms.path(1).unwrap().join(files::STATE_FILE);
        let observed = temporary.path().join("observed.json");
        let mut lock = RoomLock::try_acquire(host.rooms.path(1).unwrap()).unwrap();
        let mut maintenance = PolicyState::load(&lock).unwrap();
        maintenance.mods.enter_pending();
        let targets = json!({"42":{"version":"2","shards":["Master"]}});
        maintenance.mods.targets = serde_json::from_value(targets.clone()).unwrap();
        maintenance.save(&mut lock).unwrap();
        recovery::on_failure(
            &mut lock,
            recovery::Failure::Mods,
            &Default::default(),
            &Default::default(),
            0,
        )
        .unwrap();
        drop(lock);
        std::fs::write(
            &host.systemd.executable,
            format!(
                r#"#!/usr/bin/env python3
import json, pathlib, sys
control = pathlib.Path({})
observed = pathlib.Path({})
if 'daemon-reload' in sys.argv and observed.with_name('reject-reload').exists():
    sys.exit('reload failed')
if 'start' in sys.argv:
    state = json.loads(control.read_text())
    mods = state['policy_state']['mods']
    assert mods['phase']['state'] == 'pending' and mods['manual']
    observed.write_text(json.dumps(mods))
    state['policy_state']['mods'] = json.loads({idle_mods:?})
    control.write_text(json.dumps(state))
if 'show' in sys.argv:
    print('Id=dst-001.service\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=0\nResult=success')
"#,
                serde_json::to_string(&control).unwrap(),
                serde_json::to_string(&observed).unwrap(),
                idle_mods = serde_json::to_string(&crate::policy::ModMaintenance::default()).unwrap(),
            ),
        )
        .unwrap();
        let reject_reload = temporary.path().join("reject-reload");
        std::fs::write(&reject_reload, "").unwrap();
        let error = host.update_mods(1, false, None).await.unwrap_err();
        let error = error.downcast_ref::<crate::model::Error>().unwrap();
        assert_eq!(error.code, crate::model::ErrorCode::PartialFailure);
        assert_eq!(
            error.details,
            json!({"number":1, "stage":"daemon_reload", "applied":true})
        );
        let state: Value = serde_json::from_slice(&std::fs::read(&control).unwrap()).unwrap();
        let pending = &state["policy_state"]["mods"];
        assert!(pending["origin"]["operation_id"].is_string());
        assert_eq!(pending["origin"]["source"], "manual");
        assert_eq!(pending["targets"], targets);
        std::fs::remove_file(reject_reload).unwrap();
        assert!(
            host.update_mods(1, true, None)
                .await
                .unwrap_err()
                .to_string()
                .contains("already pending")
        );
        assert!(!observed.exists());

        for restart in [false, true] {
            let result = host.update_mods(1, restart, None).await;
            if restart {
                assert_eq!(
                    result.unwrap_err().to_string(),
                    "room Agent service stopped after Mod maintenance"
                );
            } else {
                assert_eq!(
                    result.unwrap(),
                    json!({"restarted":false,"verification_pending":false})
                );
            }
            let intent: Value = serde_json::from_slice(&std::fs::read(&observed).unwrap()).unwrap();
            assert_eq!(intent["resume_game"], restart);
            if !restart {
                assert_eq!(&intent, pending);
            }
        }
    }

    #[tokio::test]
    async fn native_permission_metadata_survives_configuration_and_targeted_edits() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("rooms");
        let script = temporary.path().join("systemctl");
        std::fs::write(&script, "#!/bin/sh\nprintf 'Id=dst-001.service\\nLoadState=not-found\\nActiveState=inactive\\nSubState=dead\\nJob=0\\nResult=success\\n'\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut host = Host::new(&root, temporary.path().join("units"));
        host.systemd.executable = script;
        let mut cluster = crate::rooms::build_template("pure_survival", 1, "token", None, None)
            .unwrap()
            .into_value();
        cluster["blocklist"] = json!("KU_initial_ban\n");
        cluster["adminlist"] = json!("KU_initial_admin\n");
        cluster["whitelist"] = json!("KU_initial_guest\n");
        let definition = Room::new(1, ClusterConfig::from_value(cluster).unwrap()).unwrap();
        let definition = definition
            .edit("/cluster", definition.as_value()["cluster"].take(), false)
            .unwrap()
            .edit("/cluster/settings/max_players", json!(10), false)
            .unwrap();
        host.create(&definition).await.unwrap();
        let room = root.join("001");
        for (name, contents) in [
            ("blocklist.txt", "KU_initial_ban\n"),
            ("adminlist.txt", "KU_initial_admin\n"),
            ("whitelist.txt", "KU_initial_guest\n"),
        ] {
            assert_eq!(std::fs::read_to_string(room.join(name)).unwrap(), contents);
        }
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
            host.call(1, Request::Blocklist {}).await.unwrap(),
            json!(["KU_first", "KU_second"])
        );
        host.call(
            1,
            Request::Ban {
                userid: "KU_first".into(),
                seconds: None,
            },
        )
        .await
        .unwrap();
        assert_eq!(std::fs::read(room.join("blocklist.txt")).unwrap(), native);
        host.call(
            1,
            Request::Ban {
                userid: "KU_new".into(),
                seconds: None,
            },
        )
        .await
        .unwrap();
        assert_eq!(
            std::fs::read(room.join("blocklist.txt")).unwrap(),
            [&native[..], b"KU_new\n"].concat()
        );
        host.call(
            1,
            Request::Unban {
                userid: "KU_first".into(),
            },
        )
        .await
        .unwrap();
        let remaining = [&second[..], b"KU_new\n"].concat();
        assert_eq!(
            std::fs::read(room.join("blocklist.txt")).unwrap(),
            remaining
        );
        host.call(
            1,
            Request::SetAdmin {
                userid: "KU_newadmin".into(),
                enabled: true,
            },
        )
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
    async fn provisioning_retries_a_rejected_quadlet_and_initializes_permission_lists() {
        let (_temporary, host) = offline_host().await;
        let mut cluster = crate::rooms::build_template("pure_survival", 2, "token", None, None)
            .unwrap()
            .into_value();
        cluster["adminlist"] = json!("KU_owner\n");
        cluster["blocklist"] = json!("");
        cluster.as_object_mut().unwrap().remove("whitelist");
        let definition = Room::new(2, ClusterConfig::from_value(cluster).unwrap()).unwrap();
        let mut oversized_label = definition.clone();
        oversized_label.template = Some("界".repeat(86));
        assert!(host.create(&oversized_label).await.is_err());
        assert!(!host.rooms.path(2).unwrap().exists());
        let mut too_large = definition.clone();
        too_large
            .deployment
            .environment
            .insert("TEST_VALUE".into(), "x".repeat(files::MAX_FILE_BYTES));
        let error = host.create(&too_large).await.unwrap_err();
        assert!(
            error.to_string().contains("host file exceeds limit"),
            "{error}"
        );
        let directory = host.rooms.path(2).unwrap();
        assert!(
            std::fs::read_dir(&directory)
                .unwrap()
                .all(|entry| { entry.unwrap().file_name() == files::ROOM_LOCK_FILE })
        );
        assert!(!host.quadlet_dir.join("dst-002.container").exists());
        let result = host
            .provision(std::slice::from_ref(&definition))
            .await
            .unwrap();
        assert_eq!(result[0]["result"]["ok"], true, "{result:?}");
        assert_eq!(
            std::fs::read(directory.join("adminlist.txt")).unwrap(),
            b"KU_owner\n"
        );
        for name in ["blocklist.txt", "whitelist.txt"] {
            assert!(std::fs::read(directory.join(name)).unwrap().is_empty());
        }
        std::fs::write(directory.join("adminlist.txt"), b"KU_new_owner\n").unwrap();
        assert_eq!(
            host.create(&definition).await.unwrap_err().to_string(),
            "room already exists"
        );
        assert_eq!(
            std::fs::read(directory.join("adminlist.txt")).unwrap(),
            b"KU_new_owner\n"
        );
    }

    #[tokio::test]
    async fn waited_stop_reports_systemd_failures() {
        let (_temporary, host) = offline_host().await;
        for (active, result) in [("failed", "timeout"), ("inactive", "exit-code")] {
            std::fs::write(
                &host.systemd.executable,
                format!(
                    "#!/bin/sh\ncase \"$*\" in *show*) printf 'LoadState=loaded\\nActiveState={active}\\nJob=0\\nResult={result}\\n';; esac\n"
                ),
            )
            .unwrap();
            let error = host
                .stop(1, true, Duration::from_secs(2))
                .await
                .unwrap_err()
                .to_string();
            assert!(
                error.contains(&format!("ActiveState={active}, Result={result}")),
                "{error}"
            );
        }
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
        std::fs::remove_file(dropin.join("image.conf")).unwrap();
        let unit_path = units.join("dst-001.container");
        let current = std::fs::read_to_string(&unit_path).unwrap();
        let old = current
            .replace("StopSignal=SIGTERM\n", "")
            .replace("StopTimeout=540", "StopTimeout=360")
            .replace("TimeoutStopSec=600", "TimeoutStopSec=420");
        std::fs::write(&unit_path, &old).unwrap();
        assert!(
            host.edit(&changed)
                .await
                .unwrap_err()
                .to_string()
                .contains("unsupported or inconsistent managed Quadlet fields")
        );
        assert_eq!(std::fs::read(root.join("001/cluster.ini")).unwrap(), native);
        assert_eq!(std::fs::read_to_string(&unit_path).unwrap(), old);
        std::fs::write(&unit_path, current).unwrap();
        host.edit(&changed).await.unwrap();
        assert!(
            std::fs::read_to_string(unit_path)
                .unwrap()
                .contains("StopSignal=SIGTERM\nStopTimeout=540\n")
        );
    }

    #[tokio::test]
    async fn corrupt_room_still_has_status_and_stop_uses_only_its_service() {
        let (temporary, mut host) = offline_host().await;
        let root = host.rooms.root();
        std::fs::write(root.join("001/cluster.ini"), "broken").unwrap();
        std::fs::write(root.join("001/.dst-server.json"), "[bad").unwrap();
        let script = temporary.path().join("systemctl");
        let trace = temporary.path().join("calls");
        std::fs::write(&script, format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\ncase \"$*\" in *show*) printf 'Id=dst-001.service\\nLoadState=loaded\\nActiveState=inactive\\nSubState=dead\\nJob=0\\nResult=success\\n';; esac\n", trace.display())).unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
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
