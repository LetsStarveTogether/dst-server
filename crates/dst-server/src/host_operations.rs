//! Shared host operations for the CLI and Python SDK.

use std::{collections::BTreeSet, time::Duration};

use anyhow::{Result, ensure};
use futures::{StreamExt, stream};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    host::{Host, PermissionKind},
    model::{Countdown, Request},
    policy::Policy,
    rooms::Room,
};

fn yes() -> bool {
    true
}
fn one() -> u64 {
    1
}
fn interval() -> f64 {
    1.0
}
fn timeout() -> f64 {
    900.0
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Permission {
    Admin,
    Whitelist,
    Ban,
}
impl From<Permission> for PermissionKind {
    fn from(value: Permission) -> Self {
        match value {
            Permission::Admin => Self::Admin,
            Permission::Whitelist => Self::Whitelist,
            Permission::Ban => Self::Ban,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum HostOperation {
    Status {
        #[serde(default = "yes")]
        game: bool,
    },
    Diagnose,
    Start {
        #[serde(default = "yes")]
        wait: bool,
        #[serde(default = "timeout")]
        timeout: f64,
    },
    Stop {
        #[serde(default = "yes")]
        wait: bool,
        #[serde(default = "timeout")]
        timeout: f64,
    },
    Restart {
        #[serde(default = "yes")]
        wait: bool,
        #[serde(default = "timeout")]
        timeout: f64,
    },
    WaitReady {
        #[serde(default = "timeout")]
        timeout: f64,
    },
    Announce {
        message: String,
        #[serde(default = "one")]
        count: u64,
        #[serde(default = "interval")]
        interval: f64,
    },
    UpdateMods {
        #[serde(default)]
        restart: bool,
        #[serde(default)]
        notice: Option<Countdown>,
    },
    Permission {
        kind: Permission,
        #[serde(default)]
        userid: Option<String>,
        #[serde(default)]
        remove: bool,
    },
    SetPolicy {
        policy: Policy,
    },
    Edit {
        #[serde(default)]
        changes: Vec<(String, Value)>,
        #[serde(default)]
        unset: Vec<String>,
    },
    Call {
        request: Request,
    },
}

pub fn duration(seconds: f64) -> Result<Duration> {
    Ok(crate::model::positive_duration("timeout", seconds)?)
}

impl HostOperation {
    pub fn validate(&self) -> Result<()> {
        match self {
            Self::Start { timeout, .. }
            | Self::Stop { timeout, .. }
            | Self::Restart { timeout, .. }
            | Self::WaitReady { timeout } => {
                duration(*timeout)?;
            }
            Self::Announce {
                message,
                count,
                interval,
            } => Request::Announce {
                message: message.clone(),
                count: *count,
                interval: *interval,
            }
            .validate()?,
            Self::UpdateMods { restart, notice } => Request::UpdateMods {
                restart: *restart,
                notice: notice.clone(),
            }
            .validate()?,
            Self::Permission {
                userid: Some(userid),
                ..
            } => Request::IsAdmin {
                userid: userid.clone(),
            }
            .validate()?,
            Self::SetPolicy { policy } => policy.validate()?,
            Self::Edit { changes, unset } => ensure!(
                !changes.is_empty() || !unset.is_empty(),
                "provide changes or unset fields"
            ),
            Self::Call { request } => request.validate()?,
            _ => {}
        }
        Ok(())
    }
}

pub fn outcome(number: u16, result: Result<Value>) -> Value {
    match result {
        Ok(value) => json!({"number":number,"result":{"ok":true,"value":value}}),
        Err(error) => {
            let error = error
                .downcast_ref::<crate::model::Error>()
                .and_then(|error| serde_json::to_value(error).ok())
                .unwrap_or_else(|| json!({"code":"invalid","message":error.to_string()}));
            json!({"number":number,"result":{"ok":false,"error":error}})
        }
    }
}

impl Host {
    pub async fn edit_fields(
        &self,
        number: u16,
        changes: &[(String, Value)],
        unset: &[String],
    ) -> Result<Room> {
        let definition = self.load(number).await?;
        let changes: Vec<_> = changes
            .iter()
            .map(|(pointer, value)| (pointer.as_str(), value.clone()))
            .collect();
        let unset: Vec<_> = unset.iter().map(String::as_str).collect();
        self.edit(&definition.edit_many(&changes, &unset)?).await
    }

    pub async fn operate(&self, number: u16, operation: &HostOperation) -> Result<Value> {
        operation.validate()?;
        self.rooms.path(number)?;
        match operation {
            HostOperation::Status { game } => self.status(number, *game).await,
            HostOperation::Diagnose => self.diagnose(number).await,
            HostOperation::Start { wait, timeout } => {
                self.start(number, *wait, duration(*timeout)?).await
            }
            HostOperation::Stop { wait, timeout } => {
                self.stop(number, *wait, duration(*timeout)?).await
            }
            HostOperation::Restart { wait, timeout } => {
                self.restart(number, *wait, duration(*timeout)?).await
            }
            HostOperation::WaitReady { timeout } => {
                self.wait_ready(number, duration(*timeout)?).await
            }
            HostOperation::Announce {
                message,
                count,
                interval,
            } => {
                self.announce(number, message.clone(), *count, *interval)
                    .await
            }
            HostOperation::UpdateMods { restart, notice } => {
                self.update_mods(number, *restart, notice.clone()).await
            }
            HostOperation::Permission {
                kind,
                userid,
                remove,
            } => {
                self.permission(number, (*kind).into(), userid.as_deref(), *remove)
                    .await
            }
            HostOperation::SetPolicy { policy } => {
                self.set_policy(number, policy).await?;
                Ok(Value::Null)
            }
            HostOperation::Edit { changes, unset } => Ok(serde_json::to_value(
                self.edit_fields(number, changes, unset).await?,
            )?),
            HostOperation::Call { request } => self.call(number, request.clone()).await,
        }
    }

    fn validate_batch(&self, numbers: &[u16]) -> Result<()> {
        ensure!(numbers.len() <= 1024, "at most 1024 rooms are allowed");
        let mut unique = BTreeSet::new();
        for number in numbers {
            self.rooms.path(*number)?;
            ensure!(unique.insert(*number), "duplicate room number {number}");
        }
        Ok(())
    }

    pub async fn batch(&self, numbers: &[u16], operation: &HostOperation) -> Result<Vec<Value>> {
        self.validate_batch(numbers)?;
        operation.validate()?;
        Ok(stream::iter(
            numbers.iter().copied().map(|number| async move {
                outcome(number, self.operate(number, operation).await)
            }),
        )
        .buffered(8)
        .collect()
        .await)
    }

    pub async fn provision(&self, definitions: &[Room]) -> Result<Vec<Value>> {
        self.validate_batch(
            &definitions
                .iter()
                .map(|room| room.number)
                .collect::<Vec<_>>(),
        )?;
        for definition in definitions {
            definition.validate()?;
        }
        Ok(
            stream::iter(definitions.iter().cloned().map(|definition| async move {
                outcome(
                    definition.number,
                    self.create(&definition)
                        .await
                        .and_then(|room| Ok(serde_json::to_value(room)?)),
                )
            }))
            .buffered(8)
            .collect()
            .await,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn invalid_batches_do_not_contact_systemd_and_failures_are_per_room() {
        let temporary = tempfile::tempdir().unwrap();
        let mut host = Host::new(temporary.path(), temporary.path().join("units"));
        host.systemd.executable = temporary.path().join("missing-systemctl");
        let status = HostOperation::Status { game: false };
        assert!(host.batch(&[1, 1], &status).await.is_err());
        assert!(host.batch(&[300], &status).await.is_err());
        assert!(
            host.batch(
                &[1],
                &HostOperation::Start {
                    wait: false,
                    timeout: f64::NAN
                }
            )
            .await
            .is_err()
        );
        let results = host.batch(&[2, 1], &status).await.unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0]["number"], 2);
        assert_eq!(results[1]["number"], 1);
        assert!(results.iter().all(|result| result["result"]["ok"] == false));
        assert!(
            serde_json::from_value::<HostOperation>(json!({"operation":"stop","unexpected":true}))
                .is_err()
        );
    }
}
