//! The native files and transformations shared by import and export.

use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};

use crate::files::{self, IniDocument, RoomReader, render_ini};
use crate::settings::ClusterConfig;
use crate::{configuration, lua};

pub(super) const MAX_ENTRIES: usize = 100_000;
pub(super) const MAX_BYTES: u64 = 2 * 1024 * 1024 * 1024;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Conversion {
    None,
    ToClient,
    FromClient,
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
    if parts.iter().any(|name| noise(name)) {
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

fn noise(name: &str) -> bool {
    sdk_entry(name)
        || matches!(name, "__MACOSX" | ".DS_Store" | "Thumbs.db" | "desktop.ini")
        || name.starts_with("._")
}

/// Include directory prefixes; raw Klei player IDs retain their significant case.
#[derive(Default)]
pub(super) struct Paths(BTreeMap<PathBuf, (PathBuf, bool)>);

impl Paths {
    pub(super) fn insert(&mut self, path: &Path, directory: bool) -> Result<()> {
        validate_path(path)?;
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
    pub(super) fn read_directory(room: &RoomReader) -> Result<Self> {
        let mut files = Self::default();
        files.visit(room, Path::new(""))?;
        Ok(files)
    }

    fn visit(&mut self, room: &RoomReader, parent: &Path) -> Result<()> {
        for (name, entry) in room.list_directory(parent)? {
            let path = parent.join(name);
            let Some(kind) = kind(&path) else { continue };
            validate_path(&path)?;
            ensure!(
                !entry.is_symlink() && (entry.is_dir() || entry.is_file()),
                "save entry must be a regular file or directory"
            );
            if entry.is_dir() {
                ensure!(
                    kind != Kind::Configuration,
                    "native configuration must be a regular file"
                );
                self.paths.insert(&path, true)?;
                self.visit(room, &path)?;
            } else if kind != Kind::Directory {
                let mut file = room.open_regular(&path)?;
                self.read(&path, file.metadata()?.len(), &mut file)?;
            }
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

    pub(super) fn normalize(
        self,
        source: ClusterConfig,
        replacement: Option<&ClusterConfig>,
        conversion: Conversion,
    ) -> Result<Self> {
        let configuration = replacement.unwrap_or(&source);
        let source = source.resolved();
        let target = configuration.resolved();
        let shards = source["shards"]
            .as_object()
            .context("archive configuration has no shards")?;
        let topology = |value: &serde_json::Value| -> BTreeMap<String, bool> {
            value["shards"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(name, shard)| (name.clone(), shard["settings"]["is_master"] == true))
                .collect()
        };
        ensure!(
            topology(&source) == topology(&target),
            "archive configuration must match source shard topology"
        );
        ensure!(
            conversion != Conversion::ToClient || shards.len() == 2,
            "client export requires exactly two shards: one master and one secondary"
        );
        let mut targets = BTreeMap::new();
        for (name, shard) in shards {
            ensure!(
                conversion != Conversion::None
                    || shard["settings"]["encode_user_path"]
                        == target["shards"][name]["settings"]["encode_user_path"],
                "archive configuration must preserve source player path encoding"
            );
            let target = match conversion {
                Conversion::None => (name.as_str(), shard["settings"]["encode_user_path"] == true),
                Conversion::ToClient => (
                    if shard["settings"]["is_master"] == true {
                        "Master"
                    } else {
                        "Caves"
                    },
                    true,
                ),
                Conversion::FromClient => (
                    match name.as_str() {
                        "Master" => "forest",
                        "Caves" => "cave",
                        _ => bail!("client import requires Master and optionally Caves"),
                    },
                    false,
                ),
            };
            targets.insert(name.as_str(), target);
            let save = Path::new(name).join("save");
            ensure!(
                !self.entries.contains_key(&save.join("saveindex"))
                    || self.entries.contains_key(&save.join("shardindex")),
                "legacy saveindex must be migrated by the game before transfer"
            );
        }
        let remap = |path: &Path| {
            let first = path.iter().next().unwrap().to_str().unwrap();
            targets.get(first).map_or_else(
                || path.to_owned(),
                |(name, _)| Path::new(name).join(path.iter().skip(1).collect::<PathBuf>()),
            )
        };
        let mut output = Self::default();
        for (path, mut contents) in configuration.files()? {
            if kind(&path) != Some(Kind::Configuration) {
                continue;
            }
            if path.extension().is_some_and(|extension| extension == "ini") {
                let mut ini = configuration::Ini::parse(&path, &contents, None)?.into_sections();
                sanitize_ini(&mut ini);
                if path.file_name().is_some_and(|name| name == "server.ini") {
                    let (name, encoded) = targets[path.iter().next().unwrap().to_str().unwrap()];
                    ini.entry("account".into())
                        .or_default()
                        .insert("encode_user_path".into(), encoded.to_string());
                    if conversion == Conversion::FromClient {
                        ini.entry("shard".into())
                            .or_default()
                            .insert("name".into(), name.into());
                    }
                }
                contents = render_ini(&ini)?;
            }
            output.insert(remap(&path), contents.into_bytes())?;
        }
        let mut players = BTreeMap::new();
        for (path, mut bytes) in self.entries {
            let parts: Vec<_> = path
                .iter()
                .map(|name| name.to_str().unwrap().to_owned())
                .collect();
            if parts.get(1).is_none_or(|part| part != "save") || parts[2] == "saveindex" {
                continue;
            }
            let shard = shards
                .get(&parts[0])
                .context("save files have no matching shard configuration")?;
            let encoded = shard["settings"]["encode_user_path"] == true;
            let (_, target_encoded) = targets[parts[0].as_str()];
            let mut target = remap(&path);
            if parts[2] == "shardindex" {
                bytes = sanitize_shard_index(
                    std::str::from_utf8(&bytes).context("shard index is not UTF-8")?,
                    target_encoded,
                )?;
            } else if encoded != target_encoded && parts[2] == "session" && parts.len() >= 6 {
                let mut renamed = parts.clone();
                if target_encoded && parts[4].starts_with("KU_") {
                    let userid = if parts[4].len() == 12 && parts[4].ends_with('_') {
                        &parts[4][..11]
                    } else {
                        &parts[4]
                    };
                    renamed[4] = encode_klei_id(userid)?;
                } else if !target_encoded && parts[4].starts_with("A7") {
                    // Encoded Klei IDs start with A7; offline IDs keep their native names.
                    renamed[4] = format!("{}_", decode_klei_id(&parts[4])?);
                }
                let player: PathBuf = renamed.iter().take(5).collect();
                let original: PathBuf = parts.iter().take(5).collect();
                if let Some(previous) = players.insert(player, original.clone()) {
                    ensure!(
                        previous == original,
                        "player save directories collide after conversion"
                    );
                }
                target = remap(&renamed.iter().collect::<PathBuf>());
            }
            output.insert(target, bytes)?;
        }
        Ok(output)
    }
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

fn sanitize_shard_index(source: &str, encode: bool) -> Result<Vec<u8>> {
    let mut index = lua::parse_return_table(source)?;
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
    ensure!(path.components().next().is_some(), "empty archive path");
    ensure!(path.components().count() <= 64, "archive path is too deep");
    for component in path.components() {
        let Component::Normal(name) = component else {
            bail!("archive path must be relative");
        };
        let name = name.to_str().context("archive path is not UTF-8")?;
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
