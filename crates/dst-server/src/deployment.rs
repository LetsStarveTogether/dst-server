//! Single-container Quadlets and durable UDP publication allocation.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{Ipv4Addr, UdpSocket};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};

use crate::settings::ClusterConfig;

pub const DEFAULT_IMAGE: &str = "quay.io/wh2099/dst-server:latest";
pub const PUBLISHED_PORTS_ENV: &str = "DST_SERVER_PUBLISHED_PORTS";
const OWNER: &str = "# Managed by dst-server: room container v1";
const LIMIT: u64 = 1024 * 1024;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Protocol {
    #[default]
    Udp,
    Tcp,
}
impl Protocol {
    fn label(self) -> &'static str {
        match self {
            Self::Udp => "udp",
            Self::Tcp => "tcp",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortMapping {
    pub host: u16,
    pub container: u16,
    #[serde(default)]
    pub protocol: Protocol,
}
impl PortMapping {
    pub fn parse(value: &str) -> Result<Self> {
        let (ports, protocol) = value.split_once('/').context("missing port protocol")?;
        let (host, container) = ports.split_once(':').context("missing host port")?;
        let mapping = Self {
            host: host.parse()?,
            container: container.parse()?,
            protocol: match protocol {
                "udp" => Protocol::Udp,
                "tcp" => Protocol::Tcp,
                _ => bail!("unsupported port protocol"),
            },
        };
        mapping.validate()?;
        Ok(mapping)
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.host >= 1024 && self.container >= 1024,
            "ports must be between 1024 and 65535"
        );
        Ok(())
    }
    pub fn render(&self) -> String {
        format!("{}:{}/{}", self.host, self.container, self.protocol.label())
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct DeploymentOptions {
    pub image: String,
    pub environment: BTreeMap<String, String>,
    pub volume_idmap: Option<String>,
    pub userns: Option<String>,
    pub start_on_boot: bool,
    pub ports: Vec<PortMapping>,
}
impl Default for DeploymentOptions {
    fn default() -> Self {
        Self {
            image: DEFAULT_IMAGE.into(),
            environment: BTreeMap::new(),
            volume_idmap: None,
            userns: None,
            start_on_boot: true,
            ports: Vec::new(),
        }
    }
}
impl DeploymentOptions {
    pub fn validate(&self) -> Result<()> {
        token(&self.image)?;
        ensure!(
            !self.image.ends_with(".image"),
            "image must be an image reference, not another Quadlet"
        );
        for (name, value) in &self.environment {
            ensure!(valid_environment_name(name), "invalid environment name");
            literal(value)?;
            ensure!(
                name != PUBLISHED_PORTS_ENV,
                "{PUBLISHED_PORTS_ENV} is managed by the deployment"
            );
        }
        if let Some(value) = &self.userns {
            token(value)?;
        }
        if let Some(value) = &self.volume_idmap {
            ensure!(regex::Regex::new(r"^(?:uids=\d+-\d+-[1-9]\d*(?:#\d+-\d+-[1-9]\d*)*(?:;gids=\d+-\d+-[1-9]\d*(?:#\d+-\d+-[1-9]\d*)*)?|gids=\d+-\d+-[1-9]\d*(?:#\d+-\d+-[1-9]\d*)*(?:;uids=\d+-\d+-[1-9]\d*(?:#\d+-\d+-[1-9]\d*)*)?)$")?.is_match(value), "invalid volume idmap");
        }
        let mut hosts = BTreeSet::new();
        let mut containers = BTreeSet::new();
        for mapping in &self.ports {
            mapping.validate()?;
            ensure!(
                mapping.protocol == Protocol::Udp,
                "DST publication requires UDP"
            );
            ensure!(
                hosts.insert((mapping.protocol, mapping.host)),
                "duplicate host port"
            );
            ensure!(
                containers.insert((mapping.protocol, mapping.container)),
                "duplicate container port"
            );
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ContainerUnit {
    pub name: String,
    pub cluster: PathBuf,
    pub options: DeploymentOptions,
    pub published_game_ports: BTreeMap<String, u16>,
}
impl ContainerUnit {
    pub fn for_cluster(
        name: &str,
        cluster: impl AsRef<Path>,
        configuration: &ClusterConfig,
        options: DeploymentOptions,
    ) -> Result<Self> {
        options.validate()?;
        let endpoints = endpoints(configuration)?;
        let expected: BTreeSet<_> = endpoints
            .iter()
            .map(|endpoint| endpoint.container)
            .collect();
        let actual: BTreeSet<_> = options
            .ports
            .iter()
            .map(|mapping| mapping.container)
            .collect();
        ensure!(
            expected == actual,
            "publish exactly the game and query ports for every shard"
        );
        let ports: BTreeMap<_, _> = options
            .ports
            .iter()
            .map(|mapping| (mapping.container, mapping.host))
            .collect();
        let unit = Self {
            name: escape_unit_name(name)?,
            cluster: cluster.as_ref().to_owned(),
            published_game_ports: endpoints
                .into_iter()
                .filter(|endpoint| endpoint.game)
                .map(|endpoint| (endpoint.shard, ports[&endpoint.container]))
                .collect(),
            options,
        };
        unit.validate()?;
        Ok(unit)
    }
    pub fn service(&self) -> String {
        format!("{}.service", self.name)
    }
    pub fn filename(&self) -> String {
        format!("{}.container", self.name)
    }
    pub fn validate(&self) -> Result<()> {
        validate_unit_name(&self.name)?;
        validate_directory_path(&self.cluster)?;
        ensure!(
            !self.cluster.as_os_str().as_encoded_bytes().contains(&b':'),
            "volume path cannot contain a colon"
        );
        self.options.validate()?;
        ensure!(
            !self.published_game_ports.is_empty(),
            "deployment requires at least one shard"
        );
        for (name, host) in &self.published_game_ports {
            crate::configuration::validate_shard_name(name)?;
            ensure!(
                self.options
                    .ports
                    .iter()
                    .any(|mapping| mapping.host == *host && mapping.protocol == Protocol::Udp),
                "published game port is missing from mappings"
            );
        }
        Ok(())
    }
    pub fn render(&self) -> Result<String> {
        self.validate()?;
        let mut source = format!(
            "{OWNER}\n[Unit]\nDescription=Don't Starve Together {}\n\n[Container]\nImage={}\nRunInit=true\nExec=/usr/local/bin/dst-server agent --cluster /cluster\nStopTimeout=360\n",
            escape(&self.name),
            quote(&self.options.image)
        );
        let suffix = self
            .options
            .volume_idmap
            .as_ref()
            .map(|value| format!(":idmap={value}"))
            .unwrap_or_default();
        source.push_str(&format!(
            "Volume={}\n",
            volume_escape(&format!("{}:/cluster{suffix}", self.cluster.display()))
        ));
        source.push_str(&format!(
            "Volume={}\n",
            volume_escape(&format!(
                "{}/mods:/install/mods{suffix}",
                self.cluster.display()
            ))
        ));
        if let Some(userns) = &self.options.userns {
            source.push_str(&format!("UserNS={}\n", quote(userns)));
        }
        for mapping in &self.options.ports {
            source.push_str(&format!("PublishPort={}\n", mapping.render()));
        }
        let mut environment = self.options.environment.clone();
        environment.insert(
            PUBLISHED_PORTS_ENV.into(),
            serde_json::to_string(&self.published_game_ports)?,
        );
        for (name, value) in environment {
            source.push_str(&format!(
                "Environment={}\n",
                quote(&format!("{name}={value}"))
            ));
        }
        source.push_str("\n[Service]\nRestart=on-failure\nRestartSec=5\nTimeoutStartSec=900\nTimeoutStopSec=420\n");
        if self.options.start_on_boot {
            source.push_str("\n[Install]\nWantedBy=default.target\n");
        }
        Ok(source)
    }
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let source = read_regular(path)?;
        ensure!(
            source.lines().next() == Some(OWNER),
            "unrelated Quadlet file: {}",
            path.display()
        );
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix(".container"))
            .context("expected a .container unit")?
            .to_owned();
        let fields = parse_sections(&source)?;
        let get = |section: &str, key: &str| -> Result<String> {
            let values = fields
                .get(&(section.into(), key.into()))
                .with_context(|| format!("missing {section}.{key}"))?;
            ensure!(values.len() == 1, "duplicate {section}.{key}");
            Ok(values[0].clone())
        };
        for (section, key, value) in [
            ("Container", "RunInit", "true"),
            (
                "Container",
                "Exec",
                "/usr/local/bin/dst-server agent --cluster /cluster",
            ),
            ("Container", "StopTimeout", "360"),
            ("Service", "Restart", "on-failure"),
            ("Service", "RestartSec", "5"),
            ("Service", "TimeoutStartSec", "900"),
            ("Service", "TimeoutStopSec", "420"),
        ] {
            ensure!(get(section, key)? == value, "unsupported {section}.{key}");
        }
        let mut options = DeploymentOptions {
            image: unquote(&get("Container", "Image")?)?,
            start_on_boot: false,
            ..Default::default()
        };
        if fields.contains_key(&("Install".into(), "WantedBy".into())) {
            ensure!(
                get("Install", "WantedBy")? == "default.target",
                "unsupported install target"
            );
            options.start_on_boot = true;
        }
        if fields.contains_key(&("Container".into(), "UserNS".into())) {
            options.userns = Some(unquote(&get("Container", "UserNS")?)?);
        }
        let volumes = fields
            .get(&("Container".into(), "Volume".into()))
            .context("missing cluster volume")?;
        ensure!(
            volumes.len() == 2,
            "expected cluster and shared Mods volumes"
        );
        let volume = volume_unescape(&volumes[0])?;
        let (cluster, suffix) = volume
            .split_once(":/cluster")
            .context("invalid cluster volume")?;
        options.volume_idmap = if suffix.is_empty() {
            None
        } else {
            Some(
                suffix
                    .strip_prefix(":idmap=")
                    .context("unsupported volume options")?
                    .to_owned(),
            )
        };
        ensure!(
            volume_unescape(&volumes[1])? == format!("{cluster}/mods:/install/mods{suffix}"),
            "invalid shared Mods volume"
        );
        for value in fields
            .get(&("Container".into(), "PublishPort".into()))
            .into_iter()
            .flatten()
        {
            options.ports.push(PortMapping::parse(value)?);
        }
        let mut published_game_ports = None;
        for value in fields
            .get(&("Container".into(), "Environment".into()))
            .into_iter()
            .flatten()
        {
            let assignment = unquote(value)?;
            let (name, value) = assignment
                .split_once('=')
                .context("invalid environment assignment")?;
            if name == PUBLISHED_PORTS_ENV {
                ensure!(
                    published_game_ports.is_none(),
                    "duplicate managed environment"
                );
                published_game_ports = Some(serde_json::from_str(value)?);
            } else {
                ensure!(
                    options
                        .environment
                        .insert(name.into(), value.into())
                        .is_none(),
                    "duplicate environment variable"
                );
            }
        }
        let unit = Self {
            name,
            cluster: cluster.into(),
            options,
            published_game_ports: published_game_ports.context("missing published game ports")?,
        };
        unit.validate()?;
        // Comparing parsed fields also rejects unknown directives rather than silently deleting them.
        ensure!(
            parse_sections(&unit.render()?)? == fields,
            "unsupported or inconsistent managed Quadlet fields"
        );
        Ok(unit)
    }
    pub fn validate_update(&self, directory: impl AsRef<Path>) -> Result<()> {
        let directory = directory.as_ref();
        validate_real_directory(directory)?;
        let path = directory.join(self.filename());
        let updated = parse_sections(&self.render()?)?;
        let old = if path.try_exists()? {
            Some(parse_sections(&Self::load(&path)?.render()?)?)
        } else {
            None
        };
        let changed: BTreeSet<_> = updated
            .keys()
            .chain(old.iter().flat_map(|fields| fields.keys()))
            .filter(|key| old.as_ref().and_then(|fields| fields.get(*key)) != updated.get(*key))
            .cloned()
            .collect();
        for path in dropins(directory, &self.name)? {
            let fields = parse_sections(&read_regular(&path)?)?;
            for (key, values) in fields {
                if changed.contains(&key) {
                    if key == ("Container".into(), "Environment".into()) {
                        let changed_names = changed_environment(old.as_ref(), &updated)?;
                        let overlap =
                            values
                                .iter()
                                .try_fold(false, |overlap, value| -> Result<bool> {
                                    if value.is_empty() {
                                        return Ok(true);
                                    }
                                    let assignment = unquote(value)?;
                                    let name = assignment
                                        .split_once('=')
                                        .context("invalid drop-in environment")?
                                        .0;
                                    Ok(overlap || changed_names.contains(name))
                                })?;
                        ensure!(
                            !overlap,
                            "drop-in overrides changed environment: {}",
                            path.display()
                        );
                    } else {
                        bail!("drop-in overrides {}.{}: {}", key.0, key.1, path.display());
                    }
                }
            }
        }
        Ok(())
    }
    pub fn save(&self, directory: impl AsRef<Path>) -> Result<PathBuf> {
        let directory = directory.as_ref();
        self.validate_update(directory)?;
        let path = directory.join(self.filename());
        atomic_write(&path, self.render()?.as_bytes())?;
        Ok(path)
    }
}

#[derive(Clone, Debug)]
struct Endpoint {
    shard: String,
    container: u16,
    game: bool,
}
fn endpoints(configuration: &ClusterConfig) -> Result<Vec<Endpoint>> {
    let resolved = configuration.resolved();
    let shards = resolved["shards"].as_object().context("missing shards")?;
    let mut result = Vec::new();
    let mut used = BTreeSet::new();
    let master_port = resolved["settings"]["master_port"]
        .as_u64()
        .context("missing internal master port")?;
    for (shard, value) in shards {
        for (field, game) in [("server_port", true), ("master_server_port", false)] {
            let container: u16 = value["settings"][field]
                .as_u64()
                .context("missing shard UDP port")?
                .try_into()?;
            ensure!(
                container >= 1024 && u64::from(container) != master_port && used.insert(container),
                "conflicting container UDP port"
            );
            result.push(Endpoint {
                shard: shard.clone(),
                container,
                game,
            });
        }
    }
    ensure!(!result.is_empty(), "room requires at least one shard");
    Ok(result)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Reservation {
    mappings: Vec<PortMapping>,
    held: BTreeSet<u16>,
}

/// The flock stays held through reservation, native unit replacement and finalization.
/// Interrupted publication retains both old and new claims; retry safely releases the old claims.
pub struct PortAllocation {
    path: PathBuf,
    _lock: File,
    owner_pid: u32,
    reservations: BTreeMap<u16, Reservation>,
    room: u16,
    pub mappings: Vec<PortMapping>,
}

impl Drop for PortAllocation {
    fn drop(&mut self) {
        if self.owner_pid == std::process::id() {
            // A child can retain this shared descriptor before exec; only the
            // allocating process may release the lock on drop or failed setup.
            // SAFETY: self owns the valid lock descriptor throughout Drop.
            unsafe { libc::flock(self._lock.as_raw_fd(), libc::LOCK_UN) };
        }
    }
}

impl PortAllocation {
    pub fn reserve(
        root: impl AsRef<Path>,
        quadlets: impl AsRef<Path>,
        room: u16,
        configuration: &ClusterConfig,
        requested: &[PortMapping],
        pool: std::ops::RangeInclusive<u16>,
    ) -> Result<Self> {
        let root = root.as_ref();
        validate_real_directory(root)?;
        validate_real_directory(quadlets.as_ref())?;
        ensure!(
            *pool.start() >= 1024 && pool.start() <= pool.end(),
            "invalid UDP port pool"
        );
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .mode(0o600)
            .open(root.join(".dst-host.lock"))?;
        ensure!(
            lock.metadata()?.is_file(),
            "host lock is not a regular file"
        );
        // SAFETY: lock owns a valid descriptor throughout this allocation.
        ensure!(
            unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0,
            "another host deployment is in progress"
        );
        let mut allocation = Self {
            path: root.join(".dst-ports.json"),
            _lock: lock,
            owner_pid: std::process::id(),
            reservations: BTreeMap::new(),
            room,
            mappings: Vec::new(),
        };
        let path = &allocation.path;
        let mut reservations: BTreeMap<u16, Reservation> = if path.try_exists()? {
            serde_json::from_str(&read_regular(path)?)?
        } else {
            BTreeMap::new()
        };
        let mut occupied = BTreeSet::new();
        for (number, reservation) in &reservations {
            for mapping in &reservation.mappings {
                mapping.validate()?;
            }
            if *number != room {
                occupied.extend(reservation.held.iter().copied());
                occupied.extend(reservation.mappings.iter().map(|mapping| mapping.host));
            }
        }
        // Include native units even when the registry was created after them.
        for entry in fs::read_dir(quadlets.as_ref())? {
            let path = entry?.path();
            if path
                .extension()
                .is_some_and(|extension| extension == "container")
                && path
                    .file_name()
                    .is_none_or(|name| name != format!("dst-{room:03}.container").as_str())
            {
                let fields = parse_sections(&read_regular(&path)?)?;
                for value in fields
                    .get(&("Container".into(), "PublishPort".into()))
                    .into_iter()
                    .flatten()
                {
                    occupied.extend(existing_udp_ports(value)?);
                }
            }
        }
        let endpoints = endpoints(configuration)?;
        let container_ports: BTreeSet<_> = endpoints
            .iter()
            .map(|endpoint| endpoint.container)
            .collect();
        let mut explicit = BTreeMap::new();
        for mapping in requested {
            mapping.validate()?;
            ensure!(
                mapping.protocol == Protocol::Udp && container_ports.contains(&mapping.container),
                "explicit mapping must refer to a shard game or query port"
            );
            ensure!(
                explicit.insert(mapping.container, mapping.host).is_none(),
                "duplicate explicit container port"
            );
        }
        let previous = reservations.get(&room).cloned().unwrap_or_default();
        let old: BTreeMap<_, _> = previous
            .mappings
            .iter()
            .map(|mapping| (mapping.container, mapping.host))
            .collect();
        let owned: BTreeSet<_> = previous
            .held
            .iter()
            .copied()
            .chain(old.values().copied())
            .collect();
        let mut mappings = Vec::new();
        let mut available = pool;
        for endpoint in endpoints {
            let selected = explicit
                .get(&endpoint.container)
                .copied()
                .or_else(|| old.get(&endpoint.container).copied());
            let host = if let Some(host) = selected {
                ensure!(
                    !occupied.contains(&host),
                    "UDP host port {host} is allocated to another endpoint"
                );
                ensure!(
                    owned.contains(&host) || udp_available(host),
                    "UDP host port {host} is already in use"
                );
                host
            } else {
                available
                    .find(|candidate| {
                        !occupied.contains(candidate)
                            && (owned.contains(candidate) || udp_available(*candidate))
                    })
                    .context("UDP port pool is exhausted")?
            };
            occupied.insert(host);
            mappings.push(PortMapping {
                host,
                container: endpoint.container,
                protocol: Protocol::Udp,
            });
        }
        let held = owned
            .into_iter()
            .chain(mappings.iter().map(|mapping| mapping.host))
            .collect();
        reservations.insert(
            room,
            Reservation {
                mappings: mappings.clone(),
                held,
            },
        );
        atomic_write(path, &serde_json::to_vec(&reservations)?)?;
        allocation.reservations = reservations;
        allocation.mappings = mappings;
        Ok(allocation)
    }
    pub fn commit(mut self) -> Result<()> {
        self.reservations.get_mut(&self.room).unwrap().held =
            self.mappings.iter().map(|mapping| mapping.host).collect();
        atomic_write(&self.path, &serde_json::to_vec(&self.reservations)?)
    }
}

fn existing_udp_ports(value: &str) -> Result<BTreeSet<u16>> {
    let Some(ports) = value.strip_suffix("/udp") else {
        return Ok(BTreeSet::new());
    };
    let host = ports
        .rsplit(':')
        .nth(1)
        .context("cannot determine existing Quadlet UDP host port")?;
    let (first, last) = host.split_once('-').unwrap_or((host, host));
    let first: u16 = first.parse().context("invalid existing UDP host port")?;
    let last: u16 = last
        .parse()
        .context("invalid existing UDP host port range")?;
    ensure!(
        first > 0 && first <= last,
        "invalid existing UDP host port range"
    );
    Ok((first..=last).collect())
}

fn udp_available(port: u16) -> bool {
    UdpSocket::bind((Ipv4Addr::UNSPECIFIED, port)).is_ok()
}
fn valid_environment_name(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_')
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}
fn literal(value: &str) -> Result<()> {
    ensure!(
        !value
            .chars()
            .any(|character| matches!(character, '\0' | '\r' | '\n')),
        "unit value contains NUL or a newline"
    );
    Ok(())
}
fn token(value: &str) -> Result<()> {
    literal(value)?;
    ensure!(
        !value.is_empty() && !value.chars().any(char::is_whitespace),
        "unit token is empty or contains whitespace"
    );
    Ok(())
}
fn escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "%%")
        .replace('$', "$$")
}
// Quadlet reads Volume as a raw value, then quotes it for systemd itself.
// Doubling backslashes here would mount a different host path.
fn volume_escape(value: &str) -> String {
    value.replace('%', "%%").replace('$', "$$")
}
fn volume_unescape(value: &str) -> Result<String> {
    let mut chars = value.chars();
    let mut decoded = String::new();
    while let Some(character) = chars.next() {
        if matches!(character, '$' | '%') {
            ensure!(
                chars.next() == Some(character),
                "dynamic volume expansion is unsupported"
            );
        }
        decoded.push(character);
    }
    literal(&decoded)?;
    Ok(decoded)
}
fn quote(value: &str) -> String {
    format!("\"{}\"", escape(value).replace('"', "\\\""))
}
fn unquote(value: &str) -> Result<String> {
    let mut chars = value
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .context("expected a quoted literal")?
        .chars();
    let mut result = String::new();
    while let Some(character) = chars.next() {
        match character {
            '\\' => {
                let next = chars.next().context("unfinished unit escape")?;
                ensure!(matches!(next, '\\' | '"'), "unsupported unit escape");
                result.push(next);
            }
            '$' | '%' => {
                ensure!(
                    chars.next() == Some(character),
                    "dynamic unit expansion is unsupported"
                );
                result.push(character);
            }
            '"' => bail!("unescaped quote in unit value"),
            _ => result.push(character),
        }
    }
    literal(&result)?;
    Ok(result)
}
pub fn escape_unit_name(value: &str) -> Result<String> {
    literal(value)?;
    ensure!(!value.is_empty(), "unit name is empty");
    let mut name = String::new();
    for (index, byte) in value.bytes().enumerate() {
        if (byte.is_ascii_alphanumeric() || b":_.-".contains(&byte))
            && !(index == 0 && byte == b'.')
        {
            name.push(char::from(byte));
        } else {
            name.push_str(&format!("\\x{byte:02x}"));
        }
    }
    validate_unit_name(&name)?;
    Ok(name)
}
fn validate_unit_name(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty() && value.len() <= 240 && !value.starts_with('.'),
        "unsafe or too long unit name"
    );
    let mut bytes = value.bytes();
    while let Some(byte) = bytes.next() {
        if byte == b'\\' {
            ensure!(bytes.next() == Some(b'x'), "invalid unit escape");
            let high = bytes.next().context("invalid unit escape")?;
            let low = bytes.next().context("invalid unit escape")?;
            ensure!(
                high.is_ascii_hexdigit()
                    && low.is_ascii_hexdigit()
                    && (high != b'0' || low != b'0'),
                "invalid unit escape"
            );
        } else {
            ensure!(
                byte.is_ascii_alphanumeric() || b":_.-".contains(&byte),
                "invalid unit name"
            );
        }
    }
    Ok(())
}
type Fields = BTreeMap<(String, String), Vec<String>>;
fn parse_sections(source: &str) -> Result<Fields> {
    let mut result: Fields = BTreeMap::new();
    let mut section = "";
    for line in source
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with(['#', ';']))
    {
        if let Some(value) = line
            .strip_prefix('[')
            .and_then(|line| line.strip_suffix(']'))
        {
            section = value;
            continue;
        }
        let (key, value) = line.split_once('=').context("invalid Quadlet line")?;
        ensure!(
            !section.is_empty() && !key.trim().is_empty() && !value.ends_with('\\'),
            "invalid or continued Quadlet line"
        );
        result
            .entry((section.into(), key.trim().into()))
            .or_default()
            .push(value.trim().into());
    }
    Ok(result)
}
fn changed_environment(old: Option<&Fields>, new: &Fields) -> Result<BTreeSet<String>> {
    let environment = |fields: Option<&Fields>| -> Result<BTreeMap<String, String>> {
        let mut values = BTreeMap::new();
        for value in fields
            .and_then(|fields| fields.get(&("Container".into(), "Environment".into())))
            .into_iter()
            .flatten()
        {
            let value = unquote(value)?;
            let (name, value) = value.split_once('=').context("invalid environment")?;
            values.insert(name.into(), value.into());
        }
        Ok(values)
    };
    let old = environment(old)?;
    let new = environment(Some(new))?;
    Ok(old
        .keys()
        .chain(new.keys())
        .filter(|name| old.get(*name) != new.get(*name))
        .cloned()
        .collect())
}
fn dropins(directory: &Path, name: &str) -> Result<Vec<PathBuf>> {
    let mut directories = vec![directory.join("container.d")];
    for (index, character) in name.char_indices() {
        if character == '-' {
            directories.push(directory.join(format!("{}.container.d", &name[..=index])));
        }
    }
    directories.push(directory.join(format!("{name}.container.d")));
    let mut files = BTreeMap::new();
    for directory in directories {
        if !directory.try_exists()? {
            continue;
        }
        validate_real_directory(&directory)?;
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            if entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "conf")
            {
                files.insert(entry.file_name(), entry.path());
            }
        }
    }
    Ok(files.into_values().collect())
}
pub(crate) fn validate_directory_path(path: &Path) -> Result<()> {
    ensure!(
        path.is_absolute()
            && !path
                .components()
                .any(|component| matches!(component, Component::ParentDir | Component::CurDir)),
        "directory must be an absolute path without traversal"
    );
    literal(path.to_str().context("directory is not UTF-8")?)?;
    Ok(())
}
pub(crate) fn validate_real_directory(path: &Path) -> Result<()> {
    validate_directory_path(path)?;
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        ensure!(
            fs::symlink_metadata(&current)?.file_type().is_dir(),
            "directory contains a symlink or is not a directory: {}",
            current.display()
        );
    }
    Ok(())
}
pub(crate) fn read_regular(path: &Path) -> Result<String> {
    validate_real_directory(path.parent().context("file needs parent")?)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(path)?;
    ensure!(
        file.metadata()?.is_file() && file.metadata()?.len() <= LIMIT,
        "file is not regular or exceeds limit"
    );
    let mut source = String::new();
    file.take(LIMIT + 1).read_to_string(&mut source)?;
    ensure!(source.len() as u64 <= LIMIT, "file exceeds limit");
    Ok(source)
}
pub(crate) fn atomic_write(path: &Path, contents: &[u8]) -> Result<()> {
    ensure!(contents.len() as u64 <= LIMIT, "host file exceeds limit");
    let parent = path.parent().context("file needs parent")?;
    validate_real_directory(parent)?;
    match fs::symlink_metadata(path) {
        Ok(metadata) => ensure!(metadata.is_file(), "refusing to replace a non-regular file"),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
        Err(error) => return Err(error.into()),
    }
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(contents)?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::fd::FromRawFd;
    use std::os::unix::{net::UnixStream, process::CommandExt};
    use std::process::Command;
    use std::time::Duration;

    fn cluster(count: usize) -> ClusterConfig {
        let shards: serde_json::Map<String, serde_json::Value> = (0..count).map(|index| (format!("Shard{index}"), json!({"settings":{"id":index+1,"name":format!("Shard{index}"),"is_master":index==0,"server_port":11000+index*2,"master_server_port":11001+index*2}}))).collect();
        ClusterConfig::from_value(json!({"settings":{"master_ip":"127.0.0.1"},"shards":shards}))
            .unwrap()
    }

    #[test]
    fn allocation_unlocks_before_an_unrelated_child_executes() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let configuration = cluster(1);
        for commit in [false, true] {
            let allocation = PortAllocation::reserve(
                root.path(),
                units.path(),
                0,
                &configuration,
                &[],
                43000..=43100,
            )
            .unwrap();
            let owner_pid = allocation.owner_pid;
            let lock_fd = allocation._lock.as_raw_fd();
            let (mut parent, child) = UnixStream::pair().unwrap();
            parent
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let parent_fd = parent.as_raw_fd();
            let launched = std::thread::spawn(move || {
                let mut command = Command::new("/bin/true");
                // SAFETY: the callback only performs async-signal-safe descriptor
                // operations with valid inherited FDs and stack-local byte storage.
                unsafe {
                    command.pre_exec(move || {
                        libc::close(parent_fd);
                        let lock_fd = libc::dup(lock_fd);
                        if lock_fd < 0 {
                            return Err(std::io::Error::last_os_error());
                        }
                        // Empty collections avoid allocation/deallocation after
                        // fork; this Drop must leave the parent's lock in place.
                        drop(PortAllocation {
                            path: PathBuf::new(),
                            _lock: File::from_raw_fd(lock_fd),
                            owner_pid,
                            reservations: BTreeMap::new(),
                            room: 0,
                            mappings: Vec::new(),
                        });
                        let mut byte = 1_u8;
                        if libc::write(child.as_raw_fd(), (&byte as *const u8).cast(), 1) != 1
                            || libc::read(child.as_raw_fd(), (&mut byte as *mut u8).cast(), 1) != 1
                        {
                            return Err(std::io::Error::last_os_error());
                        }
                        Ok(())
                    });
                }
                command.status().unwrap()
            });
            parent.read_exact(&mut [0]).unwrap();
            let parent_lock_intact = PortAllocation::reserve(
                root.path(),
                units.path(),
                0,
                &configuration,
                &[],
                43000..=43100,
            )
            .is_err();
            if commit {
                allocation.commit().unwrap();
            } else {
                drop(allocation);
            }
            let next = PortAllocation::reserve(
                root.path(),
                units.path(),
                0,
                &configuration,
                &[],
                43000..=43100,
            );
            parent.write_all(&[1]).unwrap();
            assert!(launched.join().unwrap().success());
            assert!(
                parent_lock_intact,
                "child Drop released its parent's allocation lock"
            );
            assert!(next.is_ok(), "{:#}", next.err().unwrap());
        }
    }
    #[test]
    fn five_world_allocation_is_stable_and_excludes_other_rooms() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let first = PortAllocation::reserve(
            root.path(),
            units.path(),
            0,
            &cluster(5),
            &[],
            42000..=42100,
        )
        .unwrap();
        assert_eq!(first.mappings.len(), 10);
        let mappings = first.mappings.clone();
        assert!(
            PortAllocation::reserve(
                root.path(),
                units.path(),
                1,
                &cluster(1),
                &[],
                42000..=42100
            )
            .is_err()
        );
        first.commit().unwrap();
        let retry = PortAllocation::reserve(
            root.path(),
            units.path(),
            0,
            &cluster(5),
            &[],
            42000..=42100,
        )
        .unwrap();
        assert_eq!(retry.mappings, mappings);
        retry.commit().unwrap();
        let next = PortAllocation::reserve(
            root.path(),
            units.path(),
            1,
            &cluster(2),
            &[],
            42000..=42100,
        )
        .unwrap();
        assert!(
            next.mappings
                .iter()
                .all(|mapping| !mappings.iter().any(|old| old.host == mapping.host))
        );
    }
    #[test]
    fn native_quadlet_roundtrip_conflicts_and_literal_expansions() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let config = cluster(5);
        let allocation =
            PortAllocation::reserve(root.path(), units.path(), 1, &config, &[], 42200..=42300)
                .unwrap();
        let mut options = DeploymentOptions {
            ports: allocation.mappings.clone(),
            ..Default::default()
        };
        options
            .environment
            .insert("SPECIAL".into(), "a b %n $HOME \\ \"中\"".into());
        let unit = ContainerUnit::for_cluster(
            "dst-001",
            root.path().join("space %n $x \\ 中"),
            &config,
            options,
        )
        .unwrap();
        let path = unit.save(units.path()).unwrap();
        assert_eq!(ContainerUnit::load(path).unwrap(), unit);
        let generator = Path::new("/usr/lib/systemd/system-generators/podman-system-generator");
        if generator.exists() {
            let output = std::process::Command::new(generator)
                .arg("--dryrun")
                .env("QUADLET_UNIT_DIRS", units.path())
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            let service = String::from_utf8(output.stdout).unwrap();
            let volume = volume_escape(unit.cluster.to_str().unwrap())
                .replace('\\', "\\\\")
                .replace(' ', "\\x20");
            assert!(
                service.contains(&format!("-v \"{volume}:/cluster\"")),
                "{service}"
            );
            assert!(service.contains("--stop-timeout 360 --init"));
            assert!(service.contains("TimeoutStopSec=420"));
        }
        let dropin = units.path().join("dst-.container.d");
        fs::create_dir(&dropin).unwrap();
        fs::write(dropin.join("limits.conf"), "[Service]\nMemoryMax=4G\n").unwrap();
        unit.save(units.path()).unwrap();
        fs::write(
            dropin.join("image.conf"),
            "[Container]\nImage=another/image\n",
        )
        .unwrap();
        let mut updated = unit.clone();
        updated.options.image = "example/image:v2".into();
        assert!(updated.save(units.path()).is_err());
        fs::write(
            units.path().join("dst-002.container"),
            "[Container]\nImage=foreign\n",
        )
        .unwrap();
        updated.name = "dst-002".into();
        assert!(updated.save(units.path()).is_err());
    }
}
