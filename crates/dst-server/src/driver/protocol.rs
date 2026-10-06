//! Decode native output and advance the state of a single game process.
use super::*;
use ControlFailureReason as Reason;

pub(super) fn handle_line(pump: &Pump, stream: StreamKind, line: &[u8]) -> DriverResult<()> {
    let inner = &pump.inner;
    let bytes = line.len();
    let line = match stream {
        StreamKind::Reply => line,
        _ => native_message(line),
    };
    if stream == StreamKind::Reply
        && let Some(payload) = line.strip_prefix(b"DST_OTEL|")
    {
        telemetry(pump, payload.strip_suffix(b"\n").unwrap_or(payload));
        return Ok(());
    }
    let line = line.trim_ascii_end();
    match stream {
        StreamKind::Reply => {
            if let Some(payload) = line.strip_prefix(b"DST_DRIVER|") {
                return bootstrap(inner, payload);
            }
            if line == b"DST_RemoteCommandDone" || line == b"DST_LuaBusy" {
                native_barrier(inner, line);
                return Ok(());
            }
            if let Some(payload) = line.strip_prefix(b"DST_RPC|") {
                return rpc(inner, payload);
            }
            if let Some(payload) = line.strip_prefix(b"DST_CONTROL|") {
                return control(pump, payload);
            }
        }
        StreamKind::Lifecycle => return lifecycle(pump, line),
        StreamKind::Stdout | StreamKind::Stderr => (),
    }
    pump.publish(bytes, || {
        DriverEventKind::Log(json!({"stream":stream,"line":String::from_utf8_lossy(line)}))
    });
    Ok(())
}

fn mandatory_record(inner: &Inner, payload: &[u8]) -> DriverResult<Value> {
    let value: Value = serde_json::from_slice(payload)
        .map_err(|_| Reason::Json.error("malformed mandatory JSON frame"))?;
    if !value.is_object()
        || value["nonce"].as_str() != Some(inner.snapshots.borrow().nonce.as_str())
    {
        return Err(Reason::Identity.error("mandatory frame has an invalid nonce or envelope"));
    }
    Ok(value)
}

fn advance_generation(shared: &mut Shared, generation: u64) -> bool {
    if shared
        .state
        .generation
        .is_some_and(|current| current > generation)
    {
        return false;
    }
    if shared.state.generation == Some(generation) {
        return true;
    }
    if shared.state.generation.is_some() {
        shared.state.generation_changes += 1;
        shared.state.session_id = None;
    }
    shared.state.generation = Some(generation);
    shared.state.stopping = shared.stop_requested;
    shared.state.ready = false;
    shared.state.health = None;
    shared.state.runtime = None;
    shared.state.last_control = None;
    shared.state.load_failure = None;
    shared.state.control_count = 0;
    shared.state.telemetry_sequence = 0;
    shared.observation = None;
    if let Some(active) = &mut shared.active
        && active.record.generation != generation
    {
        active.deliver(Err(active.error(
            DriverErrorCode::Unknown,
            "world generation changed while the command was pending",
        )));
        shared.state.recent_request = Some(active.record.clone());
    }
    true
}

fn recompute_ready(state: &mut DriverState) {
    state.ready = state.running
        && !state.stopping
        && state.failure.is_none()
        && state.native_ready
        && state.health.as_ref().is_some_and(|health| {
            safe_integer(&health["generation"]) == state.generation && control_ready(health)
        });
}

fn bootstrap(inner: &Inner, payload: &[u8]) -> DriverResult<()> {
    let record = mandatory_record(inner, payload)?;
    if record.get("error").is_some() {
        if safe_integer(&record["generation"])
            .zip(inner.snapshots.borrow().generation)
            .is_some_and(|(received, current)| received < current)
        {
            return Ok(());
        }
        let category = record["error"]
            .as_str()
            .filter(|category| {
                matches!(
                    *category,
                    "configuration_failed" | "installation_failed" | "publication_failed"
                )
            })
            .unwrap_or("invalid failure category");
        return Err(Reason::Bootstrap.error(format!("driver bootstrap failed: {category}")));
    }
    let health = record.get("health");
    let generation = safe_integer(&health.unwrap_or(&record)["generation"])
        .ok_or_else(|| Reason::Generation.error("invalid driver generation"))?;
    if health.is_some_and(|health| !health["control_ready"].is_boolean()) {
        return Err(Reason::Bootstrap.error("invalid driver health"));
    }
    inner.change(|shared| {
        if !advance_generation(shared, generation) {
            return;
        }
        if let Some(health) = health {
            shared.state.health = Some(health.clone());
        }
        recompute_ready(&mut shared.state);
    });
    Ok(())
}

fn rpc(inner: &Inner, payload: &[u8]) -> DriverResult<()> {
    let mut response = mandatory_record(inner, payload)?;
    if safe_integer(&response["generation"]).is_none() || !response["id"].is_string() {
        return Err(Reason::Rpc.error("invalid RPC response header"));
    }
    inner.change(|shared| {
        let Some(active) = shared
            .active
            .as_mut()
            .filter(|active| response["id"] == active.record.id)
        else {
            shared.state.unmatched_replies += 1;
            return Ok(());
        };
        let rejected = response["result"]["ok"] == false
            && !active.record.accepted
            && ["invalid_request", "not_ready", "stale_generation"]
                .iter()
                .any(|error| response["result"]["error"] == *error);
        if safe_integer(&response["generation"]) != Some(active.record.generation) && !rejected {
            return Err(
                Reason::Generation.error("RPC response generation does not match its request")
            );
        }
        active.record.written = true;
        active.synchronized = true;
        if response["accepted"] == true && response.get("result").is_none() {
            active.record.accepted = true;
        } else if let Some(result) = response.get_mut("result") {
            if active.result.is_some() {
                return Err(Reason::Rpc.error("duplicate RPC result"));
            }
            if result["ok"] == true && result.get("data").is_some() {
                active.record.accepted = true;
                let mut data = result["data"].take();
                if active.record.method == "presence"
                    && shared.state.generation == Some(active.record.generation)
                {
                    let fields = data
                        .as_object_mut()
                        .ok_or_else(|| Reason::Rpc.error("invalid presence result"))?;
                    let epoch = fields
                        .remove("observation_epoch")
                        .as_ref()
                        .and_then(safe_integer)
                        .ok_or_else(|| Reason::Rpc.error("invalid presence observation epoch"))?;
                    if shared
                        .observation
                        .as_ref()
                        .is_none_or(|(previous, _)| *previous != epoch)
                    {
                        shared.observation = Some((epoch, Ulid::generate().to_string()));
                    }
                    fields.insert(
                        "observation".into(),
                        json!(shared.observation.as_ref().unwrap().1),
                    );
                }
                if active.record.method == "runtime"
                    && shared.state.generation == Some(active.record.generation)
                {
                    shared.state.runtime = Some(data.clone());
                    shared.state.session_id = data["session_id"].as_str().map(str::to_owned);
                }
                if active.record.method == "health"
                    && shared.state.generation == Some(active.record.generation)
                {
                    shared.state.health = Some(data.clone());
                    recompute_ready(&mut shared.state);
                }
                active.result = Some(Ok(data));
            } else if result["ok"] == false {
                let error = result["error"]
                    .as_str()
                    .ok_or_else(|| Reason::Rpc.error("invalid RPC error"))?;
                if !rejected {
                    active.record.accepted = true;
                }
                let code = match error {
                    "invalid_request" | "rejected" => DriverErrorCode::InvalidRequest,
                    "not_ready" | "stale_generation" => DriverErrorCode::NotReady,
                    "unsupported" => DriverErrorCode::Unsupported,
                    "not_found" => DriverErrorCode::NotFound,
                    "stale_reference" => DriverErrorCode::StaleReference,
                    "indeterminate" => DriverErrorCode::Unknown,
                    "lua_error" | "invalid_json_value" | "invalid_utf8" | "response_too_large" => {
                        DriverErrorCode::Lua
                    }
                    _ => return Err(Reason::Rpc.error("unknown RPC error code")),
                };
                active.result = Some(Err(active.error(code, error)));
            } else {
                return Err(Reason::Rpc.error("invalid RPC result"));
            }
        } else {
            return Err(Reason::Rpc.error("RPC response lacks an acceptance or result"));
        }
        shared.state.recent_request = Some(active.record.clone());
        Ok(())
    })
}

fn native_barrier(inner: &Inner, line: &[u8]) {
    inner.change(|shared| {
        let Some(mut active) = shared.active.take() else {
            shared.state.unmatched_replies += 1;
            return;
        };
        if !active.synchronized {
            shared.state.unmatched_replies += 1;
            shared.active = Some(active);
            return;
        }
        if line == b"DST_LuaBusy" {
            if active.record.accepted || active.result.is_some() {
                shared.state.unmatched_replies += 1;
                shared.active = Some(active);
                return;
            }
            active.deliver(Err(active.error(
                DriverErrorCode::Busy,
                "native Lua rejected the request before acceptance",
            )));
        } else {
            let result = active.result.take().unwrap_or_else(|| {
                Err(active.error(
                    DriverErrorCode::Unknown,
                    "native command completed without a structured result",
                ))
            });
            active.deliver(result);
        }
        shared.state.recent_request = Some(active.record);
    });
}

fn control(pump: &Pump, payload: &[u8]) -> DriverResult<()> {
    let inner = &pump.inner;
    let record = mandatory_record(inner, payload)?;
    let generation = safe_integer(&record["generation"])
        .ok_or_else(|| Reason::Generation.error("invalid control generation"))?;
    if !record["event"].as_str().is_some_and(valid_method) {
        return Err(Reason::Control.error("invalid control record"));
    }
    let accepted = inner.change(|shared| {
        if !advance_generation(shared, generation) {
            return false;
        }
        if record["event"] == "world_load_failed" {
            shared.state.load_failure = Some(record.clone());
        }
        shared.state.last_control = Some(record.clone());
        shared.state.control_count += 1;
        true
    });
    if accepted {
        pump.publish(payload.len(), || DriverEventKind::Control(record));
    }
    Ok(())
}

fn lifecycle(pump: &Pump, line: &[u8]) -> DriverResult<()> {
    let inner = &pump.inner;
    let text = std::str::from_utf8(line)
        .map_err(|_| Reason::Lifecycle.error("invalid lifecycle UTF-8"))?;
    let record = if text == "DST_Master_Ready" || text.starts_with("DST_Master_Ready|") {
        inner.change(|shared| {
            shared.state.native_ready = true;
            recompute_ready(&mut shared.state);
        });
        json!({"event": "ready", "detail": text.split_once('|').map_or("", |(_, detail)| detail)})
    } else if let Some(session) = text.strip_prefix("DST_SessionId|") {
        if session.is_empty() || session.len() > 128 {
            return Err(Reason::Lifecycle.error("invalid native session identity"));
        }
        inner.change(|shared| {
            shared.state.session_id = Some(session.into());
            shared.state.native_ready = true;
            recompute_ready(&mut shared.state);
        });
        json!({"event": "session", "session_id": session})
    } else if text == "DST_Saved" || text.starts_with("DST_Saved|") {
        let path = text.split_once('|').map_or("", |(_, path)| path);
        let snapshot = path
            .rsplit('/')
            .next()
            .and_then(|value| value.parse::<u64>().ok());
        let record = json!({"event": "saved", "path": path, "snapshot": snapshot});
        inner.change(|shared| shared.state.last_native_save = Some(record.clone()));
        record
    } else if text == "DST_Stopping" || text == "DST_Shutdown" {
        inner.change(|shared| {
            shared.state.stopping = true;
            shared.state.ready = false;
        });
        json!({"event": if text == "DST_Stopping" { "stopping" } else { "shutdown" }})
    } else {
        json!({"event": "unknown", "line": text})
    };
    pump.publish(line.len(), || DriverEventKind::Lifecycle(record));
    Ok(())
}
