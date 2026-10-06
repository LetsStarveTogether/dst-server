//! Compare the versions loaded by DST with Workshop declarations without delaying room policy.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::{
    model::{Envelope, Request, Target},
    room::Room,
    steam,
};

use super::{UpdateTarget, UpdateTargets, valid_version};

pub(crate) const INTERVAL: Duration = Duration::from_secs(300);
type LoadedMods = BTreeMap<u64, BTreeMap<String, String>>;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct Check {
    pub checked_at: Option<DateTime<Utc>>,
    pub targets: UpdateTargets,
    pub versions: BTreeMap<u64, Versions>,
    pub unavailable: BTreeMap<u64, String>,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct Versions {
    pub loaded: BTreeMap<String, String>,
    pub declared: Option<String>,
}

impl Check {
    pub fn verification(
        &self,
        required: &UpdateTargets,
    ) -> std::result::Result<UpdateTargets, String> {
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        if required.keys().any(|id| self.unavailable.contains_key(id)) {
            return Err("Mod update verification is waiting for comparable versions".into());
        }
        for (id, target) in required {
            let Some(versions) = self.versions.get(id) else {
                return Err("Mod update verification is missing an expected Mod".into());
            };
            if target
                .shards
                .iter()
                .any(|shard| !versions.loaded.contains_key(shard))
            {
                return Err("Mod update verification is missing an expected shard".into());
            }
        }
        let mut targets = self.targets.clone();
        if !targets.is_empty() {
            for (id, required) in required {
                if let Some(target) = targets.get_mut(id) {
                    target.shards.extend(required.shards.iter().cloned());
                } else {
                    let versions = &self.versions[id].loaded;
                    let Some(version) = versions.values().next() else {
                        return Err("Mod update verification has no loaded version".into());
                    };
                    if !valid_version(version) || versions.values().any(|other| other != version) {
                        return Err(
                            "Mod update verification has inconsistent loaded versions".into()
                        );
                    }
                    // A comparable required item outside targets matched this query's publication.
                    targets.insert(
                        *id,
                        UpdateTarget {
                            version: version.clone(),
                            shards: required
                                .shards
                                .iter()
                                .chain(versions.keys())
                                .cloned()
                                .collect(),
                        },
                    );
                }
            }
        }
        Ok(targets)
    }
}

#[derive(Deserialize)]
struct EnabledMod {
    id: String,
    #[serde(default)]
    version: String,
}

pub(crate) async fn check(
    room: &Arc<Room>,
    client: &std::result::Result<steam::Client, steam::Error>,
    required: &UpdateTargets,
) -> Result<Check> {
    tokio::time::timeout(Duration::from_secs(30), async {
        let loaded = read_loaded(room).await?;
        if loaded.is_empty() && required.is_empty() {
            return Ok(report(loaded));
        }
        let report = if !required.is_empty() && satisfied(&loaded, required) {
            report(loaded.clone())
        } else {
            let client = client
                .as_ref()
                .map_err(|error| anyhow::anyhow!(error.to_string()))?;
            query(client, &loaded, required).await?
        };
        ensure!(
            read_loaded(room).await? == loaded,
            "loaded Mod versions changed during the check"
        );
        Ok(report)
    })
    .await
    .context("Workshop check timed out")?
}

async fn read_loaded(room: &Arc<Room>) -> Result<LoadedMods> {
    let mut loaded = LoadedMods::new();
    for shard in room.status().shards {
        let value = room
            .invoke_until_complete(Envelope {
                target: Target::Shard(shard.name.clone()),
                request: Request::Mods {},
                timeout: Some(5.0),
            })
            .await?;
        for item in serde_json::from_value::<Vec<EnabledMod>>(value)? {
            if let Some(id) = item.id.strip_prefix("workshop-") {
                let id = crate::settings::workshop_id(id)?;
                ensure!(
                    loaded
                        .entry(id)
                        .or_default()
                        .insert(shard.name.clone(), item.version)
                        .is_none(),
                    "a shard reported the same Workshop Mod more than once"
                );
            }
        }
    }
    Ok(loaded)
}

fn satisfied(loaded: &LoadedMods, targets: &UpdateTargets) -> bool {
    targets.iter().all(|(id, target)| {
        loaded.get(id).is_some_and(|versions| {
            target
                .shards
                .iter()
                .all(|shard| versions.contains_key(shard))
                && versions.values().all(|version| version == &target.version)
        })
    })
}

fn report(loaded: LoadedMods) -> Check {
    Check {
        checked_at: Some(Utc::now()),
        versions: loaded
            .into_iter()
            .map(|(id, loaded)| {
                (
                    id,
                    Versions {
                        loaded,
                        declared: None,
                    },
                )
            })
            .collect(),
        ..Default::default()
    }
}

async fn query(
    client: &steam::Client,
    loaded: &LoadedMods,
    required: &UpdateTargets,
) -> Result<Check> {
    let mut report = report(loaded.clone());
    let ids: BTreeSet<_> = loaded.keys().chain(required.keys()).copied().collect();
    let ids: Vec<_> = ids.into_iter().map(steam::PublishedFileId).collect();
    for batch in ids.chunks(steam::MAX_PUBLISHED_FILES) {
        for file in client.published_file_details(batch).await? {
            let id = file.id.0;
            let compared = (|| -> Result<()> {
                let remote = file.details.context("Workshop item is unavailable")?;
                ensure!(
                    remote.consumer_app_id == steam::AppId(322330),
                    "Workshop item is not for DST"
                );
                ensure!(!remote.banned, "Workshop item is banned");
                let (declared, expected) = declared_version(&remote)?;
                let versions = report.versions.entry(id).or_default();
                versions.declared = Some(declared);
                ensure!(
                    !versions.loaded.is_empty(),
                    "Mod is missing from the running game"
                );
                ensure!(
                    versions
                        .loaded
                        .values()
                        .all(|version| valid_version(version)),
                    "loaded Mod version is empty or invalid"
                );
                let shards: BTreeSet<_> = versions.loaded.keys().cloned().collect();
                if let Some(target) = required.get(&id) {
                    ensure!(
                        target.shards.is_subset(&shards),
                        "Mod is missing from an expected shard"
                    );
                }
                if versions.loaded.values().any(|version| version != &expected) {
                    report.targets.insert(
                        id,
                        UpdateTarget {
                            version: expected,
                            shards,
                        },
                    );
                }
                Ok(())
            })();
            if let Err(error) = compared {
                report.unavailable.insert(id, error.to_string());
            }
        }
    }
    Ok(report)
}

fn declared_version(remote: &steam::PublishedFileDetails) -> Result<(String, String)> {
    let mut versions = remote
        .tags
        .iter()
        .filter_map(|tag| tag.tag.strip_prefix("version:"));
    let declared = versions.next().context("Workshop version tag is missing")?;
    ensure!(
        declared.len() <= super::MAX_VERSION_BYTES,
        "Workshop version tag exceeds the byte limit"
    );
    // ModIndex loads versions through Lua TrimString and string.lower in the C locale.
    let version = declared
        .trim_matches([' ', '\t', '\n', '\r', '\x0b', '\x0c'])
        .to_ascii_lowercase();
    ensure!(
        valid_version(&version),
        "Workshop version tag is empty or invalid"
    );
    ensure!(
        versions.all(|other| other == declared),
        "Workshop version tags conflict"
    );
    Ok((declared.to_owned(), version))
}

#[cfg(test)]
mod tests;
