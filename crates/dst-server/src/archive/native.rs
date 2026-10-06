//! The native files and transformations shared by import and export.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::FileType;
use std::io::Read;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use serde_json::Value;

use crate::files::{self, IniDocument, RoomReader, render_ini};
use crate::settings::{ClusterConfig, event_game_mode};
use crate::{configuration, lua};

pub(super) const MAX_ENTRIES: usize = 100_000;
pub(super) const MAX_BYTES: u64 = 2 * 1024 * 1024 * 1024;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Conversion {
    None,
    ToClient,
    Import,
}

#[derive(Clone, Copy, PartialEq)]
pub(super) enum Kind {
    Directory,
    Configuration,
    Progress,
}

/// A whitelist also lets directory reads skip downloaded Mods and caches entirely.
pub(super) fn kind(path: &Path) -> Option<Kind> {
    let parts: Vec<_> = path
        .iter()
        .map(|name| name.to_str())
        .collect::<Option<_>>()?;
    if parts.iter().any(|name| noise(name))
        || parts.first().is_some_and(|name| name.starts_with('.'))
    {
        return None;
    }
    use Kind::{Configuration, Directory, Progress};
    match parts.as_slice() {
        [] => Some(Directory),
        ["cluster.ini"] => Some(Configuration),
        [
            "mods",
            "modsettings.lua" | "dedicated_server_mods_setup.lua",
        ] => Some(Configuration),
        ["mods", ..] if parts.len() > 1 => None,
        [name] if !name.starts_with('.') && configuration::validate_shard_name(name).is_ok() => {
            Some(Directory)
        }
        ["mods"] => Some(Directory),
        [
            _,
            "server.ini" | "worldgenoverride.lua" | "leveldataoverride.lua" | "modoverrides.lua",
        ] => Some(Configuration),
        [_, "save"] | [_, "save", "session" | "mod_config_data"] => Some(Directory),
        [_, "save", "shardindex" | "saveindex"] => Some(Configuration),
        [
            _,
            "save",
            "recipebook" | "reforged_achievements_server" | "reforged_exp_server",
        ] => Some(Progress),
        [_, "save", "session", ..] => Some(Progress),
        [_, "save", "mod_config_data", name] if name.starts_with("mod_worldjump_data_") => {
            Some(Progress)
        }
        _ => None,
    }
}

pub(super) fn noise(name: &str) -> bool {
    sdk_entry(name)
        || matches!(name, "__MACOSX" | ".DS_Store" | "Thumbs.db" | "desktop.ini")
        || name.starts_with("._")
}

pub(super) fn shard_directory(name: impl AsRef<Path>) -> bool {
    let name = name.as_ref();
    !name.as_os_str().is_empty() && name != Path::new("mods") && kind(name) == Some(Kind::Directory)
}

pub(super) fn select_shards(
    conversion: Conversion,
    mut with_file: impl FnMut(&Path) -> Result<BTreeSet<String>>,
) -> Result<BTreeSet<String>> {
    let shards = with_file(Path::new("server.ini"))?;
    // Dedicated rooms retain saves after a shard is removed. Client slots can have only indexes.
    if shards.is_empty() && conversion == Conversion::Import {
        with_file(Path::new("save/shardindex"))
    } else {
        Ok(shards)
    }
}

pub(super) fn directory_shards(
    room: &RoomReader,
    entries: &[(String, FileType)],
    conversion: Conversion,
) -> Result<BTreeSet<String>> {
    select_shards(conversion, |marker| {
        let mut shards = BTreeSet::new();
        for (name, entry) in entries {
            if entry.is_dir()
                && shard_directory(name)
                && room.has_file(&Path::new(name).join(marker))?
            {
                shards.insert(name.clone());
            }
        }
        Ok(shards)
    })
}

/// Include directory prefixes; raw Klei player IDs retain their significant case.
#[derive(Default)]
pub(super) struct Paths(BTreeMap<PathBuf, (PathBuf, bool)>);

impl Paths {
    pub(super) fn insert(&mut self, path: &Path, directory: bool) -> Result<()> {
        let mut prefix = PathBuf::new();
        let mut folded = PathBuf::new();
        for part in path.iter() {
            prefix.push(part);
            let is_directory = prefix != path || directory;
            let name = part.to_str().context("archive path is not UTF-8")?;
            let session = prefix.parent().and_then(Path::parent);
            let player = name.starts_with("KU_")
                && session
                    .and_then(Path::file_name)
                    .is_some_and(|name| name == "session")
                && session
                    .and_then(Path::parent)
                    .and_then(Path::file_name)
                    .is_some_and(|name| name == "save");
            folded.push(if player {
                name.to_owned()
            } else {
                name.to_lowercase()
            });
            if let Some((previous, was_directory)) = self.0.get(&folded) {
                ensure!(
                    previous == &prefix && *was_directory && is_directory,
                    "archive paths collide"
                );
            } else {
                self.0
                    .insert(folded.clone(), (prefix.clone(), is_directory));
            }
        }
        Ok(())
    }
}

#[derive(Default)]
pub(super) struct NativeFiles {
    pub(super) entries: BTreeMap<PathBuf, Vec<u8>>,
    paths: Paths,
    bytes: u64,
}

impl NativeFiles {
    pub(super) fn read_directory(room: &RoomReader, conversion: Conversion) -> Result<Self> {
        let entries = room.list_directory("")?;
        let shards = directory_shards(room, &entries, conversion)?;
        let mut files = Self::default();
        for (name, entry) in entries {
            if entry.is_dir() && shard_directory(&name) && !shards.contains(&name) {
                continue;
            }
            files.visit(room, Path::new(&name), entry)?;
        }
        Ok(files)
    }

    fn visit(&mut self, room: &RoomReader, path: &Path, entry: FileType) -> Result<()> {
        let Some(kind) = kind(path) else {
            return Ok(());
        };
        validate_path(path)?;
        ensure!(
            !entry.is_symlink() && (entry.is_dir() || entry.is_file()),
            "save entry must be a regular file or directory"
        );
        if entry.is_dir() {
            ensure!(
                kind != Kind::Configuration,
                "native configuration must be a regular file"
            );
            self.paths.insert(path, true)?;
            for (name, child) in room.list_directory(path)? {
                self.visit(room, &path.join(name), child)?;
            }
        } else if kind != Kind::Directory {
            let mut file = room.open_regular(path)?;
            self.read(path, file.metadata()?.len(), &mut file)?;
        }
        Ok(())
    }

    pub(super) fn read(&mut self, path: &Path, size: u64, source: &mut dyn Read) -> Result<()> {
        let limit = if kind(path) == Some(Kind::Configuration) {
            files::MAX_FILE_BYTES as u64
        } else {
            MAX_BYTES - self.bytes
        };
        ensure!(
            size <= limit && self.bytes + size <= MAX_BYTES,
            "native save exceeds the byte limit"
        );
        ensure!(
            self.entries.len() < MAX_ENTRIES,
            "native save has too many files"
        );
        let mut bytes = Vec::new();
        source.take(limit + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= limit,
            "native save exceeds the byte limit"
        );
        self.insert(path.to_owned(), bytes)
    }

    pub(super) fn insert(&mut self, path: PathBuf, bytes: Vec<u8>) -> Result<()> {
        validate_path(&path)?;
        self.paths.insert(&path, false)?;
        self.bytes += bytes.len() as u64;
        ensure!(
            self.bytes <= MAX_BYTES,
            "native save exceeds the byte limit"
        );
        self.entries.insert(path, bytes);
        Ok(())
    }

    pub(super) fn text(&self, path: &Path) -> Result<&str> {
        let bytes = self
            .entries
            .get(path)
            .with_context(|| format!("missing native file: {}", path.display()))?;
        ensure!(
            bytes.len() <= files::MAX_FILE_BYTES,
            "configuration file exceeds the byte limit"
        );
        std::str::from_utf8(bytes).context("native configuration is not UTF-8")
    }

    pub(super) fn configuration(&self) -> Result<ClusterConfig> {
        ClusterConfig::from_files(&self.entries)
    }

    fn normalized_index(
        &self,
        shard: &str,
        encode: bool,
        conversion: Conversion,
    ) -> Result<Option<Vec<u8>>> {
        let save = Path::new(shard).join("save");
        let index = self.entries.get(&save.join("shardindex"));
        ensure!(
            index.is_some() || !self.entries.contains_key(&save.join("saveindex")),
            "legacy saveindex must be migrated by the game before transfer"
        );
        let index = index
            .map(|bytes| lua::parse_return_table(std::str::from_utf8(bytes)?))
            .transpose()?;
        let session = match index.as_ref().and_then(|index| index.get("session_id")) {
            None | Some(Value::Null) => "",
            Some(value) => value
                .as_str()
                .context("shard session ID must be a string")?,
        };
        if session.is_empty() {
            ensure!(
                !has_session_files(&self.entries, shard),
                "shard {shard} has session files but no active session in shardindex"
            );
        } else {
            self.validate_snapshot(shard, session)?;
        }
        index
            .map(|index| sanitize_shard_index(index, encode, conversion))
            .transpose()
    }

    fn validate_snapshot(&self, shard: &str, session: &str) -> Result<()> {
        ensure!(
            files::relative_components(Path::new(session))?.len() == 1,
            "invalid shard session ID"
        );
        let directory = Path::new(shard).join("save/session").join(session);
        let mut latest: Option<(u32, &[u8])> = None;
        for (path, bytes) in self
            .entries
            .range(directory.clone()..)
            .take_while(|(path, _)| path.starts_with(&directory))
        {
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
            // Fixed-width snapshot names sort by sequence in the file map.
            latest = Some((id, bytes));
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

    pub(super) fn normalize(
        self,
        source: ClusterConfig,
        replacement: Option<&ClusterConfig>,
        conversion: Conversion,
    ) -> Result<Self> {
        let configuration = replacement.unwrap_or(&source);
        let shards = &source.data().shards;
        let configured = &configuration.data().shards;
        ensure!(
            shards.len() == configured.len()
                && shards.iter().all(|(name, shard)| configured
                    .get(name)
                    .is_some_and(|other| shard.settings.is_master() == other.settings.is_master())),
            "archive configuration must match source shard topology"
        );
        ensure!(
            conversion != Conversion::ToClient || shards.len() == 2,
            "client export requires exactly two shards: one master and one secondary"
        );
        if replacement.is_some() {
            let from = source.data().settings().game_mode();
            let to = configuration.data().settings().game_mode();
            ensure!(
                event_game_mode(&from) == event_game_mode(&to)
                    || !shards
                        .keys()
                        .any(|name| has_session_files(&self.entries, name)),
                "cannot export {from} saves with replacement game mode {to}"
            );
        }
        let mut targets = BTreeMap::new();
        let mut output = Self::default();
        for (name, shard) in shards {
            let encoded = shard.settings.encode_user_path();
            ensure!(
                shard.settings.is_master()
                    || shard.settings.id() == configured[name].settings.id()
                    || !has_session_files(&self.entries, name),
                "archive configuration must preserve the source shard ID for saved world {name}"
            );
            ensure!(
                conversion != Conversion::None
                    || encoded == configured[name].settings.encode_user_path(),
                "archive configuration must preserve source player path encoding"
            );
            let target = match conversion {
                Conversion::None => (name.as_str(), encoded),
                Conversion::ToClient => (
                    if shard.settings.is_master() {
                        "Master"
                    } else {
                        "Caves"
                    },
                    true,
                ),
                Conversion::Import => (name.as_str(), false),
            };
            if let Some(index) = self.normalized_index(name, target.1, conversion)? {
                output.insert(Path::new(target.0).join("save/shardindex"), index)?;
            }
            targets.insert(name.as_str(), target);
        }
        let remap = |path: &Path| {
            let mut parts = path.iter();
            let first = parts.next().unwrap().to_str().unwrap();
            targets.get(first).map_or_else(
                || path.to_owned(),
                |(name, _)| Path::new(name).join(parts.as_path()),
            )
        };
        for (path, mut contents) in configuration.files()? {
            if kind(&path) != Some(Kind::Configuration) {
                continue;
            }
            if path.extension().is_some_and(|extension| extension == "ini") {
                let mut ini = configuration::Ini::parse(&path, &contents, None)?.into_sections();
                sanitize_ini(&mut ini);
                if path == Path::new("cluster.ini") {
                    ini.entry("shard".into())
                        .or_default()
                        .insert("cluster_key".into(), crate::settings::new_cluster_key()?);
                } else if path.file_name().is_some_and(|name| name == "server.ini") {
                    let (_, encoded) = targets[path.iter().next().unwrap().to_str().unwrap()];
                    ini.entry("account".into())
                        .or_default()
                        .insert("encode_user_path".into(), encoded.to_string());
                }
                contents = render_ini(&ini)?;
            }
            output.insert(remap(&path), contents.into_bytes())?;
        }
        let mut players = BTreeMap::new();
        for (path, bytes) in self.entries {
            let mut parts = path.iter();
            let name = parts.next().unwrap().to_str().unwrap();
            let Ok(save_path) = parts.as_path().strip_prefix("save") else {
                continue;
            };
            if save_path == Path::new("saveindex") || save_path == Path::new("shardindex") {
                continue;
            }
            let shard = shards
                .get(name)
                .context("save files have no matching shard configuration")?;
            let encoded = shard.settings.encode_user_path();
            let (_, target_encoded) = targets[name];
            let mut target = remap(&path);
            if (encoded != target_encoded || conversion == Conversion::Import)
                && save_path.starts_with("session")
                && save_path.iter().count() >= 4
            {
                let renamed = player_path(&path, target_encoded)?;
                let player: PathBuf = renamed.iter().take(5).collect();
                let original: PathBuf = path.iter().take(5).collect();
                if let Some(previous) = players.insert(player, original.clone()) {
                    ensure!(
                        previous == original,
                        "player save directories collide after conversion"
                    );
                }
                target = remap(&renamed);
            }
            output.insert(target, bytes)?;
        }
        Ok(output)
    }
}

pub(super) fn has_session_files(entries: &BTreeMap<PathBuf, Vec<u8>>, shard: &str) -> bool {
    let sessions = Path::new(shard).join("save/session");
    entries
        .range(sessions.clone()..)
        .next()
        .is_some_and(|(path, _)| path.starts_with(&sessions))
}

fn sanitize_ini(ini: &mut IniDocument) {
    for fields in ini.values_mut() {
        fields.retain(|key, _| {
            ![
                "password",
                "cluster_password",
                "cluster_key",
                "cluster_token",
                "token",
                "steam_group_id",
                "steam_group_only",
                "steam_group_admins",
            ]
            .contains(&key.as_str())
        });
    }
    ini.retain(|_, fields| !fields.is_empty());
}

pub(super) fn sanitize_shard_index(
    mut index: Value,
    encode: bool,
    conversion: Conversion,
) -> Result<Vec<u8>> {
    if conversion == Conversion::ToClient
        && let Some(mods) = index
            .get_mut("enabled_mods")
            .and_then(serde_json::Value::as_object_mut)
    {
        // The client loads every entry as a Mod configuration table.
        mods.remove("client_mods_disabled");
    }
    let server = index
        .get_mut("server")
        .and_then(serde_json::Value::as_object_mut)
        .context("shard index must contain a server table")?;
    for key in [
        "password",
        "cluster_password",
        "cluster_key",
        "cluster_token",
        "token",
        "clan",
    ] {
        server.remove(key);
    }
    if server
        .get("privacy_type")
        .and_then(serde_json::Value::as_u64)
        == Some(3)
    {
        server.insert("privacy_type".into(), 0.into());
    }
    server.insert("encode_user_path".into(), encode.into());
    Ok(format!("KLEI     1 return {}\n", lua::render_literal(&index)?).into_bytes())
}

pub(super) fn player_path(path: &Path, encoded: bool) -> Result<PathBuf> {
    let mut parts: Vec<_> = path
        .iter()
        .map(|part| part.to_str().unwrap().to_owned())
        .collect();
    if parts.len() >= 6 && parts[1] == "save" && parts[2] == "session" {
        if parts[4].starts_with("KU_") {
            let userid = if parts[4].len() == 12 && parts[4].ends_with('_') {
                &parts[4][..11]
            } else {
                &parts[4]
            };
            let name = encode_klei_id(userid)?;
            parts[4] = if encoded { name } else { format!("{userid}_") };
        } else if !encoded && parts[4].starts_with("A7") {
            parts[4] = format!("{}_", decode_klei_id(&parts[4])?);
        }
    }
    Ok(parts.iter().collect())
}

fn sdk_entry(name: &str) -> bool {
    matches!(name, ".last_login" | "dst_server_driver.json")
        || [
            ".dst-",
            "..dst-",
            "..last_login.",
            ".dst_server_driver.json.",
        ]
        .iter()
        .any(|prefix| name.starts_with(prefix))
}

pub(super) fn validate_path(path: &Path) -> Result<()> {
    for name in files::relative_components(path)? {
        ensure!(
            !name.ends_with(['.', ' '])
                && !name
                    .chars()
                    .any(|c| c.is_control() || "\\:*?\"<>|".contains(c)),
            "save path is unsafe on Windows"
        );
        let base = name
            .split('.')
            .next()
            .unwrap_or_default()
            .trim_end_matches(' ')
            .to_uppercase();
        let reserved = ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"].contains(&base.as_str())
            || ["COM", "LPT"].iter().any(|prefix| {
                base.strip_prefix(prefix).is_some_and(|suffix| {
                    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³"].contains(&suffix)
                })
            });
        ensure!(!reserved, "save path is unsafe on Windows");
    }
    Ok(())
}

const ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_";
const PATH_ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUV";

pub fn encode_klei_id(userid: &str) -> Result<String> {
    ensure!(
        userid.len() == 11 && userid.starts_with("KU_"),
        "Klei ID must match KU_[0-9A-Za-z_-] with eight ID characters"
    );
    let mut bits = 0_u64;
    for (index, byte) in userid.bytes().enumerate() {
        if index == 2 {
            continue;
        }
        let value = ALPHABET
            .iter()
            .position(|candidate| *candidate == byte)
            .context("invalid Klei ID character")?;
        bits = (bits << 6) | value as u64;
    }
    Ok((0..12)
        .rev()
        .map(|index| PATH_ALPHABET[((bits >> (index * 5)) & 31) as usize] as char)
        .collect())
}

pub(super) fn decode_klei_id(encoded: &str) -> Result<String> {
    ensure!(
        encoded.len() == 12,
        "encoded Klei ID must contain 12 characters from 0-9 and A-V"
    );
    let mut bits = 0_u64;
    for byte in encoded.bytes() {
        let value = PATH_ALPHABET
            .iter()
            .position(|candidate| *candidate == byte)
            .context("encoded Klei ID must contain 12 characters from 0-9 and A-V")?;
        bits = (bits << 5) | value as u64;
    }
    let mut userid: String = (0..10)
        .rev()
        .map(|index| ALPHABET[((bits >> (index * 6)) & 63) as usize] as char)
        .collect();
    ensure!(
        userid.starts_with("KU"),
        "encoded path does not contain a Klei ID"
    );
    userid.insert(2, '_');
    Ok(userid)
}
