use super::*;
use crate::{files::PermissionFiles, policy::Policy, settings::ClusterConfig};

impl Room {
    pub(super) fn read_configuration(&self) -> Result<Value> {
        self.with_lock(|lock| {
            let cluster = ClusterConfig::load(lock)?;
            let mut state = lock.read_state()?;
            Ok(json!({
                "cluster": cluster.into_value(),
                "policy": state.remove("policy").unwrap_or_else(|| json!({})),
                "template": state.remove("template"),
            }))
        })
    }

    pub(super) fn edit_configuration(
        &self,
        changes: &[(String, Value)],
        unset: &[String],
    ) -> Result<Value> {
        let edit_cluster = changes
            .iter()
            .map(|(path, _)| path)
            .chain(unset)
            .any(|path| path == "/cluster" || path.starts_with("/cluster/"));
        let (cluster, policy) = self.with_lock(|lock| {
            Ok((
                edit_cluster
                    .then(|| ClusterConfig::load(lock))
                    .transpose()?,
                Policy::load(lock)?,
            ))
        })?;
        let mut data = json!({"policy":policy});
        let mut defaults = data.clone();
        if let Some(cluster) = &cluster {
            data["cluster"] = cluster.as_value();
            defaults["cluster"] = cluster.resolved();
        }
        let mut edited = crate::rooms::edit_fields(data, defaults, changes, unset)
            .map_err(|e| Error::invalid("pointer", e.to_string()))?;
        let next_policy: Policy = match edited.get_mut("policy") {
            Some(value) => serde_json::from_value(value.take())
                .map_err(|e| Error::invalid("policy", e.to_string()))?,
            None => Policy::default(),
        };
        next_policy
            .validate()
            .map_err(|e| Error::invalid("policy", e.to_string()))?;
        let mut changed = Vec::new();
        if let Some(cluster) = cluster {
            let next = ClusterConfig::from_value(edited["cluster"].take())
                .map_err(|e| Error::invalid("cluster", e.to_string()))?;
            if cluster != next {
                changed = self.configure(&next, false)?;
            }
        }
        let policy_changed = policy != next_policy;
        if policy_changed {
            self.with_lock(|lock| next_policy.save(lock))?;
        }
        Ok(json!({"changed":changed, "policy_changed":policy_changed}))
    }

    pub(super) fn configure(
        &self,
        configuration: &ClusterConfig,
        replace_permissions: bool,
    ) -> Result<Vec<PathBuf>> {
        self.require_stopped()?;
        crate::telemetry::validate_context(
            &self.telemetry.local.resource,
            &self.cluster_name,
            configuration.data().shards.keys().map(String::as_str),
        )?;
        if !self.ports.lock().unwrap().is_empty()
            && !crate::deployment::same_shard_ports(&self.cluster.lock().unwrap(), configuration)
                .map_err(internal)?
        {
            return Err(internal(
                "changing shard names or published game or query ports requires an inactive room service",
            ));
        }
        let changed = self.with_lock(|lock| {
            let permissions = if replace_permissions {
                PermissionFiles::ReplaceOffline
            } else {
                PermissionFiles::Preserve
            };
            configuration.save(&mut lock.while_stopped(), permissions)
        })?;
        let cluster = configuration::discover(&self.directory).map_err(internal)?;
        self.status.send_modify(|status| {
            status.master = cluster.master().name.clone();
            status.shards = cluster
                .shards
                .iter()
                .map(|shard| ShardStatus {
                    name: shard.name.clone(),
                    is_master: shard.master,
                    phase: Phase::Stopped,
                    pid: None,
                    identity: None,
                    readiness: Readiness::default(),
                    error: None,
                })
                .collect();
        });
        *self.cluster.lock().unwrap() = cluster;
        self.drivers.lock().unwrap().clear();
        Ok(changed)
    }
}
