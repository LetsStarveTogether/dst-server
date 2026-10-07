//! Compare enabled Workshop items with installed content without delaying room policy.

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

use super::manifest::{self, Version};

pub(crate) const INTERVAL: Duration = Duration::from_secs(300);

#[derive(Clone, Debug, Default, Serialize)]
pub(crate) struct Check {
    pub checked_at: Option<DateTime<Utc>>,
    pub outdated: BTreeSet<u64>,
    pub unavailable: BTreeSet<u64>,
    pub error: Option<String>,
}

#[derive(Deserialize)]
struct EnabledMod {
    id: String,
}

pub(crate) async fn check(room: &Arc<Room>, client: &steam::Client) -> Result<Check> {
    tokio::time::timeout(Duration::from_secs(30), async {
        let mut ids = BTreeSet::new();
        for shard in room.status().shards {
            let value = room
                .invoke_until_complete(Envelope {
                    target: Target::Shard(shard.name),
                    request: Request::Mods {},
                    timeout: Some(5.0),
                })
                .await?;
            for item in serde_json::from_value::<Vec<EnabledMod>>(value)? {
                if let Some(id) = item.id.strip_prefix("workshop-") {
                    ids.insert(crate::settings::workshop_id(id)?);
                }
            }
        }
        let installed = read_installed(room, &ids)?;
        let report = query(client, &ids, &installed).await?;
        ensure!(
            read_installed(room, &ids)? == installed,
            "installed Mods changed during the check"
        );
        Ok(report)
    })
    .await
    .context("Workshop check timed out")?
}

fn read_installed(room: &Room, ids: &BTreeSet<u64>) -> Result<BTreeMap<u64, Version>> {
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    Ok(room.with_lock(|lock| {
        let source = lock
            .read_optional_text(manifest::PATH)?
            .context("installed Workshop manifest is unavailable")?;
        let mut installed = manifest::parse(&source)?;
        installed.retain(|id, _| ids.contains(id));
        Ok(installed)
    })?)
}

async fn query(
    client: &steam::Client,
    ids: &BTreeSet<u64>,
    installed: &BTreeMap<u64, Version>,
) -> Result<Check> {
    let mut report = Check::default();
    let ids: Vec<_> = ids.iter().map(|id| steam::PublishedFileId(*id)).collect();
    for batch in ids.chunks(steam::MAX_PUBLISHED_FILES) {
        for file in client.published_file_details(batch).await? {
            match (installed.get(&file.id.0), file.details) {
                (Some(local), Ok(remote))
                    if remote.consumer_app_id == steam::AppId(322330) && !remote.banned =>
                {
                    if changed(local, &remote) {
                        report.outdated.insert(file.id.0);
                    }
                }
                _ => {
                    report.unavailable.insert(file.id.0);
                }
            }
        }
    }
    report.checked_at = Some(Utc::now());
    Ok(report)
}

fn changed(installed: &Version, remote: &steam::PublishedFileDetails) -> bool {
    if remote.updated_at < installed.updated_at {
        return false;
    }
    if [installed.content.0, remote.content_handle.0]
        .iter()
        .all(|handle| ![0, u64::MAX].contains(handle))
    {
        // Content handles are opaque; metadata-only edits retain the same content.
        remote.content_handle != installed.content
    } else {
        remote.updated_at > installed.updated_at
    }
}

#[cfg(test)]
mod tests;
