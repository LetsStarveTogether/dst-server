//! Read native INI topology without evaluating world or Mod configuration.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::net::{IpAddr, Ipv4Addr};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::Serialize;

use crate::files::{self, RoomReader};
use crate::settings::types::{ClusterSettings, ShardSettings};

/// Native INI readers strip surrounding Unicode whitespace and cannot store line breaks or NUL.
pub(crate) static INI_VALUE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
    regex::Regex::new(r"\A(?:[^\s\x00](?:[^\x00\r\n]*[^\s\x00])?)?\z")
        .expect("valid INI value pattern")
});

/// A validated room topology. Credentials are deliberately not retained.
#[derive(Clone, Debug, Serialize)]
pub struct Cluster {
    pub directory: PathBuf,
    /// The master comes first; other shards are sorted by directory name.
    pub shards: Vec<Shard>,
    pub shard_enabled: bool,
    pub master_port: u16,
}

impl Cluster {
    pub fn master(&self) -> &Shard {
        &self.shards[0]
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Shard {
    pub name: String,
    pub master: bool,
    /// An omitted secondary ID is assigned by the native game.
    pub id: Option<u32>,
    pub server_port: u16,
    pub master_server_port: u16,
    pub master_ip: Option<String>,
    pub bind_ip: Ipv4Addr,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum ClusterKey {
    Required,
    GenerateOnSave,
}

/// Discover every immediate shard directory containing `server.ini`.
///
/// Discovery never loads tokens, saves, world overrides or Lua programs.
/// INI diagnostics identify a file and line without echoing configuration values.
pub fn discover(directory: impl AsRef<Path>) -> Result<Cluster> {
    let directory = directory.as_ref();
    reject_symlink_components(directory)?;
    ensure!(directory.is_dir(), "cluster path must be a directory");
    let directory = directory
        .canonicalize()
        .context("resolve cluster directory")?;
    let reader = RoomReader::open(&directory)?;
    read_topology(&reader, ClusterKey::Required)
}

pub(crate) fn read_topology(reader: &RoomReader, key: ClusterKey) -> Result<Cluster> {
    let mut cluster_ini = reader.read_ini("cluster.ini")?;
    cluster_ini.retain(|section, _| section == "shard");
    let settings = ClusterSettings::from_ini(cluster_ini).context("cluster.ini")?;
    let mut shard_settings = BTreeMap::new();
    for name in reader.shard_directories()? {
        let path = Path::new(&name).join("server.ini");
        if let Some(source) = reader.read_optional_text(&path)? {
            let mut ini = Ini::parse(&path, &source, Some(true))?.into_sections();
            ini.remove("account");
            let shard = ShardSettings::from_ini(ini).with_context(|| path.display().to_string())?;
            shard_settings.insert(name, shard);
        }
    }
    ensure!(!shard_settings.is_empty(), "no DST shard directories found");
    let shard_enabled = settings.shard_enabled();
    check_native_roles(
        shard_enabled,
        shard_settings.values().map(|shard| shard.is_master),
    )?;
    let master_port = settings.check_topology(shard_settings.iter(), key)?;
    let mut shards = Vec::with_capacity(shard_settings.len());
    for (name, shard) in shard_settings {
        shards.push(Shard {
            name,
            master: shard.is_master(),
            id: shard.id().map(u32::try_from).transpose()?,
            server_port: shard.server_port().try_into()?,
            master_server_port: shard.master_server_port().try_into()?,
            master_ip: shard.master_ip().or_else(|| settings.master_ip()),
            bind_ip: shard
                .bind_ip()
                .unwrap_or_else(|| settings.bind_ip())
                .parse()?,
        });
    }
    shards.sort_by_cached_key(|shard| (!shard.master, shard.name.to_lowercase()));
    Ok(Cluster {
        directory: reader.directory().to_owned(),
        shards,
        shard_enabled,
        master_port,
    })
}

pub(crate) fn check_native_roles(
    shard_enabled: bool,
    mut roles: impl ExactSizeIterator<Item = Option<bool>>,
) -> Result<()> {
    ensure!(
        roles.len() <= 1 || shard_enabled,
        "shard_enabled must be true for multiple shards"
    );
    ensure!(
        !shard_enabled || roles.all(|role| role.is_some()),
        "is_master is required for each enabled shard"
    );
    Ok(())
}

pub(crate) fn validate_shard_name(name: &str) -> Result<()> {
    ensure!(
        !name.trim().is_empty()
            && name.len() <= 255
            && !name.starts_with('.')
            && !name.contains(['\0', '/', '\\', '\r', '\n'])
            && ![
                "console",
                "mods",
                "cluster.ini",
                "cluster_token.txt",
                "adminlist.txt",
                "whitelist.txt",
                "blocklist.txt"
            ]
            .contains(&name.to_lowercase().as_str()),
        "unsafe shard directory name"
    );
    Ok(())
}

fn reject_symlink_components(path: &Path) -> Result<()> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut checked = PathBuf::new();
    for component in absolute.components() {
        checked.push(component.as_os_str());
        let metadata = fs::symlink_metadata(&checked).context("inspect cluster path")?;
        ensure!(
            !metadata.file_type().is_symlink(),
            "configuration path cannot contain a symlink"
        );
    }
    Ok(())
}

pub(crate) fn validate_host(host: &str) -> Result<()> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        ensure!(
            !ip.is_unspecified() && !ip.is_multicast() && ip != IpAddr::V4(Ipv4Addr::BROADCAST),
            "master_ip must identify a unicast destination"
        );
        return Ok(());
    }
    let name = host.strip_suffix('.').unwrap_or(host);
    ensure!(
        !name.is_empty()
            && name.len() <= 253
            && !name
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte == b'.')
            && name.split('.').all(|label| {
                !label.is_empty()
                    && label.len() <= 63
                    && !label.starts_with('-')
                    && !label.ends_with('-')
                    && label
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            }),
        "master_ip must be an IP address or host name"
    );
    Ok(())
}

// Raw values may contain credentials; never derive Debug or Serialize.
pub(crate) struct Ini {
    values: files::IniDocument,
}

impl Ini {
    pub(crate) fn parse(path: &Path, source: &str, shard: Option<bool>) -> Result<Self> {
        ensure!(
            source.len() <= files::MAX_FILE_BYTES,
            "INI configuration exceeds size limit: {}",
            path.display()
        );
        let mut values = BTreeMap::<String, BTreeMap<String, String>>::new();
        let mut sections = BTreeSet::new();
        let mut section = String::new();
        for (index, line) in source.lines().enumerate() {
            let line = line.trim();
            let invalid = || {
                format!(
                    "invalid DST INI configuration: {} at line {}",
                    path.display(),
                    index + 1
                )
            };
            ensure!(!line.contains(['\0', '\r']), "{}", invalid());
            if line.is_empty() || line.starts_with([';', '#']) {
                continue;
            }
            if line.starts_with('[') {
                let Some(end) = line.find(']') else {
                    bail!("{}", invalid());
                };
                let rest = line[end + 1..].trim();
                ensure!(
                    rest.is_empty() || rest.starts_with([';', '#']),
                    "{}",
                    invalid()
                );
                section = line[1..end].trim().to_ascii_lowercase();
                ensure!(
                    !section.is_empty() && section != "default" && sections.insert(section.clone()),
                    "{}",
                    invalid()
                );
                if shard == Some(true) {
                    ensure!(
                        ShardSettings::SECTIONS
                            .iter()
                            .any(|(name, _)| name.eq_ignore_ascii_case(&section)),
                        "{} (unknown section)",
                        invalid()
                    );
                }
                continue;
            }
            let Some((key, value)) = line.split_once(['=', ':']) else {
                bail!("{}", invalid());
            };
            let key = key.trim().to_ascii_lowercase();
            ensure!(!section.is_empty() && !key.is_empty(), "{}", invalid());
            ensure!(
                values
                    .entry(section.clone())
                    .or_default()
                    .insert(key.clone(), value.trim().to_owned())
                    .is_none(),
                "{} (duplicate option)",
                invalid()
            );
            let schema = match shard {
                Some(true) => ShardSettings::SECTIONS,
                Some(false) if section == "shard" => ClusterSettings::SECTIONS,
                _ => continue,
            };
            let allowed = schema
                .iter()
                .find(|(name, _)| name.eq_ignore_ascii_case(&section));
            ensure!(
                allowed.is_some_and(|(_, fields)| fields.contains(&key.as_str())),
                "{} (unknown topology option)",
                invalid()
            );
        }
        Ok(Self { values })
    }

    pub(crate) fn into_sections(self) -> files::IniDocument {
        self.values
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn cluster(count: usize) -> TempDir {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("cluster.ini"), format!("[SHARD]\nshard_enabled = {}\nmaster_ip = localhost\ncluster_key = PRIVATE # ; VALUE\n", count > 1)).unwrap();
        for index in 0..count {
            let path = root.path().join(if index == 0 {
                "Z主世界".to_owned()
            } else {
                format!("world-{index}")
            });
            fs::create_dir(&path).unwrap();
            fs::write(path.join("server.ini"), format!("[SHARD]\nis_master = {}\nname = World {index}\nid = {}\n[NETWORK]\nserver_port = {}\n[STEAM]\nmaster_server_port = {}\n", index == 0, index + 1, 12000 + index, 27016 + index)).unwrap();
            fs::write(
                path.join("modoverrides.lua"),
                "return make_dynamic_mod_settings()",
            )
            .unwrap();
            fs::write(
                path.join("worldgenoverride.lua"),
                "broken Lua is not topology",
            )
            .unwrap();
        }
        root
    }

    fn replace(root: &Path, path: &str, from: &str, to: &str) {
        let path = root.join(path);
        fs::write(&path, fs::read_to_string(&path).unwrap().replace(from, to)).unwrap();
    }

    #[test]
    fn discovers_one_two_and_five_arbitrary_shards_without_loading_lua() {
        for count in [1, 2, 5] {
            let root = cluster(count);
            let topology = discover(root.path()).unwrap();
            assert_eq!(topology.shards.len(), count);
            assert_eq!(topology.master().name, "Z主世界");
            assert_eq!(topology.master_port, 10888);
            assert!(!format!("{topology:?}").contains("PRIVATE"));
            assert!(
                !serde_json::to_string(&topology)
                    .unwrap()
                    .contains("PRIVATE")
            );
            assert_eq!(
                topology
                    .shards
                    .iter()
                    .skip(1)
                    .map(|shard| &shard.name)
                    .collect::<Vec<_>>(),
                (1..count)
                    .map(|index| format!("world-{index}"))
                    .collect::<Vec<_>>()
                    .iter()
                    .collect::<Vec<_>>()
            );
        }
    }

    #[test]
    fn permits_single_world_native_defaults_and_implicit_secondary_id() {
        let root = cluster(1);
        fs::write(root.path().join("cluster.ini"), "").unwrap();
        fs::write(root.path().join("Z主世界/server.ini"), "").unwrap();
        let topology = discover(root.path()).unwrap();
        assert_eq!(topology.master().server_port, 10999);
        let root = cluster(2);
        replace(root.path(), "world-1/server.ini", "id = 2\n", "");
        assert_eq!(discover(root.path()).unwrap().shards[1].id, None);
    }

    #[test]
    fn rejects_incoherent_topology() {
        for (path, from, to, message) in [
            (
                "cluster.ini",
                "shard_enabled = true",
                "shard_enabled = false",
                "shard_enabled",
            ),
            (
                "cluster.ini",
                "shard_enabled = true",
                "shard_enabled = yes",
                "true or false",
            ),
            ("cluster.ini", "shard_enabled = true\n", "", "shard_enabled"),
            (
                "cluster.ini",
                "cluster_key = PRIVATE # ; VALUE",
                "cluster_key =",
                "cluster_key",
            ),
            (
                "cluster.ini",
                "cluster_key = PRIVATE # ; VALUE\n",
                "",
                "cluster_key",
            ),
            (
                "cluster.ini",
                "master_ip = localhost",
                "master_ip = 0.0.0.0",
                "master_ip",
            ),
            (
                "cluster.ini",
                "master_ip = localhost",
                "master_ip = http://localhost",
                "master_ip",
            ),
            ("cluster.ini", "master_ip = localhost\n", "", "master_ip"),
            (
                "world-1/server.ini",
                "is_master = false",
                "is_master = true",
                "id",
            ),
            ("world-1/server.ini", "is_master = false\n", "", "is_master"),
            ("world-1/server.ini", "id = 2", "id = 1", "id"),
            (
                "world-1/server.ini",
                "id = 2",
                "id = 4294967296",
                "4294967295",
            ),
            ("world-1/server.ini", "name = World 1", "name =", "name"),
            (
                "world-1/server.ini",
                "server_port = 12001",
                "server_port = 27016",
                "UDP port",
            ),
            (
                "world-1/server.ini",
                "server_port = 12001",
                "server_port = 10888",
                "master_port",
            ),
            (
                "world-1/server.ini",
                "server_port = 12001",
                "server_port = 1023",
                "1024",
            ),
            (
                "world-1/server.ini",
                "server_port = 12001",
                "server_port = 65536",
                "65535",
            ),
            (
                "world-1/server.ini",
                "id = 2",
                "id = 2\nmaster_port = 13000",
                "master_port",
            ),
            (
                "world-1/server.ini",
                "id = 2",
                "id = 2\ncluster_key = different",
                "cluster_key",
            ),
            (
                "world-1/server.ini",
                "id = 2",
                "id = 2\nunknown = value",
                "unknown topology option",
            ),
            (
                "world-1/server.ini",
                "master_server_port = 27017",
                "master_server_port = 27017\nauthentication_port = 8766",
                "unknown topology option",
            ),
            ("world-2/server.ini", "id = 3", "id = 2", "unique"),
        ] {
            let root = cluster(5);
            replace(root.path(), path, from, to);
            let error = format!("{:#}", discover(root.path()).unwrap_err());
            assert!(error.contains(message), "expected {message}: {error}");
            assert!(!error.contains("PRIVATE"));
        }
    }

    #[test]
    fn rejects_malformed_ini_without_disclosing_values() {
        for source in [
            "cluster_key = PRIVATE_SECRET",
            "[SHARD]\ncluster_key PRIVATE_SECRET",
            "[SHARD]\ncluster_key = PRIVATE_SECRET\nCLUSTER_KEY = PRIVATE_SECRET",
            "[SHARD]\ncluster_key = PRIVATE_SECRET\n[shard]",
            "[SHARD]\ncluster_key = PRIVATE_SECRET\0",
            "[DEFAULT]\ncluster_key = PRIVATE_SECRET",
        ] {
            let root = cluster(1);
            fs::write(root.path().join("cluster.ini"), source).unwrap();
            let error = format!("{:#}", discover(root.path()).unwrap_err());
            assert!(error.contains("cluster.ini at line"), "{error}");
            assert!(!error.contains("PRIVATE_SECRET"));
        }
    }

    #[test]
    fn preserves_inline_comment_characters_in_secrets() {
        let root = cluster(2);
        replace(
            root.path(),
            "world-1/server.ini",
            "id = 2",
            "id = 2\ncluster_key = PRIVATE # ; DIFFERENT",
        );
        assert!(
            discover(root.path())
                .unwrap_err()
                .to_string()
                .contains("cluster_key")
        );
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinks_in_root_shard_and_ini_paths() {
        use std::os::unix::fs::symlink;
        for path in ["cluster.ini", "Z主世界", "world-1/server.ini"] {
            let root = cluster(2);
            let target = root.path().join(path);
            let moved = root.path().join(".original");
            fs::rename(&target, &moved).unwrap();
            symlink(&moved, &target).unwrap();
            assert!(
                discover(root.path())
                    .unwrap_err()
                    .to_string()
                    .contains("symlink")
            );
        }
        let root = cluster(1);
        let links = tempfile::tempdir().unwrap();
        symlink(root.path(), links.path().join("room")).unwrap();
        assert!(
            discover(links.path().join("room"))
                .unwrap_err()
                .to_string()
                .contains("symlink")
        );
    }
}
