//! Native RPC fields; serde_json::Value is only an in-memory SDK value here.

use capnp::message::{Builder, HeapAllocator, TypedBuilder};
use serde_json::{Map, Number, Value};

use crate::model::{Countdown, Envelope, Error, ErrorCode, Request, Target};
use crate::room_capnp as wire;

use super::{MAX_MESSAGE, Result};

const MAX_DEPTH: usize = 64;

pub(super) type ValueMessage = TypedBuilder<wire::value::Owned>;
pub(super) type EnvelopeMessage = TypedBuilder<wire::envelope::Owned>;

fn message<T: capnp::traits::Owned>() -> TypedBuilder<T> {
    TypedBuilder::new(Builder::new(HeapAllocator::new().first_segment_words(64)))
}

fn invalid(error: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Invalid, error.to_string())
}

fn text(value: capnp::Result<capnp::text::Reader<'_>>) -> Result<String> {
    Ok(value
        .map_err(invalid)?
        .to_str()
        .map_err(invalid)?
        .to_owned())
}

pub(super) fn check_size(size: capnp::MessageSize, limit: usize) -> Result<()> {
    if size.word_count > (limit / 8) as u64 || size.cap_count != 0 {
        return Err(Error::new(
            ErrorCode::Overflow,
            "RPC value exceeds its size limit",
        ));
    }
    Ok(())
}

// Bound expanded values even when the wire uses compact, default-valued structs.
struct Budget(usize);

impl Budget {
    fn new(bytes: usize) -> Self {
        Self(bytes)
    }

    fn take(&mut self, bytes: usize) -> Result<()> {
        self.0 = self
            .0
            .checked_sub(bytes)
            .ok_or_else(|| Error::new(ErrorCode::Overflow, "RPC value exceeds its size limit"))?;
        Ok(())
    }

    fn text(&mut self, value: &str) -> Result<()> {
        self.take(value.len().saturating_add(8) & !7)
    }

    fn list(&mut self, length: usize, item_bytes: usize) -> Result<()> {
        if length > self.0 / item_bytes {
            return Err(Error::new(
                ErrorCode::Overflow,
                "RPC list exceeds its size limit",
            ));
        }
        self.take(8)
    }
}

fn check_depth(depth: usize) -> Result<()> {
    if depth > MAX_DEPTH {
        Err(Error::new(
            ErrorCode::Overflow,
            "RPC value exceeds 64 nesting levels",
        ))
    } else {
        Ok(())
    }
}

pub(super) fn encode_value(value: &Value, limit: usize) -> Result<ValueMessage> {
    let mut encoded = message::<wire::value::Owned>();
    write_value(encoded.init_root(), value, &mut Budget::new(limit), 0)?;
    check_size(
        encoded
            .get_root_as_reader()
            .map_err(invalid)?
            .total_size()
            .map_err(invalid)?,
        limit,
    )?;
    Ok(encoded)
}

fn write_value(
    mut builder: wire::value::Builder<'_>,
    value: &Value,
    budget: &mut Budget,
    depth: usize,
) -> Result<()> {
    check_depth(depth)?;
    budget.take(24)?;
    match value {
        Value::Null => builder.set_null(()),
        Value::Bool(value) => builder.set_boolean(*value),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                builder.set_signed(value);
            } else if let Some(value) = value.as_u64() {
                builder.set_unsigned(value);
            } else if let Some(float) = value.as_f64()
                && Number::from_f64(float).as_ref() == Some(value)
            {
                builder.set_float(float);
            } else {
                let decimal = value.to_string();
                budget.text(&decimal)?;
                builder.set_decimal(decimal.as_str());
            }
        }
        Value::String(value) => {
            budget.text(value)?;
            builder.set_text(value.as_str());
        }
        Value::Array(values) => {
            budget.list(values.len(), 24)?;
            let mut list = builder.init_list(values.len() as u32);
            for (index, value) in values.iter().enumerate() {
                write_value(list.reborrow().get(index as u32), value, budget, depth + 1)?;
            }
        }
        Value::Object(values) => {
            budget.list(values.len(), 40)?;
            budget.take(values.len() * 16)?;
            let mut entries = builder.init_object(values.len() as u32);
            for (index, (key, value)) in values.iter().enumerate() {
                budget.text(key)?;
                let mut entry = entries.reborrow().get(index as u32);
                entry.set_key(key.as_str());
                write_value(entry.init_value(), value, budget, depth + 1)?;
            }
        }
    }
    Ok(())
}

pub(super) fn read_value(reader: wire::value::Reader<'_>) -> Result<Value> {
    read_value_with_budget(reader, &mut Budget::new(MAX_MESSAGE), 0)
}

pub(super) fn read_values(
    values: capnp::struct_list::Reader<'_, wire::value::Owned>,
) -> Result<Vec<Value>> {
    read_list(values, &mut Budget::new(MAX_MESSAGE), 0)
}

fn read_list(
    values: capnp::struct_list::Reader<'_, wire::value::Owned>,
    budget: &mut Budget,
    depth: usize,
) -> Result<Vec<Value>> {
    budget.list(values.len() as usize, 24)?;
    values
        .iter()
        .map(|value| read_value_with_budget(value, budget, depth))
        .collect()
}

fn read_value_with_budget(
    reader: wire::value::Reader<'_>,
    budget: &mut Budget,
    depth: usize,
) -> Result<Value> {
    use wire::value::Which;
    check_depth(depth)?;
    budget.take(24)?;
    Ok(match reader.which().map_err(invalid)? {
        Which::Null(()) => Value::Null,
        Which::Boolean(value) => Value::Bool(value),
        Which::Signed(value) => value.into(),
        Which::Unsigned(value) => value.into(),
        Which::Float(value) => Number::from_f64(value)
            .map(Value::Number)
            .ok_or_else(|| invalid("RPC numbers must be finite"))?,
        Which::Text(value) => {
            let value = text(value)?;
            budget.text(&value)?;
            Value::String(value)
        }
        Which::Decimal(value) => {
            let value = text(value)?;
            budget.text(&value)?;
            Value::Number(value.parse::<Number>().map_err(invalid)?)
        }
        Which::List(values) => {
            Value::Array(read_list(values.map_err(invalid)?, budget, depth + 1)?)
        }
        Which::Object(entries) => {
            Value::Object(read_entries(entries.map_err(invalid)?, budget, depth)?)
        }
    })
}

fn read_entries(
    entries: capnp::struct_list::Reader<'_, wire::value::entry::Owned>,
    budget: &mut Budget,
    depth: usize,
) -> Result<Map<String, Value>> {
    budget.list(entries.len() as usize, 40)?;
    budget.take(entries.len() as usize * 16)?;
    let mut values = Map::new();
    for entry in entries {
        let key = text(entry.get_key())?;
        budget.text(&key)?;
        if values.contains_key(&key) {
            return Err(invalid("duplicate RPC object key"));
        }
        values.insert(
            key,
            read_value_with_budget(entry.get_value().map_err(invalid)?, budget, depth + 1)?,
        );
    }
    Ok(values)
}

pub(super) fn encode_envelope(envelope: &Envelope) -> Result<EnvelopeMessage> {
    envelope.validate()?;
    let mut encoded = message::<wire::envelope::Owned>();
    let mut builder: wire::envelope::Builder<'_> = encoded.init_root();
    match &envelope.target {
        Target::Room => builder.reborrow().init_target().set_room(()),
        Target::Shard(name) => builder.reborrow().init_target().set_shard(name.as_str()),
    }
    write_request(builder.reborrow().init_request(), &envelope.request)?;
    if let Some(timeout) = envelope.timeout {
        builder.init_timeout().set_value(timeout);
    }
    check_size(
        encoded
            .get_root_as_reader()
            .map_err(invalid)?
            .total_size()
            .map_err(invalid)?,
        MAX_MESSAGE,
    )?;
    Ok(encoded)
}

pub(super) fn read_envelope(reader: wire::envelope::Reader<'_>) -> Result<Envelope> {
    check_size(reader.total_size().map_err(invalid)?, MAX_MESSAGE)?;
    // A missing envelope must not acquire the first command's default discriminant.
    if !reader.has_target() || !reader.has_request() {
        return Err(invalid("RPC request requires a target and command"));
    }
    let target = match reader
        .get_target()
        .map_err(invalid)?
        .which()
        .map_err(invalid)?
    {
        wire::target::Room(()) => Target::Room,
        wire::target::Shard(name) => Target::Shard(text(name)?),
    };
    let envelope = Envelope {
        target,
        request: read_request(reader.get_request().map_err(invalid)?)?,
        timeout: if reader.has_timeout() {
            Some(reader.get_timeout().map_err(invalid)?.get_value())
        } else {
            None
        },
    };
    envelope.validate()?;
    Ok(envelope)
}

fn write_countdown(mut builder: wire::countdown::Builder<'_>, value: &Countdown) -> Result<()> {
    builder.set_template(value.template.as_str());
    builder.set_delay(value.delay);
    builder.set_interval(value.interval);
    let mut budget = Budget::new(MAX_MESSAGE);
    budget.list(value.parameters.len(), 40)?;
    budget.take(value.parameters.len() * 16)?;
    let mut entries = builder.init_parameters(value.parameters.len() as u32);
    for (index, (key, value)) in value.parameters.iter().enumerate() {
        budget.text(key)?;
        let mut entry = entries.reborrow().get(index as u32);
        entry.set_key(key.as_str());
        write_value(entry.init_value(), value, &mut budget, 0)?;
    }
    Ok(())
}

fn read_countdown(reader: wire::countdown::Reader<'_>) -> Result<Countdown> {
    Ok(Countdown {
        template: text(reader.get_template())?,
        delay: reader.get_delay(),
        interval: reader.get_interval(),
        parameters: read_entries(
            reader.get_parameters().map_err(invalid)?,
            &mut Budget::new(MAX_MESSAGE),
            0,
        )?
        .into_iter()
        .collect(),
    })
}

macro_rules! error_codes {
    ($($variant:ident),* $(,)?) => {
        fn write_error_code(code: ErrorCode) -> wire::ErrorCode {
            match code { $(ErrorCode::$variant => wire::ErrorCode::$variant),* }
        }
        fn read_error_code(code: wire::ErrorCode) -> ErrorCode {
            match code { $(wire::ErrorCode::$variant => ErrorCode::$variant),* }
        }
    };
}

error_codes! {
    Invalid, NotReady, Busy, Unsupported, NotFound, Conflict, PartialFailure, Unknown,
    Transport, Timeout, Overflow, StaleReference, Lua, Protocol, Internal,
}

fn write_error(mut builder: wire::error::Builder<'_>, error: &Error) -> Result<()> {
    let mut budget = Budget::new(MAX_MESSAGE);
    budget.text(&error.message)?;
    builder.set_code(write_error_code(error.code));
    builder.set_message(error.message.as_str());
    write_value(builder.init_details(), &error.details, &mut budget, 0)
}

pub(super) fn read_error(reader: wire::error::Reader<'_>) -> Result<Error> {
    Ok(Error {
        code: read_error_code(reader.get_code().map_err(invalid)?),
        message: text(reader.get_message())?,
        details: read_value(reader.get_details().map_err(invalid)?)?,
    })
}

pub(super) fn encode_outcome(result: &Result<Value>) -> Result<TypedBuilder<wire::outcome::Owned>> {
    let mut encoded = message::<wire::outcome::Owned>();
    let builder: wire::outcome::Builder<'_> = encoded.init_root();
    match result {
        Ok(value) => write_value(
            builder.init_value(),
            value,
            &mut Budget::new(MAX_MESSAGE),
            0,
        )?,
        Err(error) => write_error(builder.init_error(), error)?,
    }
    check_size(
        encoded
            .get_root_as_reader()
            .map_err(invalid)?
            .total_size()
            .map_err(invalid)?,
        MAX_MESSAGE,
    )?;
    Ok(encoded)
}
fn write_request(mut builder: wire::request::Builder<'_>, request: &Request) -> Result<()> {
    match request {
        Request::Status {} => builder.set_status(()),
        Request::Start {} => builder.set_start(()),
        Request::Stop { notice } => {
            let mut args = builder.init_stop();
            if let Some(value) = notice {
                write_countdown(args.reborrow().init_notice(), value)?;
            }
        }
        Request::Restart { notice } => {
            let mut args = builder.init_restart();
            if let Some(value) = notice {
                write_countdown(args.reborrow().init_notice(), value)?;
            }
        }
        Request::Kill {} => builder.set_kill(()),
        Request::UpdateMods { restart, notice } => {
            let mut args = builder.init_update_mods();
            args.set_restart(*restart);
            if let Some(value) = notice {
                write_countdown(args.reborrow().init_notice(), value)?;
            }
        }
        Request::ReadConfiguration {} => builder.set_read_configuration(()),
        Request::Configure {
            configuration,
            replace_permissions,
        } => {
            let mut args = builder.init_configure();
            write_value(
                args.reborrow().init_configuration(),
                configuration,
                &mut Budget::new(MAX_MESSAGE),
                0,
            )?;
            args.set_replace_permissions(*replace_permissions);
        }
        Request::SetPolicy { policy } => {
            let mut args = builder.init_set_policy();
            write_value(
                args.reborrow().init_policy(),
                policy,
                &mut Budget::new(MAX_MESSAGE),
                0,
            )?;
        }
        Request::Execute { source } => {
            let mut args = builder.init_execute();
            args.set_source(source.as_str());
        }
        Request::ExecuteJson { source } => {
            let mut args = builder.init_execute_json();
            args.set_source(source.as_str());
        }
        Request::Evaluate { source } => {
            let mut args = builder.init_evaluate();
            args.set_source(source.as_str());
        }
        Request::ExecuteAll { source } => {
            let mut args = builder.init_execute_all();
            args.set_source(source.as_str());
        }
        Request::Announce {
            message,
            count,
            interval,
        } => {
            let mut args = builder.init_announce();
            args.set_message(message.as_str());
            args.set_count(*count);
            args.set_interval(*interval);
        }
        Request::Save {} => builder.set_save(()),
        Request::Pause { paused } => {
            let mut args = builder.init_pause();
            args.set_paused(*paused);
        }
        Request::Reset {} => builder.set_reset(()),
        Request::Rollback { count } => {
            let mut args = builder.init_rollback();
            args.set_count(*count);
        }
        Request::Regenerate {
            expected_session_id,
            require_empty,
        } => {
            let mut args = builder.init_regenerate();
            if let Some(value) = expected_session_id {
                args.set_expected_session_id(value.as_str());
            }
            if let Some(value) = require_empty {
                args.reborrow().init_require_empty().set_value(*value);
            }
        }
        Request::RegenerateShard { preserve_settings } => {
            let mut args = builder.init_regenerate_shard();
            args.set_preserve_settings(*preserve_settings);
        }
        Request::Snapshots { limit, before } => {
            let mut args = builder.init_snapshots();
            args.set_limit(*limit);
            if let Some(value) = before {
                args.reborrow().init_before().set_value(*value);
            }
        }
        Request::RollbackToDay { day } => {
            let mut args = builder.init_rollback_to_day();
            args.set_day(*day);
        }
        Request::RollbackToSnapshot {
            session_id,
            snapshot_id,
        } => {
            let mut args = builder.init_rollback_to_snapshot();
            args.set_session_id(session_id.as_str());
            args.set_snapshot_id(*snapshot_id);
        }
        Request::Health {} => builder.set_health(()),
        Request::Presence {} => builder.set_presence(()),
        Request::Room {} => builder.set_room(()),
        Request::World {} => builder.set_world(()),
        Request::Runtime {} => builder.set_runtime(()),
        Request::Mods {} => builder.set_mods(()),
        Request::ConnectedShards {} => builder.set_connected_shards(()),
        Request::ListPlayers {} => builder.set_list_players(()),
        Request::GetPlayer { userid } => {
            let mut args = builder.init_get_player();
            args.set_userid(userid.as_str());
        }
        Request::Inventory { userid } => {
            let mut args = builder.init_inventory();
            args.set_userid(userid.as_str());
        }
        Request::Kick { userid } => {
            let mut args = builder.init_kick();
            args.set_userid(userid.as_str());
        }
        Request::Ban { userid, seconds } => {
            let mut args = builder.init_ban();
            args.set_userid(userid.as_str());
            if let Some(value) = seconds {
                args.reborrow().init_seconds().set_value(*value);
            }
        }
        Request::Blocklist {} => builder.set_blocklist(()),
        Request::IsBlocked { userid } => {
            let mut args = builder.init_is_blocked();
            args.set_userid(userid.as_str());
        }
        Request::Unban { userid } => {
            let mut args = builder.init_unban();
            args.set_userid(userid.as_str());
        }
        Request::IsAdmin { userid } => {
            let mut args = builder.init_is_admin();
            args.set_userid(userid.as_str());
        }
        Request::ReadPermissions {} => builder.set_read_permissions(()),
        Request::SetAdmin { userid, remove } => {
            let mut args = builder.init_set_admin();
            args.set_userid(userid.as_str());
            args.set_remove(*remove);
        }
        Request::SetVitals {
            userid,
            health,
            hunger,
            sanity,
            temperature,
            moisture,
        } => {
            let mut args = builder.init_set_vitals();
            args.set_userid(userid.as_str());
            if let Some(value) = health {
                args.reborrow().init_health().set_value(*value);
            }
            if let Some(value) = hunger {
                args.reborrow().init_hunger().set_value(*value);
            }
            if let Some(value) = sanity {
                args.reborrow().init_sanity().set_value(*value);
            }
            if let Some(value) = temperature {
                args.reborrow().init_temperature().set_value(*value);
            }
            if let Some(value) = moisture {
                args.reborrow().init_moisture().set_value(*value);
            }
        }
        Request::KillPlayer { userid } => {
            let mut args = builder.init_kill_player();
            args.set_userid(userid.as_str());
        }
        Request::Revive { userid } => {
            let mut args = builder.init_revive();
            args.set_userid(userid.as_str());
        }
        Request::Despawn { userid } => {
            let mut args = builder.init_despawn();
            args.set_userid(userid.as_str());
        }
        Request::Migrate {
            userid,
            shard_id,
            portal_id,
        } => {
            let mut args = builder.init_migrate();
            args.set_userid(userid.as_str());
            args.set_shard_id(shard_id.as_str());
            args.set_portal_id(*portal_id);
        }
        Request::Teleport { userid, x, y, z } => {
            let mut args = builder.init_teleport();
            args.set_userid(userid.as_str());
            args.set_x(*x);
            args.set_y(*y);
            args.set_z(*z);
        }
        Request::Give {
            userid,
            item,
            count,
        } => {
            let mut args = builder.init_give();
            args.set_userid(userid.as_str());
            args.set_item(item.as_str());
            args.set_count(*count);
        }
        Request::Remove {
            userid,
            item,
            count,
        } => {
            let mut args = builder.init_remove();
            args.set_userid(userid.as_str());
            args.set_item(item.as_str());
            args.set_count(*count);
        }
        Request::IsWhitelisted { userid } => {
            let mut args = builder.init_is_whitelisted();
            args.set_userid(userid.as_str());
        }
        Request::Whitelist { userid } => {
            let mut args = builder.init_whitelist();
            args.set_userid(userid.as_str());
        }
        Request::Unwhitelist { userid } => {
            let mut args = builder.init_unwhitelist();
            args.set_userid(userid.as_str());
        }
    }
    Ok(())
}

fn read_request(reader: wire::request::Reader<'_>) -> Result<Request> {
    use wire::request::Which;
    Ok(match reader.which().map_err(invalid)? {
        Which::Status(()) => Request::Status {},
        Which::Start(()) => Request::Start {},
        Which::Stop(args) => {
            let args = args.map_err(invalid)?;
            Request::Stop {
                notice: if args.has_notice() {
                    Some(read_countdown(args.get_notice().map_err(invalid)?)?)
                } else {
                    None
                },
            }
        }
        Which::Restart(args) => {
            let args = args.map_err(invalid)?;
            Request::Restart {
                notice: if args.has_notice() {
                    Some(read_countdown(args.get_notice().map_err(invalid)?)?)
                } else {
                    None
                },
            }
        }
        Which::Kill(()) => Request::Kill {},
        Which::UpdateMods(args) => {
            let args = args.map_err(invalid)?;
            Request::UpdateMods {
                restart: args.get_restart(),
                notice: if args.has_notice() {
                    Some(read_countdown(args.get_notice().map_err(invalid)?)?)
                } else {
                    None
                },
            }
        }
        Which::ReadConfiguration(()) => Request::ReadConfiguration {},
        Which::Configure(args) => {
            let args = args.map_err(invalid)?;
            Request::Configure {
                configuration: read_value(args.get_configuration().map_err(invalid)?)?,
                replace_permissions: args.get_replace_permissions(),
            }
        }
        Which::SetPolicy(args) => {
            let args = args.map_err(invalid)?;
            Request::SetPolicy {
                policy: read_value(args.get_policy().map_err(invalid)?)?,
            }
        }
        Which::Execute(args) => {
            let args = args.map_err(invalid)?;
            Request::Execute {
                source: text(args.get_source())?,
            }
        }
        Which::ExecuteJson(args) => {
            let args = args.map_err(invalid)?;
            Request::ExecuteJson {
                source: text(args.get_source())?,
            }
        }
        Which::Evaluate(args) => {
            let args = args.map_err(invalid)?;
            Request::Evaluate {
                source: text(args.get_source())?,
            }
        }
        Which::ExecuteAll(args) => {
            let args = args.map_err(invalid)?;
            Request::ExecuteAll {
                source: text(args.get_source())?,
            }
        }
        Which::Announce(args) => {
            let args = args.map_err(invalid)?;
            Request::Announce {
                message: text(args.get_message())?,
                count: args.get_count(),
                interval: args.get_interval(),
            }
        }
        Which::Save(()) => Request::Save {},
        Which::Pause(args) => {
            let args = args.map_err(invalid)?;
            Request::Pause {
                paused: args.get_paused(),
            }
        }
        Which::Reset(()) => Request::Reset {},
        Which::Rollback(args) => {
            let args = args.map_err(invalid)?;
            Request::Rollback {
                count: args.get_count(),
            }
        }
        Which::Regenerate(args) => {
            let args = args.map_err(invalid)?;
            Request::Regenerate {
                expected_session_id: if args.has_expected_session_id() {
                    Some(text(args.get_expected_session_id())?)
                } else {
                    None
                },
                require_empty: if args.has_require_empty() {
                    Some(args.get_require_empty().map_err(invalid)?.get_value())
                } else {
                    None
                },
            }
        }
        Which::RegenerateShard(args) => {
            let args = args.map_err(invalid)?;
            Request::RegenerateShard {
                preserve_settings: args.get_preserve_settings(),
            }
        }
        Which::Snapshots(args) => {
            let args = args.map_err(invalid)?;
            Request::Snapshots {
                limit: args.get_limit(),
                before: if args.has_before() {
                    Some(args.get_before().map_err(invalid)?.get_value())
                } else {
                    None
                },
            }
        }
        Which::RollbackToDay(args) => {
            let args = args.map_err(invalid)?;
            Request::RollbackToDay {
                day: args.get_day(),
            }
        }
        Which::RollbackToSnapshot(args) => {
            let args = args.map_err(invalid)?;
            Request::RollbackToSnapshot {
                session_id: text(args.get_session_id())?,
                snapshot_id: args.get_snapshot_id(),
            }
        }
        Which::Health(()) => Request::Health {},
        Which::Presence(()) => Request::Presence {},
        Which::Room(()) => Request::Room {},
        Which::World(()) => Request::World {},
        Which::Runtime(()) => Request::Runtime {},
        Which::Mods(()) => Request::Mods {},
        Which::ConnectedShards(()) => Request::ConnectedShards {},
        Which::ListPlayers(()) => Request::ListPlayers {},
        Which::GetPlayer(args) => {
            let args = args.map_err(invalid)?;
            Request::GetPlayer {
                userid: text(args.get_userid())?,
            }
        }
        Which::Inventory(args) => {
            let args = args.map_err(invalid)?;
            Request::Inventory {
                userid: text(args.get_userid())?,
            }
        }
        Which::Kick(args) => {
            let args = args.map_err(invalid)?;
            Request::Kick {
                userid: text(args.get_userid())?,
            }
        }
        Which::Ban(args) => {
            let args = args.map_err(invalid)?;
            Request::Ban {
                userid: text(args.get_userid())?,
                seconds: if args.has_seconds() {
                    Some(args.get_seconds().map_err(invalid)?.get_value())
                } else {
                    None
                },
            }
        }
        Which::Blocklist(()) => Request::Blocklist {},
        Which::IsBlocked(args) => {
            let args = args.map_err(invalid)?;
            Request::IsBlocked {
                userid: text(args.get_userid())?,
            }
        }
        Which::Unban(args) => {
            let args = args.map_err(invalid)?;
            Request::Unban {
                userid: text(args.get_userid())?,
            }
        }
        Which::IsAdmin(args) => {
            let args = args.map_err(invalid)?;
            Request::IsAdmin {
                userid: text(args.get_userid())?,
            }
        }
        Which::ReadPermissions(()) => Request::ReadPermissions {},
        Which::SetAdmin(args) => {
            let args = args.map_err(invalid)?;
            Request::SetAdmin {
                userid: text(args.get_userid())?,
                remove: args.get_remove(),
            }
        }
        Which::SetVitals(args) => {
            let args = args.map_err(invalid)?;
            Request::SetVitals {
                userid: text(args.get_userid())?,
                health: if args.has_health() {
                    Some(args.get_health().map_err(invalid)?.get_value())
                } else {
                    None
                },
                hunger: if args.has_hunger() {
                    Some(args.get_hunger().map_err(invalid)?.get_value())
                } else {
                    None
                },
                sanity: if args.has_sanity() {
                    Some(args.get_sanity().map_err(invalid)?.get_value())
                } else {
                    None
                },
                temperature: if args.has_temperature() {
                    Some(args.get_temperature().map_err(invalid)?.get_value())
                } else {
                    None
                },
                moisture: if args.has_moisture() {
                    Some(args.get_moisture().map_err(invalid)?.get_value())
                } else {
                    None
                },
            }
        }
        Which::KillPlayer(args) => {
            let args = args.map_err(invalid)?;
            Request::KillPlayer {
                userid: text(args.get_userid())?,
            }
        }
        Which::Revive(args) => {
            let args = args.map_err(invalid)?;
            Request::Revive {
                userid: text(args.get_userid())?,
            }
        }
        Which::Despawn(args) => {
            let args = args.map_err(invalid)?;
            Request::Despawn {
                userid: text(args.get_userid())?,
            }
        }
        Which::Migrate(args) => {
            let args = args.map_err(invalid)?;
            Request::Migrate {
                userid: text(args.get_userid())?,
                shard_id: text(args.get_shard_id())?,
                portal_id: args.get_portal_id(),
            }
        }
        Which::Teleport(args) => {
            let args = args.map_err(invalid)?;
            Request::Teleport {
                userid: text(args.get_userid())?,
                x: args.get_x(),
                y: args.get_y(),
                z: args.get_z(),
            }
        }
        Which::Give(args) => {
            let args = args.map_err(invalid)?;
            Request::Give {
                userid: text(args.get_userid())?,
                item: text(args.get_item())?,
                count: args.get_count(),
            }
        }
        Which::Remove(args) => {
            let args = args.map_err(invalid)?;
            Request::Remove {
                userid: text(args.get_userid())?,
                item: text(args.get_item())?,
                count: args.get_count(),
            }
        }
        Which::IsWhitelisted(args) => {
            let args = args.map_err(invalid)?;
            Request::IsWhitelisted {
                userid: text(args.get_userid())?,
            }
        }
        Which::Whitelist(args) => {
            let args = args.map_err(invalid)?;
            Request::Whitelist {
                userid: text(args.get_userid())?,
            }
        }
        Which::Unwhitelist(args) => {
            let args = args.map_err(invalid)?;
            Request::Unwhitelist {
                userid: text(args.get_userid())?,
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn numbers_and_empty_values_survive_native_encoding() {
        let value = json!({
            "empty": [null, false, 0, "", [], {}],
            "numbers": [i64::MIN, u64::MAX, 9_007_199_254_740_993_u64, 0.25, -0.0],
            "large": "123456789012345678901234567890".parse::<Number>().unwrap(),
            "decimal": "0.12345678901234567890123456789".parse::<Number>().unwrap(),
            "text": "洞穴\u{0}🌲",
        });
        let encoded = encode_value(&value, MAX_MESSAGE).unwrap();
        let bytes = capnp::serialize::write_message_to_words(encoded.borrow_inner());
        let reader =
            capnp::serialize::read_message(&bytes[..], super::super::READER_OPTIONS).unwrap();
        let restored = read_value(reader.get_root().unwrap()).unwrap();
        assert_eq!(restored, value);
        assert!(restored["numbers"][4].as_f64().unwrap().is_sign_negative());

        let scalar = encode_value(&json!(0.25), MAX_MESSAGE).unwrap();
        assert!(matches!(
            scalar.get_root_as_reader().unwrap().which().unwrap(),
            wire::value::Float(0.25)
        ));
        let integer = encode_value(&json!(u64::MAX), MAX_MESSAGE).unwrap();
        assert!(matches!(
            integer.get_root_as_reader().unwrap().which().unwrap(),
            wire::value::Unsigned(u64::MAX)
        ));
    }

    #[test]
    fn malformed_native_fields_are_rejected_before_dispatch() {
        let mut empty = message::<wire::envelope::Owned>();
        empty.init_root();
        assert_eq!(
            read_envelope(empty.get_root_as_reader().unwrap())
                .unwrap_err()
                .code,
            ErrorCode::Invalid
        );

        let mut invalid_timeout =
            encode_envelope(&Envelope::new(Target::Room, Request::Status {}).unwrap()).unwrap();
        invalid_timeout
            .get_root()
            .unwrap()
            .init_timeout()
            .set_value(f64::NAN);
        assert_eq!(
            read_envelope(invalid_timeout.get_root_as_reader().unwrap())
                .unwrap_err()
                .code,
            ErrorCode::Invalid
        );

        let mut duplicate = message::<wire::value::Owned>();
        let mut entries = duplicate.init_root().init_object(2);
        for index in 0..2 {
            let mut entry = entries.reborrow().get(index);
            entry.set_key("same");
            entry.init_value().set_unsigned(u64::from(index));
        }
        assert_eq!(
            read_value(duplicate.get_root_as_reader().unwrap())
                .unwrap_err()
                .code,
            ErrorCode::Invalid
        );
        let mut non_finite = message::<wire::value::Owned>();
        non_finite.init_root().set_float(f64::INFINITY);
        assert_eq!(
            read_value(non_finite.get_root_as_reader().unwrap())
                .unwrap_err()
                .code,
            ErrorCode::Invalid
        );
    }

    #[test]
    fn native_limits_include_nested_containers() {
        let mut nested = Value::Null;
        for _ in 0..MAX_DEPTH {
            nested = json!({"next": nested});
        }
        let encoded = encode_value(&nested, MAX_MESSAGE).unwrap();
        let bytes = capnp::serialize::write_message_to_words(encoded.borrow_inner());
        let reader =
            capnp::serialize::read_message(&bytes[..], super::super::READER_OPTIONS).unwrap();
        assert_eq!(read_value(reader.get_root().unwrap()).unwrap(), nested);

        for invalid in [
            json!({"next": nested}),
            json!("x".repeat(MAX_MESSAGE)),
            json!(vec![false; MAX_MESSAGE / 8]),
        ] {
            assert_eq!(
                encode_value(&invalid, MAX_MESSAGE).err().unwrap().code,
                ErrorCode::Overflow
            );
        }

        let mut encoded = message::<wire::value::Owned>();
        let mut node = encoded.init_root();
        for _ in 0..=MAX_DEPTH {
            node = node.init_list(1).get(0);
        }
        node.set_null(());
        assert_eq!(
            read_value(encoded.get_root_as_reader().unwrap())
                .unwrap_err()
                .code,
            ErrorCode::Overflow
        );
    }

    #[test]
    fn compact_lists_cannot_bypass_decoded_value_limits() {
        // Cap'n Proto can upgrade a void list to default-valued structs.
        // This four-word value expands to more than 1 MiB of native Value structs.
        let mut bytes = Vec::from([0_u8, 0, 0, 0, 4, 0, 0, 0]);
        for word in [0x0001_0002_0000_0000_u64, 6, 0, (100_000_u64 << 35) | 1] {
            bytes.extend_from_slice(&word.to_le_bytes());
        }
        let reader =
            capnp::serialize::read_message(&bytes[..], super::super::READER_OPTIONS).unwrap();
        let value = reader.get_root::<wire::value::Reader<'_>>().unwrap();
        check_size(value.total_size().unwrap(), MAX_MESSAGE).unwrap();
        assert_eq!(read_value(value).unwrap_err().code, ErrorCode::Overflow);
        let wire::value::List(values) = value.which().unwrap() else {
            panic!("expected a compact list");
        };
        assert_eq!(
            read_values(values.unwrap()).unwrap_err().code,
            ErrorCode::Overflow
        );
    }

    #[test]
    fn native_defaults_preserve_settings() {
        let mut encoded = message::<wire::envelope::Owned>();
        let mut envelope = encoded.init_root();
        envelope.reborrow().init_target().set_shard("Caves");
        envelope.init_request().init_regenerate_shard();
        let request = read_envelope(encoded.get_root_as_reader().unwrap())
            .unwrap()
            .request;
        assert_eq!(
            request,
            Request::RegenerateShard {
                preserve_settings: true
            }
        );
    }
}
