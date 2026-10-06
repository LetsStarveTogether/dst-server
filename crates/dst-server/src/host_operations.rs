//! Shared host operations for the CLI and Python SDK.

use std::{collections::BTreeSet, time::Duration};

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    host::Host,
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

    pub async fn list(&self) -> Result<Vec<Value>> {
        self.batch(
            &self.managed_numbers()?,
            &HostOperation::Status { game: false },
        )
        .await
    }

    pub async fn batch(&self, numbers: &[u16], operation: &HostOperation) -> Result<Vec<Value>> {
        self.batch_matching(numbers, operation, None).await
    }

    pub(crate) async fn batch_matching(
        &self,
        numbers: &[u16],
        operation: &HostOperation,
        template: Option<&str>,
    ) -> Result<Vec<Value>> {
        self.validate_batch(numbers)?;
        operation.validate()?;
        Ok(crate::batch::collect(
            numbers.iter().copied().map(|number| async move {
                if let Some(template) = template {
                    match self.rooms.template(number) {
                        Ok(saved) if saved.as_deref() != Some(template) => return None,
                        Err(error) => return Some(outcome(number, Err(error))),
                        Ok(_) => {}
                    }
                }
                Some(outcome(number, self.operate(number, operation).await))
            }),
            8,
        )
        .await
        .into_iter()
        .flatten()
        .collect())
    }

    pub async fn provision(&self, definitions: &[Room]) -> Result<Vec<Value>> {
        self.validate_batch(
            &definitions
                .iter()
                .map(|room| room.number)
                .collect::<Vec<_>>(),
        )?;
        let mut batch_ports = BTreeSet::new();
        let validated: Vec<Result<&Room>> = definitions
            .iter()
            .map(|definition| {
                definition.validate()?;
                batch_ports.extend(definition.deployment.ports.iter().map(|port| port.host));
                Ok(definition)
            })
            .collect();
        let results = crate::batch::collect(
            validated
                .into_iter()
                .map(|definition| async { self.create_files(definition?, &batch_ports).await }),
            8,
        )
        .await;
        let reload = if results.iter().any(Result::is_ok) {
            self.systemd.reload().await
        } else {
            Ok(())
        };
        Ok(definitions
            .iter()
            .zip(results)
            .map(|(definition, result)| {
                outcome(
                    definition.number,
                    result.and_then(|()| {
                        if let Err(error) = &reload {
                            return Err(crate::host::applied_error(
                                definition.number,
                                "daemon_reload",
                                error,
                            ));
                        }
                        let saved = self.rooms.load(definition.number).map_err(|e| {
                            crate::host::applied_error(definition.number, "readback", e)
                        })?;
                        Ok(serde_json::to_value(saved)?)
                    }),
                )
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};

    #[tokio::test]
    async fn provisioning_reloads_after_all_writes_and_preserves_per_room_failures() {
        let temporary = tempfile::tempdir().unwrap();
        let directory = temporary.path();
        let mut host = Host::new(directory.join("rooms"), directory.join("units"));
        host.systemd.executable = directory.join("systemctl");
        fs::write(
            &host.systemd.executable,
            include_str!("../tests/fixtures/fleet_systemctl.py"),
        )
        .unwrap();
        fs::set_permissions(&host.systemd.executable, fs::Permissions::from_mode(0o700)).unwrap();
        let counts_path = directory.join("systemd-counts.json");
        let empty_counts = r#"{"active":0,"peak":0,"started":0,"finished":0}"#;
        fs::write(&counts_path, empty_counts).unwrap();
        let counts =
            || -> Value { serde_json::from_slice(&fs::read(&counts_path).unwrap()).unwrap() };
        let definition = |number| {
            Room::new(
                number,
                crate::rooms::build_template("pure_survival", number, "fixture-token", None, None)
                    .unwrap(),
            )
            .unwrap()
        };
        host.create(&definition(1)).await.unwrap();
        assert_eq!(counts()["reloads"], 1);
        fs::write(&counts_path, empty_counts).unwrap();

        let mut second = definition(2);
        let mut third = definition(3);
        let preview = crate::deployment::PortAllocation::reserve(
            &host.quadlet_dir,
            2,
            &second.cluster,
            &[],
            host.port_pool.clone(),
        )
        .unwrap();
        // Each room needs the host port that the other would take automatically.
        second.deployment.ports.push(preview.mappings[0].clone());
        third.deployment.ports.push(preview.mappings[1].clone());
        drop(preview);
        let mut invalid = definition(4);
        invalid.deployment.image.clear();
        let results = host
            .provision(&[second, definition(1), third, invalid])
            .await
            .unwrap();
        assert_eq!(
            results
                .iter()
                .map(|value| value["number"].clone())
                .collect::<Vec<_>>(),
            [json!(2), json!(1), json!(3), json!(4)]
        );
        assert_eq!(results[0]["result"]["ok"], true);
        assert_eq!(
            results[1]["result"]["error"]["message"],
            "room already exists"
        );
        assert_eq!(results[2]["result"]["ok"], true);
        assert_eq!(results[3]["result"]["ok"], false);
        assert_eq!(counts()["reloads"], 1);
        assert_eq!(counts()["last_reload_units"], 3);

        assert!(host.provision(&[]).await.unwrap().is_empty());
        assert!(
            host.provision(&[definition(1), definition(2)])
                .await
                .unwrap()
                .iter()
                .all(|value| value["result"]["ok"] == false)
        );
        assert_eq!(counts()["reloads"], 1);

        fs::write(directory.join("fail-reload"), "").unwrap();
        let results = host
            .provision(&[definition(4), definition(1)])
            .await
            .unwrap();
        assert_eq!(results[0]["result"]["ok"], false);
        let error = &results[0]["result"]["error"];
        assert_eq!(error["code"], "partial_failure");
        assert_eq!(
            error["details"],
            json!({"applied":true, "number":4, "stage":"daemon_reload"})
        );
        assert!(
            results[0]["result"]["error"]["message"]
                .as_str()
                .unwrap()
                .contains("fixture reload failure")
        );
        assert_eq!(
            results[1]["result"]["error"]["message"],
            "room already exists"
        );
        assert_eq!(host.rooms.load(4).unwrap().number, 4);
        assert_eq!(counts()["reloads"], 2);
        assert_eq!(counts()["last_reload_units"], 4);
        assert_eq!(counts()["active"], 0);

        let root = host.rooms.root();
        std::os::unix::fs::symlink("001", root.join("latest")).unwrap();
        fs::write(
            root.join("001/.dst-server.json"),
            r#"{"template":"chosen"}"#,
        )
        .unwrap();
        fs::write(root.join("002/cluster.ini"), "broken INI").unwrap();
        fs::write(root.join("003/.dst-server.json"), "invalid state").unwrap();
        let results = host
            .batch_matching(
                &host.managed_numbers().unwrap(),
                &HostOperation::Status { game: false },
                Some("chosen"),
            )
            .await
            .unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(
            (
                results[0]["number"].as_u64(),
                results[0]["result"]["ok"].as_bool()
            ),
            (Some(1), Some(true))
        );
        assert_eq!(
            (
                results[1]["number"].as_u64(),
                results[1]["result"]["ok"].as_bool()
            ),
            (Some(3), Some(false))
        );
    }

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
