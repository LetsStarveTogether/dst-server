//! Single-container Quadlets and UDP publication allocation.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{Ipv4Addr, UdpSocket};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};

use crate::settings::ClusterConfig;

pub const DEFAULT_IMAGE: &str = "quay.io/wh2099/dst-server:latest";
pub(crate) const BETA_IMAGE: &str = "quay.io/wh2099/dst-server:beta";
pub const PUBLISHED_PORTS_ENV: &str = "DST_SERVER_PUBLISHED_PORTS";
pub(crate) const SYSTEMD_UNIT_ENV: &str = "PODMAN_SYSTEMD_UNIT";
const OWNER: &str = "# Managed by dst-server: room container v1";
const LIMIT: u64 = 1024 * 1024;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortMapping {
    pub host: u16,
    pub container: u16,
}
impl PortMapping {
    pub fn parse(value: &str) -> Result<Self> {
        let ports = value
            .strip_suffix("/udp")
            .context("DST publication requires UDP")?;
        let (host, container) = ports.split_once(':').context("missing host port")?;
        let mapping = Self {
            host: host.parse()?,
            container: container.parse()?,
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
        format!("{}:{}/udp", self.host, self.container)
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
                name != PUBLISHED_PORTS_ENV && name != SYSTEMD_UNIT_ENV,
                "{name} is managed by the deployment"
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
            ensure!(hosts.insert(mapping.host), "duplicate host port");
            ensure!(
                containers.insert(mapping.container),
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

#[derive(Debug, thiserror::Error)]
#[error(
    "native shard game or query ports differ from the managed Quadlet; stop the service and use host edit to reconcile the ports"
)]
pub(crate) struct PublishedPortMismatch;

impl ContainerUnit {
    pub fn for_cluster(
        name: &str,
        cluster: impl AsRef<Path>,
        configuration: &ClusterConfig,
        mut options: DeploymentOptions,
    ) -> Result<Self> {
        let name = escape_unit_name(name)?;
        let shards = configured_ports(configuration)?;
        ensure!(
            options.ports.len() == shards.len() * 2,
            "publish two ports per shard"
        );
        let hosts: BTreeMap<_, _> = options
            .ports
            .into_iter()
            .map(|mapping| (mapping.container, mapping.host))
            .collect();
        // Each shard's game mapping precedes its query mapping in shard-name order.
        options.ports = shards
            .values()
            .flatten()
            .map(|&container| {
                let host = *hosts.get(&container)?;
                Some(PortMapping { host, container })
            })
            .collect::<Option<_>>()
            .context("publish exactly the game and query ports for every shard")?;
        let published_game_ports = shards
            .keys()
            .zip(options.ports.as_chunks::<2>().0)
            .map(|(&name, [game, _])| (name.to_owned(), game.host))
            .collect();
        let unit = Self {
            name,
            cluster: cluster.as_ref().to_owned(),
            published_game_ports,
            options,
        };
        unit.validate()?;
        Ok(unit)
    }
    pub fn service(&self) -> String {
        format!("{}.service", self.name)
    }
    pub(crate) fn check_native_ports(&self) -> Result<()> {
        self.check_ports(native_ports(&crate::configuration::discover(
            &self.cluster,
        )?))
    }
    pub(crate) fn check_ports(&self, shards: BTreeMap<&str, [u16; 2]>) -> Result<()> {
        ensure!(
            shards
                .keys()
                .copied()
                .eq(self.published_game_ports.keys().map(String::as_str))
                && shards.values().flatten().copied().eq(self
                    .options
                    .ports
                    .iter()
                    .map(|mapping| mapping.container)),
            PublishedPortMismatch
        );
        Ok(())
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
        ensure!(
            self.options.ports.len() == self.published_game_ports.len() * 2,
            "publish two ports per shard"
        );
        for ((name, host), pair) in self
            .published_game_ports
            .iter()
            .zip(self.options.ports.as_chunks::<2>().0)
        {
            crate::configuration::validate_shard_name(name)?;
            ensure!(
                pair[0].host == *host,
                "the game mapping must precede the query mapping for shard {name}"
            );
        }
        Ok(())
    }
    pub fn render(&self) -> Result<String> {
        self.validate()?;
        let mut source = format!(
            "{OWNER}\n[Unit]\nDescription=Don't Starve Together {}\n\n[Container]\nImage={}\nPull=never\nRunInit=true\nTimezone=local\nLogDriver=passthrough\nExec=/usr/local/bin/dst-server agent\nStopSignal=SIGTERM\nStopTimeout=540\n",
            escape(&self.name),
            quote(&self.options.image)
        );
        if matches!(self.options.image.as_str(), DEFAULT_IMAGE | BETA_IMAGE) {
            source.push_str("AutoUpdate=registry\n");
        }
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
        let published = serde_json::to_string(&self.published_game_ports)?;
        source.push_str(&format!(
            "Environment={SYSTEMD_UNIT_ENV}\nEnvironment={}\n",
            quote(&format!("{PUBLISHED_PORTS_ENV}={published}"))
        ));
        for (name, value) in &self.options.environment {
            source.push_str(&format!(
                "Environment={}\n",
                quote(&format!("{name}={value}"))
            ));
        }
        source.push_str("\n[Service]\nRestart=on-failure\nRestartSec=5\nTimeoutStartSec=900\nTimeoutStopSec=600\n");
        if self.options.start_on_boot {
            source.push_str("\n[Install]\nWantedBy=default.target\n");
        }
        Ok(source)
    }
    pub(crate) fn load_for_room(path: impl AsRef<Path>, directory: &Path) -> Result<Self> {
        let unit = Self::load(path)?;
        unit.check_room_directory(directory)?;
        Ok(unit)
    }

    fn check_room_directory(&self, directory: &Path) -> Result<()> {
        ensure!(
            self.cluster == std::path::absolute(directory)?,
            "Quadlet must mount the selected room directory"
        );
        Ok(())
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let source = read_regular(path)?;
        let (unit, fields) = Self::load_source(path, &source)?;
        unit.validate_dropins(
            path.parent().context("unit needs parent")?,
            Some(&fields),
            &fields,
        )?;
        Ok(unit)
    }

    fn load_source(path: &Path, source: &str) -> Result<(Self, Fields)> {
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
        let fields = parse_sections(source)?;
        let get = |section: &str, key: &str| -> Result<String> {
            let values = fields
                .get(&(section.into(), key.into()))
                .with_context(|| format!("missing {section}.{key}"))?;
            ensure!(values.len() == 1, "duplicate {section}.{key}");
            Ok(values[0].clone())
        };
        let mut options = DeploymentOptions {
            image: unquote(&get("Container", "Image")?)?,
            start_on_boot: fields.contains_key(&("Install".into(), "WantedBy".into())),
            ..Default::default()
        };
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
        options.environment = unit_environment(&fields)?;
        let published = options
            .environment
            .remove(PUBLISHED_PORTS_ENV)
            .context("missing published game ports")?;
        let unit = Self {
            name,
            cluster: cluster.into(),
            options,
            published_game_ports: serde_json::from_str(&published)?,
        };
        ensure!(
            parse_sections(&unit.render()?)? == fields,
            "unsupported or inconsistent managed Quadlet fields"
        );
        Ok((unit, fields))
    }
    pub fn validate_update(&self, directory: impl AsRef<Path>) -> Result<()> {
        let directory = directory.as_ref();
        validate_real_directory(directory)?;
        let path = directory.join(self.filename());
        let updated = parse_sections(&self.render()?)?;
        let old = if path.try_exists()? {
            let source = read_regular(&path)?;
            let (unit, fields) = Self::load_source(&path, &source)?;
            unit.check_room_directory(&self.cluster)?;
            Some(fields)
        } else {
            None
        };
        self.validate_dropins(directory, old.as_ref(), &updated)
    }
    fn validate_dropins(
        &self,
        directory: &Path,
        old: Option<&Fields>,
        updated: &Fields,
    ) -> Result<()> {
        let volume = ("Container".into(), "Volume".into());
        let required = &updated[&volume];
        let mut volumes = required.clone();
        let mut fixed = BTreeMap::new();
        for path in dropins(directory, &self.name, "container")? {
            let fields = parse_sections(&read_regular(&path)?)?;
            for (key, mut values) in fields {
                if key == volume {
                    for value in values {
                        if value.is_empty() {
                            volumes.clear();
                        } else {
                            volumes.push(value);
                        }
                    }
                    continue;
                }
                if matches!(
                    (key.0.as_str(), key.1.as_str()),
                    ("Container", "ServiceName" | "StopSignal" | "StopTimeout")
                        | (
                            "Service",
                            "TimeoutStopSec" | "SuccessExitStatus" | "RestartForceExitStatus"
                        )
                ) {
                    fixed.insert(
                        key,
                        (values.pop().expect("parsed assignment"), path.clone()),
                    );
                    continue;
                }
                if key.0 == "Container" && key.1 == "PublishPort" {
                    bail!("drop-in overrides managed ports: {}", path.display());
                }
                if key.0 == "Container" && key.1 == "Environment" {
                    let changed = changed_environment(old, updated)?;
                    for value in values {
                        validate_dropin_environment(&value, &changed).with_context(|| {
                            format!("drop-in overrides managed environment: {}", path.display())
                        })?;
                    }
                } else if old.and_then(|fields| fields.get(&key)) != updated.get(&key) {
                    bail!("drop-in overrides {}.{}: {}", key.0, key.1, path.display());
                }
            }
        }
        for (key, (actual, path)) in fixed {
            let (actual, required) = if key.1 == "ServiceName" {
                // Quadlet uses the last value, including an explicit empty name.
                let name = actual.strip_prefix('"').and_then(|v| v.strip_suffix('"'));
                (name.unwrap_or(&actual), self.name.as_str())
            } else {
                let required = updated
                    .get(&key)
                    .and_then(|values| values.last())
                    .map_or("", String::as_str);
                (actual.as_str(), required)
            };
            ensure!(
                actual == required,
                "drop-in overrides {}.{}: {}",
                key.0,
                key.1,
                path.display()
            );
        }
        validate_room_volumes(required, &volumes)
    }
    pub fn save(&self, directory: impl AsRef<Path>) -> Result<PathBuf> {
        self.prepare_save(directory.as_ref())?.publish()
    }

    pub(crate) fn prepare_save(&self, directory: &Path) -> Result<PreparedUnit> {
        let source = self.render()?;
        ensure!(source.len() as u64 <= LIMIT, "host file exceeds limit");
        self.validate_update(directory)?;
        let path = directory.join(self.filename());
        let replace = match fs::symlink_metadata(&path) {
            Ok(metadata) => {
                ensure!(metadata.is_file(), "refusing to replace a non-regular file");
                true
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
            Err(error) => return Err(error.into()),
        };
        let mut temporary = tempfile::NamedTempFile::new_in(directory)?;
        temporary.write_all(source.as_bytes())?;
        temporary.as_file().sync_all()?;
        Ok(PreparedUnit {
            temporary,
            path,
            replace,
        })
    }
}

fn validate_room_volumes(required: &[String], volumes: &[String]) -> Result<()> {
    let mut mounted = [false; 2];
    for volume in volumes {
        // A one-field Volume creates an anonymous volume at that destination.
        let literal = volume_unescape(volume.split(':').nth(1).unwrap_or(volume))
            .context("cannot resolve Quadlet volume destination")?;
        let destination = Path::new(&literal);
        validate_directory_path(destination).context("invalid Quadlet volume destination")?;
        for (index, target) in ["/cluster", "/install/mods"].iter().enumerate() {
            if destination == Path::new(target) {
                ensure!(
                    !mounted[index] && *volume == required[index],
                    "drop-in overrides the managed {target} volume"
                );
                mounted[index] = true;
            }
        }
    }
    ensure!(
        mounted == [true; 2],
        "drop-in removes a managed room volume"
    );
    Ok(())
}

pub(crate) struct PreparedUnit {
    temporary: tempfile::NamedTempFile,
    path: PathBuf,
    replace: bool,
}

impl PreparedUnit {
    pub(crate) fn publish(self) -> Result<PathBuf> {
        if self.replace {
            self.temporary.persist(&self.path)
        } else {
            self.temporary.persist_noclobber(&self.path)
        }
        .map_err(|error| error.error)?;
        File::open(self.path.parent().context("file needs parent")?)?.sync_all()?;
        Ok(self.path)
    }
}

pub(crate) fn configured_ports(configuration: &ClusterConfig) -> Result<BTreeMap<&str, [u16; 2]>> {
    configuration
        .data()
        .shards
        .iter()
        .map(|(name, shard)| {
            Ok((
                name.as_str(),
                [
                    shard.settings.server_port().try_into()?,
                    shard.settings.master_server_port().try_into()?,
                ],
            ))
        })
        .collect()
}

fn native_ports(cluster: &crate::configuration::Cluster) -> BTreeMap<&str, [u16; 2]> {
    cluster
        .shards
        .iter()
        .map(|shard| {
            (
                shard.name.as_str(),
                [shard.server_port, shard.master_server_port],
            )
        })
        .collect()
}

pub(crate) fn same_shard_ports(
    previous: &crate::configuration::Cluster,
    next: &ClusterConfig,
) -> Result<bool> {
    Ok(native_ports(previous) == configured_ports(next)?)
}

impl ContainerUnit {
    pub(crate) fn retained_ports(&self, next: &ClusterConfig) -> Result<Vec<PortMapping>> {
        let mut ports = Vec::new();
        let next = configured_ports(next)?;
        for (name, pair) in self
            .published_game_ports
            .keys()
            .zip(self.options.ports.as_chunks::<2>().0)
        {
            if let Some(containers) = next.get(name.as_str()) {
                for (&container, mapping) in containers.iter().zip(pair) {
                    ports.push(PortMapping {
                        host: mapping.host,
                        container,
                    });
                }
            }
        }
        Ok(ports)
    }
}

/// Hold the Quadlet directory's allocation lock until the unit has been published.
pub struct PortAllocation {
    _lock: File,
    owner_pid: u32,
    pub mappings: Vec<PortMapping>,
}

impl Drop for PortAllocation {
    fn drop(&mut self) {
        if self.owner_pid == std::process::id() {
            // A child can retain this shared descriptor before exec; only the
            // allocating process may release the lock on drop or failed setup.
            let _ = self._lock.unlock();
        }
    }
}

pub fn validate_port_pool(pool: &std::ops::RangeInclusive<u16>) -> Result<()> {
    ensure!(
        *pool.start() >= 1024 && !pool.is_empty(),
        "port pool must be a nonempty range from 1024 to 65535"
    );
    Ok(())
}

impl PortAllocation {
    pub fn reserve(
        quadlets: impl AsRef<Path>,
        room: u16,
        configuration: &ClusterConfig,
        requested: &[PortMapping],
        pool: std::ops::RangeInclusive<u16>,
    ) -> Result<Self> {
        Self::reserve_for_batch(
            quadlets,
            room,
            configuration,
            requested,
            pool,
            &BTreeSet::new(),
        )
    }

    pub(crate) fn reserve_for_batch(
        quadlets: impl AsRef<Path>,
        room: u16,
        configuration: &ClusterConfig,
        requested: &[PortMapping],
        pool: std::ops::RangeInclusive<u16>,
        batch_ports: &BTreeSet<u16>,
    ) -> Result<Self> {
        let quadlets = quadlets.as_ref();
        validate_real_directory(quadlets)?;
        validate_port_pool(&pool)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .mode(0o600)
            .open(quadlets.join(".dst-host.lock"))?;
        ensure!(
            lock.metadata()?.is_file(),
            "host lock is not a regular file"
        );
        lock.try_lock()
            .context("host deployment is busy or cannot be locked")?;
        let mut allocation = Self {
            _lock: lock,
            owner_pid: std::process::id(),
            mappings: Vec::new(),
        };
        let mut occupied = BTreeSet::new();
        let mut old = BTreeMap::new();
        let current = format!("dst-{room:03}.container");
        for entry in fs::read_dir(quadlets)? {
            let path = entry?.path();
            let (extension, group) = match path.extension().and_then(|value| value.to_str()) {
                Some("container") => ("container", "Container"),
                Some("pod") => ("pod", "Pod"),
                _ => continue,
            };
            let name = path
                .file_stem()
                .and_then(|value| value.to_str())
                .context("invalid Quadlet name")?;
            if path
                .file_name()
                .is_some_and(|name| name == current.as_str())
            {
                let unit = ContainerUnit::load(&path)?;
                old.extend(
                    unit.options
                        .ports
                        .into_iter()
                        .map(|mapping| (mapping.container, mapping.host)),
                );
            } else {
                let mut ports = Vec::new();
                let overrides = dropins(quadlets, name, extension)?;
                for file in std::iter::once(path).chain(overrides) {
                    visit_fields(&read_regular(&file)?, |section, key, value| {
                        if section == group && key == "PublishPort" {
                            if value.is_empty() {
                                ports.clear();
                            } else {
                                ports.push(value.to_owned());
                            }
                        }
                    })?;
                }
                for value in ports {
                    let value = if value.starts_with('"') {
                        unquote(&value)?
                    } else {
                        value
                    };
                    occupied.extend(existing_udp_ports(&value)?);
                }
            }
        }
        let shards = configured_ports(configuration)?;
        let container_ports: BTreeSet<_> = shards.values().flatten().copied().collect();
        let mut preferred = BTreeMap::new();
        let mut reserved = BTreeSet::new();
        for mapping in requested {
            mapping.validate()?;
            ensure!(
                container_ports.contains(&mapping.container),
                "explicit mapping must refer to a shard game or query port"
            );
            ensure!(
                preferred.insert(mapping.container, mapping.host).is_none(),
                "duplicate explicit container port"
            );
            ensure!(
                reserved.insert(mapping.host),
                "duplicate explicit host port"
            );
        }
        let owned: BTreeSet<_> = old.values().copied().collect();
        for (container, host) in old {
            if container_ports.contains(&container)
                && !preferred.contains_key(&container)
                && reserved.insert(host)
            {
                preferred.insert(container, host);
            }
        }
        let mut mappings = Vec::new();
        let mut available = pool;
        for &container in shards.values().flatten() {
            let host = if let Some(&host) = preferred.get(&container) {
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
                            && !reserved.contains(candidate)
                            && !batch_ports.contains(candidate)
                            && (owned.contains(candidate) || udp_available(*candidate))
                    })
                    .context("UDP port pool is exhausted")?
            };
            occupied.insert(host);
            mappings.push(PortMapping { host, container });
        }
        allocation.mappings = mappings;
        Ok(allocation)
    }
}

fn existing_udp_ports(value: &str) -> Result<BTreeSet<u16>> {
    let Some(ports) = value.strip_suffix("/udp") else {
        return Ok(BTreeSet::new());
    };
    let mut parts = ports.rsplit(':');
    let container = parts.next().unwrap_or_default();
    let host = parts.next().unwrap_or_default();
    let range = if host.is_empty() { container } else { host };
    let (first, last) = range.split_once('-').unwrap_or((range, range));
    let first: u16 = first.parse().context("invalid existing UDP port")?;
    let last: u16 = last.parse().context("invalid existing UDP port range")?;
    ensure!(
        first > 0 && first <= last,
        "invalid existing UDP port range"
    );
    if host.is_empty() {
        return Ok(BTreeSet::new());
    }
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

fn visit_fields(source: &str, mut field: impl FnMut(&str, &str, &str)) -> Result<()> {
    let mut section = String::new();
    let mut continued = String::new();
    let mut lines = source.lines().map(str::trim).peekable();
    while let Some(line) = lines.next() {
        if line.is_empty() || line.starts_with(['#', ';']) {
            continue;
        }
        // Quadlet concatenates trimmed lines at each trailing backslash.
        let line = if let Some(prefix) = line.strip_suffix('\\') {
            if lines.peek().is_some() {
                continued.push_str(prefix);
                continue;
            }
            prefix
        } else {
            line
        };
        let line = if continued.is_empty() {
            line
        } else {
            continued.push_str(line);
            &continued
        };
        if let Some(group) = line
            .strip_prefix('[')
            .and_then(|line| line.strip_suffix(']'))
        {
            section.clear();
            section.push_str(group);
        } else {
            let (key, value) = line.split_once('=').context("invalid Quadlet line")?;
            ensure!(
                !section.is_empty() && !key.trim().is_empty(),
                "invalid Quadlet assignment"
            );
            field(&section, key.trim(), value.trim());
        }
        continued.clear();
    }
    Ok(())
}

fn parse_sections(source: &str) -> Result<Fields> {
    let mut result: Fields = BTreeMap::new();
    visit_fields(source, |section, key, value| {
        // systemd applies TimeoutSec to the stop timeout at this position in the unit.
        if section == "Service" && key == "TimeoutSec" {
            result
                .entry((section.into(), "TimeoutStopSec".into()))
                .or_default()
                .push(value.into());
        }
        result
            .entry((section.into(), key.into()))
            .or_default()
            .push(value.into());
    })?;
    Ok(result)
}
fn unit_environment(fields: &Fields) -> Result<BTreeMap<String, String>> {
    let mut values = BTreeMap::new();
    for value in fields
        .get(&("Container".into(), "Environment".into()))
        .into_iter()
        .flatten()
    {
        if value == SYSTEMD_UNIT_ENV {
            continue;
        }
        let assignment = unquote(value)?;
        let (name, value) = assignment
            .split_once('=')
            .context("invalid environment assignment")?;
        ensure!(
            values.insert(name.into(), value.into()).is_none(),
            "duplicate environment variable"
        );
    }
    Ok(values)
}

fn changed_environment(old: Option<&Fields>, new: &Fields) -> Result<BTreeSet<String>> {
    let old = old.map(unit_environment).transpose()?.unwrap_or_default();
    let new = unit_environment(new)?;
    Ok(old
        .keys()
        .chain(new.keys())
        .filter(|name| old.get(*name) != new.get(*name))
        .cloned()
        .collect())
}

fn validate_dropin_environment(value: &str, changed: &BTreeSet<String>) -> Result<()> {
    ensure!(!value.is_empty(), "cannot reset the managed environment");
    // Quadlet accepts several assignments on a line. Only their names are owned here.
    let mut quote = None;
    let mut escaped = false;
    let words = value.split(|ch: char| {
        if std::mem::take(&mut escaped) {
            return false;
        }
        match ch {
            '\\' => escaped = true,
            _ if quote == Some(ch) => quote = None,
            '\'' | '"' if quote.is_none() => quote = Some(ch),
            _ => return quote.is_none() && ch.is_ascii_whitespace(),
        }
        false
    });
    for word in words.filter(|word| !word.is_empty()) {
        let assignment = word.split_once('=');
        let name = assignment
            .map_or(word, |(name, _)| name)
            .trim_matches(['\'', '"']);
        ensure!(
            valid_environment_name(name),
            "expected a literal environment name"
        );
        ensure!(
            name != PUBLISHED_PORTS_ENV
                && (name != SYSTEMD_UNIT_ENV || assignment.is_none())
                && !changed.contains(name),
            "cannot override {name}"
        );
    }
    ensure!(
        quote.is_none() && !escaped,
        "incomplete environment assignment"
    );
    Ok(())
}

fn dropins(directory: &Path, name: &str, extension: &str) -> Result<Vec<PathBuf>> {
    let mut directories = vec![directory.join(format!("{extension}.d"))];
    for (index, character) in name.char_indices() {
        if character == '-' {
            directories.push(directory.join(format!("{}.{extension}.d", &name[..=index])));
        }
    }
    directories.push(directory.join(format!("{name}.{extension}.d")));
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
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::fd::{AsRawFd, FromRawFd};
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
        let units = tempfile::tempdir().unwrap();
        let configuration = cluster(1);
        let allocation =
            PortAllocation::reserve(units.path(), 0, &configuration, &[], 43000..=43100).unwrap();
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
                        _lock: File::from_raw_fd(lock_fd),
                        owner_pid,
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
        let parent_lock_intact =
            PortAllocation::reserve(units.path(), 0, &configuration, &[], 43000..=43100).is_err();
        drop(allocation);
        let next = PortAllocation::reserve(units.path(), 0, &configuration, &[], 43000..=43100);
        parent.write_all(&[1]).unwrap();
        assert!(launched.join().unwrap().success());
        assert!(
            parent_lock_intact,
            "child Drop released its parent's allocation lock"
        );
        assert!(next.is_ok(), "{:#}", next.err().unwrap());
    }
    #[test]
    fn five_world_allocation_is_stable_and_excludes_other_rooms() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let first =
            PortAllocation::reserve(units.path(), 0, &cluster(5), &[], 42000..=42100).unwrap();
        assert_eq!(first.mappings.len(), 10);
        let mappings = first.mappings.clone();
        assert!(PortAllocation::reserve(units.path(), 1, &cluster(1), &[], 42000..=42100).is_err());
        let mut requested = mappings.clone();
        requested.reverse();
        let unit = ContainerUnit::for_cluster(
            "dst-000",
            root.path(),
            &cluster(5),
            DeploymentOptions {
                ports: requested,
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(unit.options.ports, mappings);
        unit.save(units.path()).unwrap();
        drop(first);
        let retry =
            PortAllocation::reserve(units.path(), 0, &cluster(5), &[], 42000..=42100).unwrap();
        assert_eq!(retry.mappings, mappings);
        drop(retry);
        let next =
            PortAllocation::reserve(units.path(), 1, &cluster(2), &[], 42000..=42100).unwrap();
        assert!(
            next.mappings
                .iter()
                .all(|mapping| !mappings.iter().any(|old| old.host == mapping.host))
        );
    }
    #[test]
    fn allocation_reads_dropin_ports_and_releases_unpublished_claims() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        fs::write(
            root.path().join(".dst-ports.json"),
            "unrelated historical port claims",
        )
        .unwrap();
        fs::write(
            units.path().join("other.container"),
            "[Container]\nImage=localhost/fixture\nExec=/bin/echo \\\n# continued argument\n    hello\nPublishPort=42100:12000/udp\n",
        )
        .unwrap();
        let dropin = units.path().join("other.container.d");
        fs::create_dir(&dropin).unwrap();
        fs::write(
            dropin.join("ports.conf"),
            "[Container]\nPublishPort=\nPublishPort=\"42101-42102:12001-12002/udp\"\nPublishPort=42000/udp\nPublishPort=127.0.0.1::42001-42002/udp\n",
        )
        .unwrap();
        fs::write(
            units.path().join("shared.pod"),
            "[Pod]\nPublishPort=42103:12003/udp\n",
        )
        .unwrap();
        let pod_dropin = units.path().join("shared.pod.d");
        fs::create_dir(&pod_dropin).unwrap();
        fs::write(
            pod_dropin.join("ports.conf"),
            "[Pod]\nPublishPort=421\\\n; continued port\n    04:12004/udp\n",
        )
        .unwrap();
        let config = cluster(1);
        let first = PortAllocation::reserve(units.path(), 0, &config, &[], 42100..=42110).unwrap();
        assert_eq!(
            first
                .mappings
                .iter()
                .map(|port| port.host)
                .collect::<Vec<_>>(),
            [42100, 42105]
        );
        let mappings = first.mappings.clone();
        drop(first);
        let retry = PortAllocation::reserve(units.path(), 1, &config, &[], 42100..=42110).unwrap();
        assert_eq!(retry.mappings, mappings);
        assert_eq!(
            fs::read_to_string(root.path().join(".dst-ports.json")).unwrap(),
            "unrelated historical port claims"
        );
        ContainerUnit::for_cluster(
            "dst-001",
            root.path(),
            &config,
            DeploymentOptions {
                ports: mappings,
                ..Default::default()
            },
        )
        .unwrap()
        .save(units.path())
        .unwrap();
        drop(retry);
        fs::create_dir(units.path().join("dst-001.container.d")).unwrap();
        fs::write(
            units.path().join("dst-001.container.d/ports.conf"),
            "[Container]\nPublishPort=42104:12000/udp\n",
        )
        .unwrap();
        assert!(
            PortAllocation::reserve(units.path(), 1, &config, &[], 42100..=42110)
                .err()
                .unwrap()
                .to_string()
                .contains("drop-in overrides managed ports")
        );
    }

    #[test]
    fn partial_explicit_mappings_take_priority_before_allocating_or_reusing_ports() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let mut config = cluster(1).as_value();
        // Only the shard's effective master_port is occupied.
        config["settings"]["master_port"] = json!(11000);
        config["shards"]["Shard0"]["settings"]["master_port"] = json!(12010);
        let config = ClusterConfig::from_value(config).unwrap();
        let reserve = |host| {
            PortAllocation::reserve(
                units.path(),
                1,
                &config,
                &[PortMapping {
                    host,
                    container: 11001,
                }],
                42220..=42221,
            )
            .unwrap()
        };
        let first = reserve(42220);
        assert_eq!(
            (first.mappings[0].host, first.mappings[1].host),
            (42221, 42220)
        );
        ContainerUnit::for_cluster(
            "dst-001",
            root.path(),
            &config,
            DeploymentOptions {
                ports: first.mappings.clone(),
                ..Default::default()
            },
        )
        .unwrap()
        .save(units.path())
        .unwrap();
        drop(first);
        let edited = reserve(42221);
        assert_eq!(
            (edited.mappings[0].host, edited.mappings[1].host),
            (42220, 42221)
        );
    }

    #[test]
    fn managed_units_require_their_service_name_and_shutdown_fields() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let unit = ContainerUnit::for_cluster(
            "dst-001",
            root.path(),
            &cluster(1),
            DeploymentOptions {
                ports: vec![
                    PortMapping {
                        host: 43000,
                        container: 11000,
                    },
                    PortMapping {
                        host: 43001,
                        container: 11001,
                    },
                ],
                ..Default::default()
            },
        )
        .unwrap();
        let current = unit.render().unwrap();
        let old = current
            .replace("StopSignal=SIGTERM\n", "")
            .replace("StopTimeout=540", "StopTimeout=360")
            .replace("TimeoutStopSec=600", "TimeoutStopSec=420");
        let path = units.path().join(unit.filename());
        fs::write(&path, &current).unwrap();
        assert_eq!(ContainerUnit::load(&path).unwrap(), unit);
        for source in [
            old.clone(),
            old.replace("StopTimeout=360", "StopTimeout=540"),
            current.replace("StopTimeout=540", "StopTimeout=360"),
            current.replace("TimeoutStopSec=600", "TimeoutStopSec=420"),
            current.replace("StopSignal=SIGTERM", "StopSignal=SIGINT"),
            current.replace("StopSignal=SIGTERM\n", ""),
            current.replace(
                "StopSignal=SIGTERM",
                "StopSignal=SIGTERM\nStopSignal=SIGTERM",
            ),
            current.replace("StopTimeout=540", "StopTimeout=540\nStopTimeout=540"),
            current.replace("TimeoutStopSec=600", "TimeoutStopSec=601"),
            current.replace("Environment=PODMAN_SYSTEMD_UNIT\n", ""),
            current.replace("43000:11000/udp", "43000:11000/tcp"),
        ] {
            fs::write(&path, &source).unwrap();
            assert!(ContainerUnit::load(&path).is_err());
            assert!(unit.save(units.path()).is_err());
            assert_eq!(fs::read_to_string(&path).unwrap(), source);
        }
        fs::remove_file(&path).unwrap();
        for level in ["container.d", "dst-.container.d", "dst-001.container.d"] {
            let dropin = units.path().join(level);
            fs::create_dir(&dropin).unwrap();
            for (section, key, value) in [
                ("Container", "ServiceName", "renamed"),
                ("Container", "ServiceName", ""),
                ("Container", "ServiceName", "dst-001.service"),
                ("Container", "PublishPort", ""),
                ("Container", "PublishPort", "43002:11000/udp"),
                ("Container", "Environment", ""),
                (
                    "Container",
                    "Environment",
                    "OTHER=up PODMAN_SYSTEMD_UNIT=wrong.service",
                ),
                (
                    "Container",
                    "Environment",
                    "\"DST_SERVER_PUBLISHED_PORTS={}\"",
                ),
                ("Container", "StopSignal", "SIGINT"),
                ("Container", "StopTimeout", "360"),
                ("Service", "TimeoutStopSec", "420"),
                ("Service", "TimeoutSec", "60"),
                ("Service", "SuccessExitStatus", "8"),
                ("Service", "RestartForceExitStatus", "8"),
            ] {
                let override_path = dropin.join("stop.conf");
                fs::write(&override_path, format!("[{section}]\n{key}={value}\n")).unwrap();
                assert!(
                    unit.save(units.path())
                        .unwrap_err()
                        .to_string()
                        .contains("drop-in overrides"),
                    "{level} {key}"
                );
                assert!(!path.exists());
                fs::write(&path, &current).unwrap();
                assert!(ContainerUnit::load_for_room(&path, root.path()).is_err());
                assert!(unit.save(units.path()).is_err());
                fs::remove_file(&path).unwrap();
                fs::remove_file(override_path).unwrap();
            }
            fs::write(dropin.join("memory.conf"), "[Service]\nMemoryMax=4G\n").unwrap();
        }
        fs::write(
            units.path().join("container.d/10-stop.conf"),
            "[Container]\nStopTimeout=60\n",
        )
        .unwrap();
        fs::write(
            units.path().join("dst-001.container.d/10-stop.conf"),
            "[Container]\nStopTimeout=540\n",
        )
        .unwrap();
        fs::write(
            units.path().join("dst-.container.d/20-exit.conf"),
            "[Container]\nServiceName=renamed\n[Service]\nSuccessExitStatus=8\n",
        )
        .unwrap();
        fs::write(
            units.path().join("dst-001.container.d/30-reset.conf"),
            "[Container]\nServiceName=\"dst-001\"\nContainerName=custom\nEnvironment=OTEL_RESOURCE_ATTRIBUTES=dst.cluster.name=local \"LABEL=a PODMAN_SYSTEMD_UNIT=b\" PODMAN_SYSTEMD_UNIT\n[Service]\nSuccessExitStatus=\nTimeoutSec=45\nTimeoutStopSec=600\n",
        )
        .unwrap();
        unit.save(units.path()).unwrap();
        assert_eq!(
            ContainerUnit::load_for_room(&path, root.path()).unwrap(),
            unit
        );
        assert_eq!(fs::read_to_string(path).unwrap(), current);
    }

    #[test]
    fn native_quadlet_roundtrip_conflicts_and_literal_expansions() {
        let root = tempfile::tempdir().unwrap();
        let units = tempfile::tempdir().unwrap();
        let config = cluster(5);
        let allocation =
            PortAllocation::reserve(units.path(), 1, &config, &[], 42200..=42300).unwrap();
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
        assert_eq!(ContainerUnit::load(&path).unwrap(), unit);
        let rendered = unit.render().unwrap();
        assert!(rendered.contains("Restart=on-failure\n"));
        assert!(!rendered.contains("SuccessExitStatus="));
        assert!(!rendered.contains("RestartForceExitStatus="));
        let generator = Path::new("/usr/lib/systemd/system-generators/podman-system-generator");
        if generator.exists() {
            for (name, image) in [("dst-001", DEFAULT_IMAGE), ("dst-002", BETA_IMAGE)] {
                let units = tempfile::tempdir().unwrap();
                let mut options = unit.options.clone();
                options.image = image.into();
                if image != DEFAULT_IMAGE {
                    options.environment.insert(
                        "OTEL_RESOURCE_ATTRIBUTES".into(),
                        "dst.cluster.name=explicit-name".into(),
                    );
                }
                let channel =
                    ContainerUnit::for_cluster(name, &unit.cluster, &config, options).unwrap();
                let path = channel.save(units.path()).unwrap();
                assert_eq!(ContainerUnit::load(path).unwrap(), channel);
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
                assert!(service.contains("--stop-timeout 540"));
                assert!(service.contains("--stop-signal SIGTERM"));
                assert!(service.contains("--init"));
                assert!(service.contains("--tz local"));
                assert!(service.contains("--log-driver passthrough"));
                assert!(service.contains("TimeoutStopSec=600"));
                assert!(service.contains("rm -v -f -i"));
                assert!(service.contains("Restart=on-failure"));
                assert!(service.contains("--label io.containers.autoupdate=registry"));
                assert!(service.contains("--pull never"));
                assert!(service.contains(image));
                assert!(service.contains(&format!("---{name}.service---")));
                assert!(service.contains("Environment=PODMAN_SYSTEMD_UNIT=%n\n"));
                assert!(service.contains("--env PODMAN_SYSTEMD_UNIT "));
                if image != DEFAULT_IMAGE {
                    assert!(
                        service.contains(
                            "--env OTEL_RESOURCE_ATTRIBUTES=dst.cluster.name=explicit-name"
                        )
                    );
                }
            }
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
