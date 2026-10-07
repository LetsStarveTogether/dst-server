//! Decode one room into memory; publishing its files is a separate operation.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{Read, Seek};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{
    compression_worker, configuration,
    native::{self, Kind, NativeFiles, Paths},
    reader::Reader,
};
use crate::{
    files::{self, RoomLock, RoomReader},
    lua,
    settings::{ClusterConfig, PERMISSION_FIELDS},
};

/// Cumulative layers applied to an existing, stopped room.
#[derive(
    Clone,
    Copy,
    Debug,
    Default,
    Eq,
    PartialEq,
    Ord,
    PartialOrd,
    Deserialize,
    Serialize,
    clap::ValueEnum,
)]
#[serde(rename_all = "snake_case")]
pub enum ImportLevel {
    /// World and player saves only.
    Data,
    /// Saves, Mods and world overrides.
    #[default]
    Runtime,
    /// Also replace cluster.ini and server.ini.
    All,
}

/// Validated configuration and native files, retained in memory until explicitly saved.
pub struct ImportedCluster {
    configuration: ClusterConfig,
    files: BTreeMap<PathBuf, Vec<u8>>,
    // Generated IDs do not identify shards in the source room.
    explicit_ids: BTreeSet<String>,
}

impl ImportedCluster {
    pub fn configuration(&self) -> &ClusterConfig {
        &self.configuration
    }

    pub fn files(&self) -> &BTreeMap<PathBuf, Vec<u8>> {
        &self.files
    }

    /// Publish a new directory; existing destinations are never replaced.
    pub fn save(&self, destination: impl AsRef<Path>) -> Result<()> {
        ensure!(
            std::fs::symlink_metadata(destination.as_ref())
                .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound),
            "import destination already exists or cannot be inspected"
        );
        files::write_new_directory(destination.as_ref(), &self.files)
    }

    /// Replace selected layers while preserving the destination's other files.
    pub fn apply(
        &self,
        destination: impl AsRef<Path>,
        level: ImportLevel,
    ) -> Result<ClusterConfig> {
        let room = RoomLock::try_acquire(destination)?;
        let current = if level == ImportLevel::Data {
            ClusterConfig::load(&room)?.as_value()
        } else {
            let paths = ["cluster.ini", "cluster_token.txt"]
                .map(PathBuf::from)
                .into_iter()
                .chain(PERMISSION_FIELDS.map(|name| PathBuf::from(format!("{name}.txt"))))
                .chain(
                    room.shard_directories()?
                        .into_iter()
                        .map(|name| Path::new(&name).join("server.ini")),
                );
            let mut retained = BTreeMap::new();
            for path in paths {
                if let Some(bytes) = room.read_optional_bytes(&path)? {
                    retained.insert(path, bytes);
                }
            }
            ClusterConfig::read_files(&retained)?
        };
        let source = self.configuration.resolved();
        let shards = match_shards(&source, &current, &self.explicit_ids)?;
        let imported = self.configuration.as_value();
        let mut configuration = current;
        if level >= ImportLevel::Runtime {
            for field in ["mod_settings", "downloads"] {
                configuration[field] = imported[field].clone();
            }
        }
        if level == ImportLevel::All {
            configuration["settings"] = imported["settings"].clone();
        }
        let mut replaced = BTreeSet::new();
        for (from, to) in &shards {
            self.validate_save(from)?;
            replaced.insert(Path::new(to).join("save"));
            if level >= ImportLevel::Runtime {
                for (field, file) in [
                    ("mods", "modoverrides.lua"),
                    ("world", "worldgenoverride.lua"),
                    ("level", "leveldataoverride.lua"),
                ] {
                    configuration["shards"][to][field] = imported["shards"][from][field].clone();
                    replaced.insert(Path::new(to).join(file));
                }
            }
            if level == ImportLevel::All {
                configuration["shards"][to]["settings"] =
                    imported["shards"][from]["settings"].clone();
                replaced.insert(Path::new(to).join("server.ini"));
            }
        }
        if level >= ImportLevel::Runtime {
            replaced.extend(
                [
                    "mods/modsettings.lua",
                    "mods/dedicated_server_mods_setup.lua",
                ]
                .map(PathBuf::from),
            );
        }
        if level == ImportLevel::All {
            replaced.insert(PathBuf::from("cluster.ini"));
        }
        let configuration = ClusterConfig::from_value(configuration)?;
        let effective = configuration.resolved();
        let mut changes = BTreeMap::new();
        let mut paths = Paths::default();
        for (path, bytes) in &self.files {
            let mut destination = path.clone();
            let mut contents = Cow::Borrowed(bytes.as_slice());
            if let Some(name) = path.iter().next().and_then(|part| part.to_str())
                && let Some(to) = shards.get(name)
            {
                let encoded = effective["shards"][to]["settings"]["encode_user_path"] == true;
                let converted = native::player_path(path, encoded)?;
                destination = Path::new(to).join(converted.iter().skip(1).collect::<PathBuf>());
                if path == &Path::new(name).join("save/shardindex") {
                    contents = Cow::Owned(native::sanitize_shard_index(
                        std::str::from_utf8(bytes)?,
                        encoded,
                    )?);
                }
            }
            if replaced.iter().any(|root| destination.starts_with(root)) {
                native::validate_path(&destination)?;
                paths.insert(&destination, false)?;
                changes.insert(destination, contents);
            }
        }
        let mut state = room.read_state()?;
        let original = state.clone();
        for key in ["agent_run", "recovery", "policy_state"] {
            state.remove(key);
        }
        if state != original {
            replaced.insert(PathBuf::from(files::STATE_FILE));
            changes.insert(
                PathBuf::from(files::STATE_FILE),
                Cow::Owned(serde_json::to_vec(&state)?),
            );
        }
        room.replace_import(&changes, &replaced)?;
        Ok(configuration)
    }

    fn validate_save(&self, shard: &str) -> Result<()> {
        let bytes = self
            .files
            .get(&Path::new(shard).join("save/shardindex"))
            .with_context(|| format!("shard {shard} has no game saves: missing shardindex"))?;
        let index = lua::parse_return_table(std::str::from_utf8(bytes)?)?;
        let session = index["session_id"]
            .as_str()
            .context("shard index has no session ID")?;
        ensure!(
            files::relative_components(Path::new(session))?.len() == 1,
            "invalid shard session ID"
        );
        let directory = Path::new(shard).join("save/session").join(session);
        let mut latest: Option<(u32, &[u8])> = None;
        for (path, bytes) in &self.files {
            if path.parent() != Some(directory.as_path()) {
                continue;
            }
            let name = path.file_name().unwrap().to_str().unwrap();
            if name.len() != 10 || !name.bytes().all(|byte| byte.is_ascii_digit()) {
                continue;
            }
            let id = name
                .parse::<u32>()
                .with_context(|| format!("invalid world snapshot number: {}", path.display()))?;
            ensure!(id > 0, "world snapshot number must be positive");
            if latest.is_none_or(|(previous, _)| id > previous) {
                latest = Some((id, bytes));
            }
        }
        let (id, bytes) = latest.with_context(|| {
            format!("shard {shard} has no world snapshot for its indexed session")
        })?;
        ensure!(
            !bytes.is_empty(),
            "shard {shard} has an empty latest world snapshot: {id:010}"
        );
        Ok(())
    }
}

fn match_shards(
    source: &Value,
    target: &Value,
    explicit_ids: &BTreeSet<String>,
) -> Result<BTreeMap<String, String>> {
    let source = source["shards"].as_object().unwrap();
    let target = target["shards"].as_object().unwrap();
    let master = |shard: &Value| shard["settings"]["is_master"].as_bool().unwrap_or(true);
    ensure!(
        source.len() == target.len(),
        "import and destination must have the same number of shards"
    );
    let mut matched = BTreeMap::new();
    for (name, shard) in source {
        if let Some(existing) = target.get(name) {
            ensure!(
                master(shard) == master(existing),
                "shard {name} has a different master role in the destination"
            );
            matched.insert(name.clone(), name.clone());
        }
    }
    for (name, shard) in source {
        if matched.contains_key(name) || !explicit_ids.contains(name) {
            continue;
        }
        let candidates: Vec<_> = target
            .iter()
            .filter(|(name, existing)| {
                !matched.values().any(|used| used == *name)
                    && master(shard) == master(existing)
                    && shard["settings"]["id"] == existing["settings"]["id"]
            })
            .collect();
        ensure!(candidates.len() <= 1, "destination has ambiguous shard IDs");
        if let Some((candidate, _)) = candidates.first() {
            matched.insert(name.clone(), (*candidate).clone());
        }
    }
    for (name, shard) in source {
        if matched.contains_key(name) {
            continue;
        }
        let candidates: Vec<_> = target
            .iter()
            .filter(|(name, existing)| {
                !matched.values().any(|used| used == *name) && master(shard) == master(existing)
            })
            .collect();
        ensure!(
            candidates.len() == 1,
            "cannot match imported shard {name}; use matching shard names or explicit IDs"
        );
        matched.insert(name.clone(), candidates[0].0.clone());
    }
    Ok(matched)
}

pub enum ImportSource {
    /// A room directory, a directory containing one room, or a supported archive file.
    Path(PathBuf),
    /// An already opened, seekable supported archive stream, including an export in RAM.
    Archive(File),
}

/// Detect the layout, rebuild configuration and normalize saves entirely in memory.
pub async fn import_cluster(source: ImportSource) -> Result<ImportedCluster> {
    compression_worker(move || {
        let files = match source {
            ImportSource::Path(path) if std::fs::symlink_metadata(&path)?.is_dir() => {
                let root = directory_root(&path)?;
                NativeFiles::read_directory(&RoomReader::open(root)?)?
            }
            ImportSource::Path(path) => {
                let parent = RoomReader::open(
                    path.parent()
                        .filter(|path| !path.as_os_str().is_empty())
                        .unwrap_or(Path::new(".")),
                )?;
                read_archive(parent.open_regular(Path::new(
                    path.file_name().context("archive path has no filename")?,
                ))?)?
            }
            ImportSource::Archive(file) => read_archive(file)?,
        };
        let (configuration, explicit_ids) = configuration::configuration(&files)?;
        let mut files = files.normalize(configuration, None, native::Conversion::Import)?;
        let configuration = files.configuration()?;
        files.entries.extend(
            configuration
                .files()?
                .into_iter()
                .map(|(path, text)| (path, text.into_bytes())),
        );
        Ok(ImportedCluster {
            configuration,
            files: files.entries,
            explicit_ids,
        })
    })
    .await
}

fn shard_directory(path: &Path) -> bool {
    path != Path::new("mods") && native::kind(path) == Some(Kind::Directory)
}

fn wrapper_directory(name: &str) -> bool {
    !name.starts_with('.') && !native::noise(name)
}

fn directory_root(path: &Path) -> Result<PathBuf> {
    let reader = RoomReader::open(path)?;
    let is_root = |reader: &RoomReader| -> Result<bool> {
        if reader.read_optional_bytes("cluster.ini")?.is_some() {
            return Ok(true);
        }
        for (name, kind) in reader.list_directory("")? {
            if kind.is_dir() && shard_directory(Path::new(&name)) {
                for marker in ["server.ini", "save/shardindex"] {
                    if reader
                        .read_optional_bytes(Path::new(&name).join(marker))?
                        .is_some()
                    {
                        return Ok(true);
                    }
                }
            }
        }
        Ok(false)
    };
    let mut roots = Vec::new();
    let direct = is_root(&reader)?;
    if direct {
        roots.push(path.to_owned());
    }
    for (name, kind) in reader.list_directory("")? {
        if kind.is_dir() && wrapper_directory(&name) && !(direct && name == "mods") {
            let child = path.join(name);
            if is_root(&RoomReader::open(&child)?)? {
                roots.push(child);
            }
        }
    }
    ensure!(
        roots.len() == 1,
        "import requires exactly one room at the root or one directory below it"
    );
    Ok(roots.remove(0))
}

#[derive(PartialEq, Eq)]
pub(super) struct Entry {
    path: PathBuf,
    directory: bool,
    size: u64,
}

impl Entry {
    pub(super) fn new(name: &str, directory: bool, size: u64, mode: Option<u32>) -> Result<Self> {
        ensure!(
            size <= native::MAX_BYTES,
            "archive entry exceeds the byte limit"
        );
        ensure!(
            !directory || size == 0,
            "archive directories cannot contain file data"
        );
        if let Some(mode) = mode {
            let kind = mode & libc::S_IFMT;
            ensure!(
                kind == 0
                    || kind
                        == if directory {
                            libc::S_IFDIR
                        } else {
                            libc::S_IFREG
                        },
                "archive entries must be regular files or directories"
            );
        }
        let name = name.trim_start_matches("./");
        if directory && name.is_empty() {
            return Ok(Self {
                path: PathBuf::new(),
                directory,
                size,
            });
        }
        let name = if directory {
            name.strip_suffix('/').unwrap_or(name)
        } else {
            name
        };
        ensure!(
            !name
                .split('/')
                .any(|part| part.is_empty() || matches!(part, "." | "..")),
            "archive path must be relative without traversal"
        );
        let path = PathBuf::from(name);
        // Allow one outer directory; validate the saved path after finding the root.
        native::validate_path(Path::new(path.iter().next().unwrap()))?;
        let unwrapped = path.iter().skip(1).collect::<PathBuf>();
        if !unwrapped.as_os_str().is_empty() {
            native::validate_path(&unwrapped)?;
        }
        if !directory
            && [path.as_path(), unwrapped.as_path()]
                .into_iter()
                .any(|path| native::kind(path) == Some(Kind::Configuration))
        {
            ensure!(
                size <= files::MAX_FILE_BYTES as u64,
                "configuration file exceeds the byte limit"
            );
        }
        Ok(Self {
            path,
            directory,
            size,
        })
    }
}

fn archive_root(entries: &[Entry]) -> Result<PathBuf> {
    let mut paths = Paths::default();
    let mut roots = BTreeSet::new();
    for entry in entries {
        if entry.path.as_os_str().is_empty() {
            continue;
        }
        paths.insert(&entry.path, entry.directory)?;
        if !entry.directory {
            let depth = if entry
                .path
                .file_name()
                .is_some_and(|name| name == "cluster.ini")
            {
                Some(1)
            } else if entry
                .path
                .file_name()
                .is_some_and(|name| name == "server.ini")
            {
                Some(2)
            } else if entry.path.ends_with("save/shardindex") {
                Some(3)
            } else {
                None
            };
            if let Some(depth) = depth {
                let count = entry.path.components().count();
                if (depth..=depth + 1).contains(&count) {
                    let root = entry.path.iter().take(count - depth).collect::<PathBuf>();
                    let relative = entry.path.strip_prefix(&root)?;
                    if wrapper_directory(root.to_str().unwrap())
                        && native::kind(relative) == Some(Kind::Configuration)
                        && (depth == 1
                            || shard_directory(Path::new(relative.iter().next().unwrap())))
                    {
                        roots.insert(root);
                    }
                }
            }
        }
    }
    if roots.contains(Path::new("")) {
        roots.remove(Path::new("mods"));
    }
    ensure!(
        roots.len() == 1,
        "import requires exactly one room at the root or one directory below it"
    );
    let root = roots.pop_first().unwrap();
    for entry in entries {
        if let Ok(path) = entry.path.strip_prefix(&root)
            && !path.as_os_str().is_empty()
        {
            native::validate_path(path)?;
        }
    }
    Ok(root)
}

fn selected<'a>(entry: &'a Entry, root: &Path) -> Option<&'a Path> {
    let path = entry.path.strip_prefix(root).ok()?;
    (!entry.directory
        && matches!(
            native::kind(path),
            Some(Kind::Configuration | Kind::Progress)
        ))
    .then_some(path)
}

fn read_archive(mut file: File) -> Result<NativeFiles> {
    ensure!(
        file.metadata()?.is_file(),
        "archive stream must be a regular file"
    );
    ensure!(
        file.metadata()?.len() <= native::MAX_BYTES,
        "archive exceeds the byte limit"
    );
    file.rewind()?;
    let mut magic = [0; 6];
    file.read_exact(&mut magic)
        .context("read archive signature")?;
    file.rewind()?;
    if magic == *b"7z\xbc\xaf\x27\x1c" {
        // libarchive does not expose 7z deletion entries or Windows reparse points.
        let archive = sevenz_rust2::Archive::read(&mut file, &sevenz_rust2::Password::empty())?;
        ensure!(
            archive.files.len() <= native::MAX_ENTRIES,
            "archive has too many entries"
        );
        for entry in archive.files {
            ensure!(
                !entry.is_anti_item
                    && (!entry.has_windows_attributes || entry.windows_attributes & 0x400 == 0),
                "archive links and deletion entries are unsupported"
            );
            Entry::new(
                &entry.name,
                entry.is_directory,
                entry.size,
                entry
                    .has_windows_attributes
                    .then_some(entry.windows_attributes >> 16),
            )?;
        }
    } else if magic.starts_with(b"PK") {
        super::zip::preflight_directory(&mut file, native::MAX_BYTES, native::MAX_ENTRIES)?;
    } else if magic == *b"Rar!\x1a\x07" {
        check_rar(&mut file)?;
    } else if magic.starts_with(&[0x1f, 0x8b]) {
        // libarchive's gzip filter does not validate the CRC or uncompressed size.
        let decoder = flate2::read::MultiGzDecoder::new(&mut file);
        ensure!(
            std::io::copy(
                &mut decoder.take(native::MAX_BYTES + 1),
                &mut std::io::sink()
            )? <= native::MAX_BYTES,
            "archive exceeds the unpacked byte limit"
        );
    }
    file.rewind()?;
    let mut entries = Vec::new();
    {
        let mut reader = Reader::open(&mut file, &magic)?;
        let mut bytes = 0;
        while let Some(entry) = reader.next()? {
            bytes += entry.size;
            ensure!(
                bytes <= native::MAX_BYTES,
                "archive exceeds the unpacked byte limit"
            );
            ensure!(
                entries.len() < native::MAX_ENTRIES,
                "archive has too many entries"
            );
            entries.push(entry);
        }
    }
    let root = archive_root(&entries)?;
    file.rewind()?;
    let mut files = NativeFiles::default();
    let mut reader = Reader::open(&mut file, &magic)?;
    for entry in entries {
        ensure!(
            reader.next()?.as_ref() == Some(&entry),
            "archive changed during import"
        );
        if entry.directory {
            continue;
        }
        if let Some(path) = selected(&entry, &root) {
            read_file(&mut files, path, entry.size, &mut reader)?;
        } else {
            ensure!(
                std::io::copy(
                    &mut (&mut reader).take(entry.size + 1),
                    &mut std::io::sink()
                )? == entry.size,
                "archive entry length is inconsistent"
            );
        }
    }
    ensure!(reader.next()?.is_none(), "archive changed during import");
    Ok(files)
}

fn read_file(files: &mut NativeFiles, path: &Path, size: u64, reader: &mut dyn Read) -> Result<()> {
    files.read(path, size, &mut reader.take(size + 1))?;
    ensure!(
        files.entries[path].len() as u64 == size,
        "archive entry length is inconsistent"
    );
    Ok(())
}

fn check_rar(file: &mut File) -> Result<()> {
    // libarchive can accept the last volume alone, losing earlier files.
    let mut header = [0; 12];
    file.read_exact(&mut header)?;
    if header[6] == 0 {
        ensure!(header[9] == 0x73, "invalid RAR4 main header");
        let flags = u16::from_le_bytes([header[10], header[11]]);
        ensure!(
            flags & 0x81 == 0,
            "encrypted and multipart RAR archives are unsupported"
        );
        ensure!(flags & 0x08 == 0, "solid RAR4 archives are unsupported");
    } else {
        ensure!(
            &header[..8] == b"Rar!\x1a\x07\x01\x00",
            "invalid RAR signature"
        );
        let size = rar_integer(file)?;
        ensure!(size <= 2 * 1024 * 1024, "RAR header exceeds the byte limit");
        let mut header = file.take(size);
        ensure!(
            rar_integer(&mut header)? == 1,
            "RAR archive must start with an unencrypted main header"
        );
        let flags = rar_integer(&mut header)?;
        ensure!(flags & 2 == 0, "invalid RAR5 main header");
        if flags & 1 != 0 {
            rar_integer(&mut header)?;
        }
        ensure!(
            rar_integer(&mut header)? & 1 == 0,
            "multipart RAR archives are unsupported"
        );
    }
    Ok(())
}

fn rar_integer(reader: &mut impl Read) -> Result<u64> {
    let mut value = 0;
    for shift in (0..64).step_by(7) {
        let mut byte = [0];
        reader.read_exact(&mut byte)?;
        ensure!(shift != 63 || byte[0] <= 1, "RAR integer overflow");
        value |= u64::from(byte[0] & 0x7f) << shift;
        if byte[0] & 0x80 == 0 {
            return Ok(value);
        }
    }
    bail!("RAR integer overflow")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn shard_matching_uses_ids_for_multiple_secondaries_and_rejects_ambiguity() {
        let shard = |master, id| json!({"settings":{"is_master":master,"id":id}});
        let mut source = json!({"shards":{"Master":shard(true, 1),"Caves":shard(false, 2),"Extra":shard(false, 3)}});
        let mut target = json!({"shards":{"Surface":shard(true, 1),"Below":shard(false, 3),"Under":shard(false, 2)}});
        let explicit_ids = ["Master", "Caves", "Extra"].map(String::from).into();
        assert_eq!(
            match_shards(&source, &target, &explicit_ids).unwrap(),
            BTreeMap::from([
                ("Master".into(), "Surface".into()),
                ("Caves".into(), "Under".into()),
                ("Extra".into(), "Below".into()),
            ])
        );
        source["shards"]["Caves"]["settings"]["id"] = Value::Null;
        assert!(match_shards(&source, &target, &BTreeSet::new()).is_err());
        let master = target["shards"]
            .as_object_mut()
            .unwrap()
            .remove("Surface")
            .unwrap();
        target["shards"]["Caves"] = master;
        assert!(
            match_shards(&source, &target, &explicit_ids)
                .unwrap_err()
                .to_string()
                .contains("different master role")
        );
    }
}
