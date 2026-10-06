//! Internal identities follow retained workers, independently of RPC waiters.

use crate::model::{Error, ErrorCode, OperationStatus, Request, Target};
use opentelemetry::trace::SpanContext;
use serde::Serialize;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy)]
pub(crate) struct OperationCompletion<'a> {
    pub(crate) outcome: &'static str,
    pub(crate) error: Option<&'a Error>,
}

impl<'a> From<Result<&'static str, &'a Error>> for OperationCompletion<'a> {
    fn from(result: Result<&'static str, &'a Error>) -> Self {
        let outcome = match result {
            Ok(outcome) => outcome,
            Err(error) if error.code == ErrorCode::Unknown => "unknown",
            Err(_) => "failure",
        };
        Self {
            outcome,
            error: result.err(),
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Source {
    Rpc,
    RoomApi,
    Manual,
    ModAuto,
    Schedule,
    IdleRegeneration,
    Recovery,
    Health,
    Signal,
    AgentShutdown,
}

impl Source {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Rpc => "rpc",
            Self::RoomApi => "room_api",
            Self::Manual => "manual",
            Self::ModAuto => "mod_auto",
            Self::Schedule => "schedule",
            Self::IdleRegeneration => "idle_regeneration",
            Self::Recovery => "recovery",
            Self::Health => "health",
            Self::Signal => "signal",
            Self::AgentShutdown => "agent_shutdown",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum Layer {
    Request,
    Policy,
    Room,
}

impl Layer {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Request => "request",
            Self::Policy => "policy",
            Self::Room => "room",
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct OperationContext {
    pub(crate) id: String,
    pub(crate) parent_id: Option<String>,
    pub(crate) source: Source,
    pub(crate) layer: Layer,
    pub(crate) span: Option<SpanContext>,
    pub(crate) player_roles: Vec<(&'static str, String)>,
    pub(crate) started: Instant,
}

impl OperationContext {
    pub(crate) fn new(source: Source, layer: Layer, parent: Option<&Self>) -> Self {
        Self {
            id: ulid::Ulid::generate().to_string(),
            parent_id: parent.map(|parent| parent.id.clone()),
            source,
            layer,
            span: parent.and_then(|parent| parent.span.clone()),
            player_roles: Vec::new(),
            started: Instant::now(),
        }
    }

    pub(crate) fn child(&self, layer: Layer) -> Self {
        Self::new(self.source, layer, Some(self))
    }

    pub(crate) fn with_request_players(mut self, request: &Request) -> Self {
        let target = match request {
            Request::StartVote {
                starter_userid,
                target_userid,
                ..
            } => {
                self.player_roles
                    .push(("starter_userid", starter_userid.clone()));
                target_userid.as_deref()
            }
            _ => request.userid(),
        };
        if let Some(target) = target {
            self.player_roles.push(("target_userid", target.to_owned()));
        }
        self
    }

    pub(crate) fn status(&self, method: &str, target: Target) -> OperationStatus {
        OperationStatus {
            id: self.id.clone(),
            method: method.into(),
            target,
            started_at_ns: timestamp(),
            completed_at_ns: None,
            error: None,
        }
    }
}

pub(crate) fn timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64
}
