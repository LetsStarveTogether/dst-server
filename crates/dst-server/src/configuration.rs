//! Read native INI topology without evaluating world or Mod configuration.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::net::{IpAddr, Ipv4Addr};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::Serialize;

const MAX_INI_BYTES: u64 = 1024 * 1024;

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
    let cluster_ini = Ini::read(&directory.join("cluster.ini"), false)?;
    let shard_enabled = cluster_ini.boolean("shard", "shard_enabled", false)?;
    let cluster_port = cluster_ini.port("shard", "master_port", 10888)?;
    let cluster_bind = cluster_ini.ipv4("bind_ip", Ipv4Addr::LOCALHOST)?;
    if let Some(host) = cluster_ini.get("shard", "master_ip") {
        validate_host(host).context("cluster.ini SHARD.master_ip")?;
    }

    let mut directories = Vec::new();
    for entry in fs::read_dir(&directory).context("read cluster directory")? {
        let entry = entry.context("read cluster directory entry")?;
        let name = entry.file_name();
        let name = name
            .to_str()
            .context("cluster entry name must be valid UTF-8")?;
        if name == "mods" || name.starts_with('.') {
            continue;
        }
        let kind = entry.file_type().context("read cluster entry type")?;
        ensure!(!kind.is_symlink(), "cluster entries cannot be symlinks");
        if kind.is_dir() && regular_file_exists(&entry.path().join("server.ini"))? {
            validate_shard_name(name)?;
            directories.push((name.to_owned(), entry.path()));
        }
    }
    ensure!(!directories.is_empty(), "no DST shard directories found");
    directories.sort_by_cached_key(|(name, _)| (name.to_lowercase(), name.clone()));
    let multi = directories.len() > 1;
    ensure!(
        !multi || shard_enabled,
        "shard_enabled must be true for multiple shards"
    );

    let mut names = BTreeSet::new();
    let mut ids = BTreeSet::new();
    let mut ports = BTreeSet::new();
    let mut shards = Vec::with_capacity(directories.len());
    let mut master_ports = BTreeSet::new();
    let mut cluster_keys = BTreeSet::new();
    for (name, path) in directories {
        ensure!(
            names.insert(name.to_lowercase()),
            "duplicate shard directory names"
        );
        let ini = Ini::read(&path.join("server.ini"), true)?;
        ensure!(
            !(multi || shard_enabled) || ini.get("shard", "is_master").is_some(),
            "is_master is required for each enabled shard"
        );
        let master = ini.boolean("shard", "is_master", true)?;
        if !master {
            ensure!(
                ini.get("shard", "name")
                    .is_some_and(|value| !value.is_empty()),
                "every secondary shard requires SHARD.name"
            );
        }
        let id = ini
            .get("shard", "id")
            .map(|value| unsigned(value, "SHARD.id"))
            .transpose()?;
        if let Some(id) = id {
            ensure!(
                (master && id == 1) || (!master && id >= 2),
                "master shard id must be 1; secondary shard ids must be at least 2"
            );
            ensure!(ids.insert(id), "shard IDs must be unique");
        }
        master_ports.insert(ini.port("shard", "master_port", cluster_port)?);
        cluster_keys.insert(
            ini.get("shard", "cluster_key")
                .or_else(|| cluster_ini.get("shard", "cluster_key"))
                .map(str::to_owned),
        );
        let master_ip = ini
            .get("shard", "master_ip")
            .or_else(|| cluster_ini.get("shard", "master_ip"));
        if let Some(host) = master_ip {
            validate_host(host).context("server.ini SHARD.master_ip")?;
        }
        ensure!(
            master || master_ip.is_some(),
            "master_ip is required for every secondary shard"
        );
        let shard = Shard {
            name,
            master,
            id,
            server_port: ini.port("network", "server_port", 10999)?,
            master_server_port: ini.port("steam", "master_server_port", 27016)?,
            master_ip: master_ip.map(str::to_owned),
            bind_ip: ini.ipv4("bind_ip", cluster_bind)?,
        };
        for port in [shard.server_port, shard.master_server_port] {
            ensure!(ports.insert(port), "UDP port {port} is used more than once");
        }
        shards.push(shard);
    }

    ensure!(
        shards.iter().filter(|shard| shard.master).count() == 1,
        "expected exactly one master shard"
    );
    ensure!(
        master_ports.len() == 1,
        "all shards must use the same master_port"
    );
    let master_port = *master_ports.first().expect("at least one shard");
    ensure!(
        !ports.contains(&master_port),
        "master_port conflicts with another UDP port"
    );
    if multi || shard_enabled {
        ensure!(
            cluster_keys.len() == 1,
            "all shards must use the same cluster_key"
        );
        ensure!(
            !cluster_keys.contains(&Some(String::new()))
                && (!multi || !cluster_keys.contains(&None)),
            "multiple shards require a non-empty shared cluster_key"
        );
    }
    // Stable sorting retains the deterministic name order among secondaries.
    shards.sort_by_key(|shard| !shard.master);
    Ok(Cluster {
        directory,
        shards,
        shard_enabled,
        master_port,
    })
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

fn regular_file_exists(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            ensure!(
                !metadata.file_type().is_symlink(),
                "configuration file cannot be a symlink: {}",
                path.display()
            );
            ensure!(
                metadata.is_file(),
                "configuration must be a regular file: {}",
                path.display()
            );
            Ok(true)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => {
            Err(error).with_context(|| format!("inspect configuration file: {}", path.display()))
        }
    }
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

fn unsigned(value: &str, field: &str) -> Result<u32> {
    ensure!(
        !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()),
        "{field} must be an unsigned integer"
    );
    value
        .parse()
        .with_context(|| format!("{field} exceeds its integer range"))
}

// Raw values may contain credentials; never derive Debug or Serialize.
pub(crate) struct Ini {
    path: PathBuf,
    values: BTreeMap<(String, String), String>,
}

impl Ini {
    fn read(path: &Path, shard: bool) -> Result<Self> {
        ensure!(
            regular_file_exists(path)?,
            "missing configuration file: {}",
            path.display()
        );
        let mut source = String::new();
        let mut options = OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        }
        let file = options
            .open(path)
            .with_context(|| format!("open INI configuration: {}", path.display()))?;
        ensure!(
            file.metadata()?.is_file(),
            "INI configuration must be a regular file"
        );
        file.take(MAX_INI_BYTES + 1)
            .read_to_string(&mut source)
            .with_context(|| format!("read UTF-8 INI configuration: {}", path.display()))?;
        Self::parse(path, &source, Some(shard))
    }

    pub(crate) fn parse(path: &Path, source: &str, shard: Option<bool>) -> Result<Self> {
        ensure!(
            source.len() as u64 <= MAX_INI_BYTES,
            "INI configuration exceeds size limit: {}",
            path.display()
        );
        let mut values = BTreeMap::new();
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
                        ["shard", "network", "steam", "account"].contains(&section.as_str()),
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
                    .insert((section.clone(), key.clone()), value.trim().to_owned())
                    .is_none(),
                "{} (duplicate option)",
                invalid()
            );
            let allowed: Option<&[&str]> = match (shard, section.as_str()) {
                (Some(false), "shard") => Some(&[
                    "shard_enabled",
                    "bind_ip",
                    "master_ip",
                    "master_port",
                    "cluster_key",
                ]),
                (Some(true), "shard") => Some(&[
                    "is_master",
                    "name",
                    "id",
                    "bind_ip",
                    "master_ip",
                    "master_port",
                    "cluster_key",
                ]),
                (Some(true), "network") => Some(&["server_port"]),
                (Some(true), "steam") => Some(&["master_server_port", "authentication_port"]),
                (Some(true), "account") => Some(&["encode_user_path"]),
                _ => None,
            };
            ensure!(
                allowed.is_none_or(|allowed| allowed.contains(&key.as_str())),
                "{} (unknown topology option)",
                invalid()
            );
        }
        Ok(Self {
            path: path.to_owned(),
            values,
        })
    }

    pub(crate) fn into_sections(self) -> BTreeMap<String, BTreeMap<String, String>> {
        let mut sections = BTreeMap::<String, BTreeMap<String, String>>::new();
        for ((section, key), value) in self.values {
            sections.entry(section).or_default().insert(key, value);
        }
        sections
    }

    fn get(&self, section: &str, key: &str) -> Option<&str> {
        self.values
            .get(&(section.to_owned(), key.to_owned()))
            .map(String::as_str)
    }

    fn boolean(&self, section: &str, key: &str, default: bool) -> Result<bool> {
        match self.get(section, key) {
            None => Ok(default),
            Some(value) if value.eq_ignore_ascii_case("true") => Ok(true),
            Some(value) if value.eq_ignore_ascii_case("false") => Ok(false),
            _ => bail!(
                "{}: {section}.{key} must be true or false",
                self.path.display()
            ),
        }
    }

    fn port(&self, section: &str, key: &str, default: u16) -> Result<u16> {
        let Some(value) = self.get(section, key) else {
            return Ok(default);
        };
        let port = unsigned(value, key)
            .with_context(|| format!("{}: {section}.{key}", self.path.display()))?;
        ensure!(
            (1024..=65535).contains(&port),
            "{}: {section}.{key} must be between 1024 and 65535",
            self.path.display()
        );
        Ok(port as u16)
    }

    fn ipv4(&self, key: &str, default: Ipv4Addr) -> Result<Ipv4Addr> {
        self.get("shard", key).map_or(Ok(default), |value| {
            value.parse().with_context(|| {
                format!(
                    "{}: SHARD.{key} must be an IPv4 address",
                    self.path.display()
                )
            })
        })
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
            fs::write(path.join("server.ini"), format!("[SHARD]\nis_master = {}\nname = World {index}\nid = {}\n[NETWORK]\nserver_port = {}\n[STEAM]\nmaster_server_port = {}\nauthentication_port = ignored\n", index == 0, index + 1, 12000 + index, 27016 + index)).unwrap();
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
            ("world-1/server.ini", "id = 2", "id = 4294967296", "range"),
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
