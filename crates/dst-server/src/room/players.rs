//! Route player operations using the observed world session and entity identity.
use super::*;

impl Room {
    async fn locate(
        &self,
        userid: &str,
        requested: Option<&str>,
        include_details: bool,
    ) -> Result<(PlayerLocation, Option<u64>)> {
        self.require_ready()?;
        let names: Vec<_> = self
            .cluster
            .lock()
            .unwrap()
            .shards
            .iter()
            .filter(|shard| requested.is_none_or(|name| shard.name == name))
            .map(|shard| shard.name.clone())
            .collect();
        let mut observations = Vec::new();
        for name in names {
            let driver = self.driver(&name)?;
            let initial = identity(&driver.snapshot())?;
            let observed = driver
                .request(
                    "locate_player",
                    json!({"userid":userid, "include_details":include_details}),
                    COMMAND,
                )
                .await
                .map_err(driver_error)?;
            if identity(&driver.snapshot())? != initial
                || observed["session_id"] != initial.session_id
            {
                return Err(Error::new(
                    ErrorCode::StaleReference,
                    "world changed while locating the player",
                ));
            }
            if observed["present"] == true {
                observations.push((name, initial, observed));
            }
        }
        let avatars: Vec<_> = observations
            .iter()
            .filter(|(_, _, observed)| observed["guid"].is_u64())
            .collect();
        let active: Vec<_> = avatars
            .iter()
            .copied()
            .filter(|(_, _, observed)| observed["departing"] != true && observed["loaded"] != false)
            .collect();
        let departing = avatars
            .iter()
            .copied()
            .find(|(_, _, observed)| observed["departing"] == true);
        let state = if active.len() > 1 {
            PlayerState::Conflict
        } else if departing.is_some() {
            PlayerState::Departing
        } else if active.len() == 1 {
            PlayerState::Active
        } else if !observations.is_empty() {
            PlayerState::Loading
        } else {
            PlayerState::Disconnected
        };
        let found = departing
            .or_else(|| active.first().or(avatars.first()).copied())
            .or(match observations.as_slice() {
                [client] => Some(client),
                _ => None,
            });
        Ok((
            PlayerLocation {
                userid: userid.into(),
                shard: found.map(|(name, _, _)| name.clone()),
                identity: found.map(|(_, identity, _)| identity.clone()),
                state,
                player: found
                    .or_else(|| observations.first())
                    .map(|(_, _, observed)| observed["player"].clone())
                    .unwrap_or(Value::Null),
            },
            found.and_then(|(_, _, observed)| observed["guid"].as_u64()),
        ))
    }
    pub(super) async fn player_request(&self, target: &Target, request: &Request) -> Result<Value> {
        let userid = request.userid().expect("player request has a userid");
        let requested = match target {
            Target::Shard(name) => Some(name.as_str()),
            Target::Room => None,
        };
        let include_details = matches!(request, Request::GetPlayer { .. });
        let (location, guid) = self.locate(userid, requested, include_details).await?;
        if include_details {
            return if requested.is_some() {
                Ok(location.player)
            } else {
                value(location)
            };
        }
        let (name, current) = location.require_active()?;
        let args = player_arguments(request.arguments()?, current, guid)?;
        if let Request::Migrate { shard_id, .. } = request {
            let mut destination = None;
            for (candidate, driver) in self.drivers() {
                let runtime = driver
                    .request("runtime", json!({}), COMMAND)
                    .await
                    .map_err(driver_error)?;
                if runtime["shard_id"] == *shard_id {
                    destination = Some(candidate);
                }
            }
            let destination = destination.ok_or_else(|| {
                Error::new(
                    ErrorCode::Invalid,
                    "migration destination is not a configured shard",
                )
            })?;
            if destination == name {
                return Err(Error::new(
                    ErrorCode::Invalid,
                    "player already occupies the requested shard",
                ));
            }
        }
        let result = self.native(name, request.method(), args).await?;
        if matches!(request, Request::Despawn { .. }) && result == true {
            let deadline = Instant::now() + COMMAND;
            loop {
                let (observed, observed_guid) = self.locate(userid, None, false).await?;
                let completed =
                    observed_guid != guid || observed.identity.as_ref() != Some(current);
                if completed {
                    return Ok(result);
                }
                if Instant::now() >= deadline {
                    return Err(Error::new(
                        ErrorCode::Unknown,
                        "player removal did not finish",
                    ));
                }
                sleep(Duration::from_millis(100)).await;
            }
        }

        Ok(result)
    }

    pub(super) async fn check_remote_vote_access(
        &self,
        master: &str,
        starter: &str,
        args: &mut Value,
    ) -> Result<()> {
        let (location, guid) = self.locate(starter, None, false).await?;
        // The master can check a connected lobby client without a character.
        if guid.is_none() {
            return Ok(());
        }
        let (shard, current) = location.require_active()?;
        if shard != master {
            let mut check = player_arguments(args.clone(), current, guid)?;
            check["userid"] = json!(starter);
            let hash = vote_result(self.native(shard, "check_vote_access", check).await?)?;
            args["_checked_command_hash"] = json!(hash.as_i64().ok_or_else(|| {
                Error::new(
                    ErrorCode::Protocol,
                    "invalid vote command hash from starter's shard",
                )
            })?);
        }
        Ok(())
    }

    pub(super) async fn kick(&self, userid: &str) -> Result<Value> {
        let deadline = Instant::now() + COMMAND;
        let mut kicked = false;
        loop {
            // A migrating client can reconnect on a different shard during a kick.
            let args = json!({"userid":userid});
            kicked |= any_true(
                self.all("kick", args.clone()).await,
                ErrorCode::PartialFailure,
                "some shards could not disconnect the client",
            )?;
            if !any_true(
                self.all("is_connected", args).await,
                ErrorCode::Unknown,
                "could not confirm client disconnection",
            )? {
                return Ok(json!(kicked));
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "client disconnection did not finish",
                ));
            }
            sleep(Duration::from_millis(100)).await;
        }
    }
}

fn player_arguments(
    mut args: Value,
    current: &SessionIdentity,
    guid: Option<u64>,
) -> Result<Value> {
    args["_expected_session_id"] = json!(current.session_id);
    args["_expected_generation"] = json!(current.generation);
    args["_expected_guid"] = json!(
        guid.ok_or_else(|| Error::new(ErrorCode::NotReady, "player entity has no native GUID"))?
    );
    Ok(args)
}

fn any_true(shards: Vec<ShardResult>, code: ErrorCode, message: &str) -> Result<bool> {
    let mut found = false;
    for shard in &shards {
        match &shard.result {
            Outcome::Success {
                value: Value::Bool(value),
            } => found |= value,
            _ => return Err(Error::new(code, message).with_details(json!(shards))),
        }
    }
    Ok(found)
}
