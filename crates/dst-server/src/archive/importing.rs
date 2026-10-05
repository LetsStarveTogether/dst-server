//! Decode a single room in memory, then publish its rebuilt native directory.

use std::collections::BTreeSet;
use std::fs::File;
use std::io::{Read, Seek};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};

use super::{
    client, compression_worker,
    native::{self, Kind, NativeFiles, Paths},
};
use crate::{
    files::{self, RoomReader},
    settings::ClusterConfig,
};

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct ImportOptions {
    pub from_client: bool,
}

pub enum ImportSource {
    /// A room directory, a directory containing one room, or a ZIP/7z file.
    Path(PathBuf),
    /// An already opened, seekable ZIP/7z stream, including an export in RAM.
    Archive(File),
}

/// Rebuild native files in a new directory; existing destinations are never replaced.
pub async fn import_cluster(
    source: ImportSource,
    destination: PathBuf,
    options: ImportOptions,
) -> Result<ClusterConfig> {
    compression_worker(move || {
        ensure!(
            std::fs::symlink_metadata(&destination)
                .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound),
            "import destination already exists or cannot be inspected"
        );
        let files = match source {
            ImportSource::Path(path) if std::fs::symlink_metadata(&path)?.is_dir() => {
                let root = directory_root(&path, options.from_client)?;
                NativeFiles::read_directory(&RoomReader::open(root)?)?
            }
            ImportSource::Path(path) => {
                let parent = RoomReader::open(
                    path.parent()
                        .filter(|path| !path.as_os_str().is_empty())
                        .unwrap_or(Path::new(".")),
                )?;
                read_archive(
                    parent.open_regular(Path::new(
                        path.file_name().context("archive path has no filename")?,
                    ))?,
                    options.from_client,
                )?
            }
            ImportSource::Archive(file) => read_archive(file, options.from_client)?,
        };
        let configuration = if options.from_client {
            client::configuration(&files)?
        } else {
            files.configuration()?
        };
        let mut files = files.normalize(
            configuration,
            None,
            if options.from_client {
                native::Conversion::FromClient
            } else {
                native::Conversion::None
            },
        )?;
        let mut configuration = files.configuration()?.into_value();
        configuration["settings"]["cluster_key"] = crate::settings::new_cluster_key()?.into();
        let configuration = ClusterConfig::from_value(configuration)?;
        files.entries.extend(
            configuration
                .files()?
                .into_iter()
                .map(|(path, text)| (path, text.into_bytes())),
        );
        files::write_new_directory(&destination, &files.entries)?;
        Ok(configuration)
    })
    .await
}

fn directory_root(path: &Path, client: bool) -> Result<PathBuf> {
    let reader = RoomReader::open(path)?;
    let is_root = |reader: &RoomReader| -> Result<bool> {
        let marker = if client {
            "Master/save/shardindex"
        } else {
            "cluster.ini"
        };
        Ok(reader.read_optional_bytes(marker)?.is_some())
    };
    let mut roots = Vec::new();
    if is_root(&reader)? {
        roots.push(path.to_owned());
    }
    for (name, kind) in reader.list_directory("")? {
        if kind.is_dir() && !name.starts_with('.') && name != "__MACOSX" {
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

struct Entry {
    path: PathBuf,
    directory: bool,
    size: u64,
}

impl Entry {
    fn new(name: &str, directory: bool, size: u64, mode: Option<u32>) -> Result<Self> {
        ensure!(
            !directory || size == 0,
            "archive directories cannot contain file data"
        );
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
        native::validate_path(&path)?;
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
        Ok(Self {
            path,
            directory,
            size,
        })
    }
}

fn archive_root(entries: &[Entry], client: bool) -> Result<PathBuf> {
    ensure!(
        entries.len() <= native::MAX_ENTRIES,
        "archive has too many entries"
    );
    let mut paths = Paths::default();
    let mut roots = BTreeSet::new();
    let mut size = 0_u64;
    let marker = if client {
        Path::new("Master/save/shardindex")
    } else {
        Path::new("cluster.ini")
    };
    for entry in entries {
        paths.insert(&entry.path, entry.directory)?;
        size = size
            .checked_add(entry.size)
            .context("archive size overflow")?;
        ensure!(
            size <= native::MAX_BYTES,
            "archive exceeds the unpacked byte limit"
        );
        if !entry.directory && entry.path.ends_with(marker) {
            let depth = entry.path.components().count() - marker.components().count();
            if depth <= 1 {
                roots.insert(entry.path.iter().take(depth).collect::<PathBuf>());
            }
        }
    }
    ensure!(
        roots.len() == 1,
        "import requires exactly one room at the root or one directory below it"
    );
    Ok(roots.pop_first().unwrap())
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

fn read_archive(mut file: File, client: bool) -> Result<NativeFiles> {
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
    let mut files = NativeFiles::default();
    if magic == *b"7z\xbc\xaf\x27\x1c" {
        let mut reader = sevenz_rust2::ArchiveReader::new(file, sevenz_rust2::Password::empty())?;
        reader.set_thread_count(1);
        let entries = reader
            .archive()
            .files
            .iter()
            .map(|entry| {
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
                )
            })
            .collect::<Result<Vec<_>>>()?;
        let root = archive_root(&entries, client)?;
        reader.for_each_entries(|entry, bytes| {
            let result = (|| -> Result<()> {
                let entry = Entry::new(&entry.name, entry.is_directory, entry.size, None)?;
                if let Some(path) = selected(&entry, &root) {
                    read_file(&mut files, path, entry.size, bytes)?;
                } else {
                    // Solid archives must consume skipped streams before the next entry.
                    ensure!(
                        std::io::copy(bytes, &mut std::io::sink())? == entry.size,
                        "archive entry length is inconsistent"
                    );
                }
                Ok(())
            })();
            result
                .map(|()| true)
                .map_err(|error| sevenz_rust2::Error::Other(format!("{error:#}").into()))
        })?;
    } else if magic.starts_with(b"PK") {
        let mut reader = super::zip::open(file, native::MAX_BYTES, native::MAX_ENTRIES)?;
        let entries = (0..reader.len())
            .map(|index| {
                let entry = reader.by_index(index)?;
                Entry::new(
                    entry.name(),
                    entry.is_dir(),
                    entry.size(),
                    entry.unix_mode(),
                )
            })
            .collect::<Result<Vec<_>>>()?;
        let root = archive_root(&entries, client)?;
        for (index, entry) in entries.iter().enumerate() {
            if let Some(path) = selected(entry, &root) {
                read_file(&mut files, path, entry.size, &mut reader.by_index(index)?)?;
            }
        }
    } else {
        bail!("import supports native directories, ZIP and 7z archives");
    }
    Ok(files)
}

fn read_file(files: &mut NativeFiles, path: &Path, size: u64, reader: &mut dyn Read) -> Result<()> {
    files.read(path, size, reader)?;
    ensure!(
        files.entries[path].len() as u64 == size,
        "archive entry length is inconsistent"
    );
    Ok(())
}
