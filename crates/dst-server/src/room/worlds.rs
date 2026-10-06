//! Save, inspect and reload native worlds using the current shard identities.
use super::*;
use crate::recovery::Snapshot;

#[derive(serde::Deserialize)]
struct SnapshotPage {
    session_id: String,
    snapshots: Vec<Snapshot>,
    has_more: bool,
}

fn world_file(session: &str, id: u64) -> String {
    format!("session/{session}/{id:010}")
}

fn world_path(shard: &str, file: &str) -> PathBuf {
    Path::new(shard).join("save").join(file)
}

fn clock_day(metadata: &Value) -> Option<u64> {
    metadata["clock"]["cycles"].as_u64()?.checked_add(1)
}

fn snapshot_metadata(lock: &RoomLock, shard: &str, snapshot: &Snapshot) -> Option<Value> {
    let path = world_path(shard, snapshot.world_file.as_deref()?);
    if !recovery::nonempty_world_file(lock, &path) {
        return None;
    }
    let source = lock
        .read_optional_text(path.with_extension("meta"))
        .ok()??;
    let mut metadata = crate::lua::parse_return_table(&source).ok()?;
    if let Some(day) = clock_day(&metadata) {
        metadata["day"] = json!(day);
    }
    Some(metadata)
}

fn snapshot_record(snapshot: &Snapshot, metadata: Option<Value>) -> Value {
    json!({"snapshot_id":snapshot.snapshot_id, "world_file":snapshot.world_file, "metadata":metadata})
}

impl Room {
    pub(super) async fn save(&self) -> Result<Value> {
        self.require_ready()?;
        let master = self.master();
        let runtime = self.native(&master, "runtime", json!({})).await?;
        let target = runtime["snapshot"]
            .as_u64()
            .ok_or_else(|| Error::new(ErrorCode::Protocol, "runtime snapshot is missing"))?;
        for (name, driver) in self.drivers() {
            if name == master {
                continue;
            }
            let runtime = driver
                .request("runtime", json!({}), COMMAND)
                .await
                .map_err(driver_error)?;
            let snapshot = runtime["snapshot"]
                .as_u64()
                .ok_or_else(|| Error::new(ErrorCode::Protocol, "runtime snapshot is missing"))?;
            if snapshot > target {
                return Err(Error::new(
                    ErrorCode::Conflict,
                    "a secondary snapshot is ahead of the master; saving could roll it backward",
                )
                .with_details(
                    json!({"shard":name,"snapshot_id":snapshot,"master_snapshot_id":target}),
                ));
            }
        }
        self.native(
            &master,
            "save",
            json!({"session_id":runtime["session_id"],"snapshot_id":target}),
        )
        .await
    }

    async fn snapshot_page(
        &self,
        shard: &str,
        expected: &SessionIdentity,
        limit: u64,
        before: Option<u64>,
    ) -> Result<SnapshotPage> {
        let driver = self.driver(shard)?;
        let reply = driver
            .request(
                "list_snapshots",
                native_arguments(json!({"limit":limit,"before":before})),
                COMMAND,
            )
            .await
            .map_err(driver_error)?;
        let page: SnapshotPage = serde_json::from_value(reply)
            .map_err(|_| Error::new(ErrorCode::Protocol, "invalid native snapshot catalog"))?;
        if page.session_id != expected.session_id || identity(&driver.snapshot())? != *expected {
            return Err(Error::new(
                ErrorCode::StaleReference,
                "snapshot catalog belongs to another world session",
            ));
        }
        for snapshot in &page.snapshots {
            if snapshot.snapshot_id > MAX_SAFE_INTEGER {
                return Err(Error::new(
                    ErrorCode::Protocol,
                    "invalid native snapshot identifier",
                ));
            }
            if let Some(file) = &snapshot.world_file
                && (*file != world_file(&page.session_id, snapshot.snapshot_id)
                    || recovery::native_path(shard, &page.session_id, file).is_err())
            {
                return Err(Error::new(
                    ErrorCode::Protocol,
                    "native snapshot path does not match its session and ID",
                ));
            }
        }
        Ok(page)
    }

    pub(super) async fn snapshots(
        &self,
        shard: &str,
        limit: u64,
        before: Option<u64>,
    ) -> Result<Value> {
        let driver = self.driver(shard)?;
        let initial = identity(&driver.snapshot())?;
        let page = self.snapshot_page(shard, &initial, limit, before).await?;
        let snapshots: Vec<_> = self.with_lock(|lock| {
            Ok(page
                .snapshots
                .iter()
                .map(|snapshot| snapshot_record(snapshot, snapshot_metadata(lock, shard, snapshot)))
                .collect())
        })?;
        if identity(&driver.snapshot())? != initial {
            return Err(Error::new(
                ErrorCode::StaleReference,
                "world changed while reading snapshot metadata",
            ));
        }
        Ok(json!({"session_id":page.session_id, "snapshots":snapshots, "has_more":page.has_more}))
    }

    async fn complete_snapshot(
        &self,
        id: u64,
        sessions: &BTreeMap<String, SessionIdentity>,
        day: Option<u64>,
    ) -> Result<bool> {
        let master = self.master();
        for (name, identity) in sessions {
            if day.is_some() && name == &master {
                continue; // The master catalog already matched this day.
            }
            let page = self.snapshot_page(name, identity, 1, Some(id + 1)).await?;
            let Some(snapshot) = page
                .snapshots
                .first()
                .filter(|snapshot| snapshot.snapshot_id == id && snapshot.world_file.is_some())
            else {
                return Ok(false);
            };
            if let Some(day) = day
                && self.with_lock(|lock| {
                    Ok(snapshot_metadata(lock, name, snapshot)
                        .as_ref()
                        .and_then(clock_day))
                })? != Some(day)
            {
                return Ok(false);
            }
        }
        self.has_snapshot_files(id, sessions)
    }

    fn has_snapshot_files(
        &self,
        id: u64,
        sessions: &BTreeMap<String, SessionIdentity>,
    ) -> Result<bool> {
        self.with_lock(|lock| {
            Ok(sessions.iter().all(|(name, identity)| {
                let file = world_file(&identity.session_id, id);
                recovery::nonempty_world_file(lock, &world_path(name, &file))
            }))
        })
    }
    async fn select_snapshot(
        &self,
        mut count: u64,
        day: Option<u64>,
        sessions: &BTreeMap<String, SessionIdentity>,
    ) -> Result<Value> {
        let mut before = None;
        let mut selected = None;
        let master = self.master();
        for _ in 0..64 {
            let page = self
                .snapshot_page(&master, &sessions[&master], 100, before)
                .await?;
            let candidates = page
                .snapshots
                .iter()
                .filter(|snapshot| snapshot.snapshot_id != 0 && snapshot.world_file.is_some());
            if let Some(day) = day {
                // Each native page is newest first. Keep only its earliest complete match.
                for snapshot in candidates.rev() {
                    let metadata =
                        self.with_lock(|lock| Ok(snapshot_metadata(lock, &master, snapshot)))?;
                    if metadata.as_ref().and_then(clock_day) == Some(day)
                        && self
                            .complete_snapshot(snapshot.snapshot_id, sessions, Some(day))
                            .await?
                    {
                        selected = Some(snapshot_record(snapshot, metadata));
                        break;
                    }
                }
            } else {
                for snapshot in candidates {
                    if count > 0 {
                        count -= 1;
                        continue;
                    }
                    if !self
                        .complete_snapshot(snapshot.snapshot_id, sessions, None)
                        .await?
                    {
                        return Err(Error::new(
                            ErrorCode::NotFound,
                            "requested snapshot is missing from a configured shard",
                        ));
                    }
                    return self.with_lock(|lock| {
                        Ok(snapshot_record(
                            snapshot,
                            snapshot_metadata(lock, &master, snapshot),
                        ))
                    });
                }
            }
            if !page.has_more {
                return selected.ok_or_else(|| {
                    Error::new(
                        ErrorCode::NotFound,
                        "no complete room snapshot matches the requested target",
                    )
                });
            }
            let next = page
                .snapshots
                .last()
                .map(|snapshot| snapshot.snapshot_id)
                .ok_or_else(|| {
                    Error::new(ErrorCode::Protocol, "snapshot catalog did not advance")
                })?;
            if before.is_some_and(|before| next >= before) {
                return Err(Error::new(
                    ErrorCode::Protocol,
                    "snapshot catalog did not advance",
                ));
            }
            before = Some(next);
        }
        Err(Error::new(
            ErrorCode::Overflow,
            "snapshot selection exceeds 6400 entries",
        ))
    }
    fn identities(&self) -> Result<BTreeMap<String, SessionIdentity>> {
        self.drivers()
            .into_iter()
            .map(|(name, driver)| identity(&driver.snapshot()).map(|identity| (name, identity)))
            .collect()
    }

    pub(super) async fn reload(&self, target: &Target, request: &Request) -> Result<Value> {
        self.require_ready()?;
        let initial = self.identities()?;
        let mut shard = self.master();
        let mut method = request.method();
        let mut args = request.arguments()?;
        let mut selected = None;
        let mut snapshot_id = None;
        match request {
            Request::Reset {} | Request::Rollback { .. } | Request::RollbackToDay { .. } => {
                let count = if let Request::Rollback { count } = request {
                    *count
                } else {
                    0
                };
                let day = if let Request::RollbackToDay { day } = request {
                    Some(*day)
                } else {
                    None
                };
                let snapshot = self.select_snapshot(count, day, &initial).await?;
                snapshot_id = snapshot["snapshot_id"].as_u64();
                args = json!({"session_id":initial[&shard].session_id,"snapshot_id":snapshot_id});
                method = "rollback_to_snapshot";
                selected = Some(snapshot);
            }
            Request::RollbackToSnapshot {
                session_id,
                snapshot_id: id,
            } => {
                if &initial[&shard].session_id != session_id {
                    return Err(Error::new(
                        ErrorCode::StaleReference,
                        "rollback target belongs to another world session",
                    ));
                }
                if !self.complete_snapshot(*id, &initial, None).await? {
                    return Err(Error::new(
                        ErrorCode::NotFound,
                        "rollback target is incomplete",
                    ));
                }
                snapshot_id = Some(*id);
            }
            Request::Regenerate {
                require_empty: Some(true),
                ..
            } => {
                for name in initial.keys() {
                    let presence = self.native(name, "presence", json!({})).await?;
                    if presence["reliable"] != true
                        || presence["client_count"] != 0
                        || presence["player_count"] != 0
                    {
                        return Err(Error::new(
                            ErrorCode::Conflict,
                            "regeneration requires every shard to be empty",
                        ));
                    }
                }
            }
            Request::RegenerateShard { .. } => {
                if let Target::Shard(name) = target {
                    shard = name.clone();
                }
            }
            _ => {}
        }
        if let Some(id) = snapshot_id {
            if self.identities()? != initial {
                return Err(Error::new(
                    ErrorCode::StaleReference,
                    "world changed during snapshot selection",
                ));
            }
            if !self.has_snapshot_files(id, &initial)? {
                return Err(Error::new(
                    ErrorCode::NotFound,
                    "rollback target is incomplete",
                ));
            }
        }
        let deadline = Instant::now() + Duration::from_secs_f64(RELOAD_TIMEOUT);
        match self
            .driver(&shard)?
            .request(method, native_arguments(args), COMMAND)
            .await
        {
            Ok(_) => {}
            Err(error)
                if error.written
                    && matches!(
                        error.code,
                        DriverErrorCode::Unknown | DriverErrorCode::Timeout
                    ) => {}
            Err(error) => return Err(driver_error(error)),
        }
        self.set_phase(Phase::Starting, None);
        loop {
            let drivers = self.drivers();
            let all_ready = drivers.iter().all(|(name, driver)| {
                let state = driver.snapshot();
                let changed = matches!(request, Request::RegenerateShard { .. }) && name != &shard
                    || state
                        .generation
                        .is_some_and(|generation| generation > initial[name].generation);
                state.running && state.ready && changed
            });
            if drivers.values().any(|driver| !driver.snapshot().running) {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "shard exited while reloading the world",
                ));
            }
            if all_ready {
                break;
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "new world generation did not become ready",
                ));
            }
            sleep(Duration::from_millis(100)).await;
        }
        self.wait_ready(
            deadline.saturating_duration_since(Instant::now()),
            snapshot_id.is_some(),
        )
        .await?;
        for (name, driver) in self.drivers() {
            let state = driver.snapshot();
            let current = identity(&state)?;
            let runtime = state.runtime.as_ref().ok_or_else(|| {
                Error::new(ErrorCode::Protocol, "reloaded world runtime is unavailable")
            })?;
            let regenerating = matches!(request, Request::Regenerate { .. })
                || matches!(request, Request::RegenerateShard { .. }) && name == shard;
            if regenerating && current.session_id == initial[&name].session_id
                || !regenerating && current.session_id != initial[&name].session_id
            {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "reloaded world has an unexpected session identity",
                ));
            }
            if snapshot_id.is_some_and(|id| runtime["snapshot"].as_u64() != Some(id + 1)) {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "reloaded world did not confirm the requested snapshot",
                ));
            }
        }
        self.set_phase(Phase::Running, None);
        Ok(selected.unwrap_or(Value::Null))
    }
}
