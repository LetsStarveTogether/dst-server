//! Consistent, shareable native archives and cancellable multipart uploads.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{Read, Seek};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail, ensure};
use object_store::aws::{AmazonS3Builder, AmazonS3ConfigKey};
use object_store::{Attribute, Attributes, ObjectStore, PutMultipartOptions};
use serde::{Deserialize, Serialize};
use sevenz_rust2::encoder_options::ZstandardOptions;
use sevenz_rust2::{ArchiveEntry, ArchiveWriter};
use tempfile::TempDir;
use tokio::io::AsyncReadExt;
use tokio::sync::{Semaphore, oneshot};

use crate::files::{IniDocument, StoppedRoom, render_ini};
use crate::settings::ClusterConfig;
use crate::{configuration, lua};

pub const DEFAULT_COMPRESSION_LEVEL: u32 = 3;
pub const ARTIFACT_LEASE: Duration = Duration::from_secs(30 * 60);
pub const MAX_ARTIFACTS: usize = 8;
const PART_BYTES: usize = 8 * 1024 * 1024;
static COMPRESSION: LazyLock<Arc<Semaphore>> = LazyLock::new(|| Arc::new(Semaphore::new(1)));

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct ExportOptions {
    pub room_id: Option<String>,
    pub encode_user_path: bool,
    pub configuration: Option<ClusterConfig>,
}

impl Default for ExportOptions {
    fn default() -> Self {
        Self {
            room_id: None,
            encode_user_path: true,
            configuration: None,
        }
    }
}

/// A complete copy made while the caller held exclusive ownership of a stopped room.
pub struct PreparedArchive {
    staging: TempDir,
    filename: String,
    room_id: String,
    entries: BTreeSet<PathBuf>,
    folded_paths: BTreeSet<String>,
}

pub struct ClusterArchive {
    pub filename: String,
    file: File,
}

impl ClusterArchive {
    pub fn from_file(filename: String, mut file: File) -> Result<Self> {
        ensure!(
            Path::new(&filename).components().count() == 1,
            "archive filename must be a single path component"
        );
        validate_path(Path::new(&filename))?;
        ensure!(
            file.metadata()?.is_file(),
            "archive stream must be a regular file"
        );
        file.rewind()?;
        Ok(Self { filename, file })
    }
    /// Reopen an owned anonymous file with an independent seek position.
    pub fn try_clone(&self) -> Result<Self> {
        Ok(Self {
            filename: self.filename.clone(),
            file: File::open(format!("/proc/self/fd/{}", self.file.as_raw_fd()))
                .context("reopen archive")?,
        })
    }

    pub fn save(&self, path: impl AsRef<Path>) -> Result<()> {
        let mut source = self.try_clone()?.file;
        let mut destination = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path.as_ref())
            .context("create archive output (must not already exist)")?;
        if let Err(error) =
            std::io::copy(&mut source, &mut destination).and_then(|_| destination.sync_all())
        {
            let cleanup = fs::remove_file(path.as_ref());
            if let Err(cleanup) = cleanup {
                return Err(error).context(format!("archive output cleanup failed: {cleanup}"));
            }
            return Err(error).context("save archive output");
        }
        Ok(())
    }
    pub fn file_mut(&mut self) -> &mut File {
        &mut self.file
    }

    pub fn into_file(self) -> File {
        self.file
    }

    /// The background task owns the file until upload or cleanup has finished.
    pub fn start_upload(
        self,
        store: Arc<dyn ObjectStore>,
        object_prefix: &str,
        url_prefix: Option<&str>,
    ) -> Result<UploadTask> {
        ensure!(
            !object_prefix.starts_with('/'),
            "object prefix cannot start with a slash"
        );
        let key = format!("{object_prefix}{}", self.filename);
        let location =
            object_store::path::Path::parse(&key).context("invalid archive object key")?;
        let url =
            url_prefix.map(|prefix| format!("{prefix}{}", key.rsplit('/').next().unwrap_or(&key)));
        let result = UploadResult { key, url };
        let (cancel, cancelled) = oneshot::channel();
        let (send, receive) = oneshot::channel();
        tokio::spawn(async move {
            let outcome = upload(self, store, location, cancelled)
                .await
                .map(|()| result);
            if let Err(Err(error)) = send.send(outcome) {
                eprintln!("archive upload cleanup: {error:#}");
            }
        });
        Ok(UploadTask {
            cancel: Some(cancel),
            receive,
        })
    }
}

/// A same-host transfer lease. The RPC only carries this receipt; archive bytes
/// remain in a generated, private controller file for at most thirty minutes.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub artifact_id: String,
    pub filename: String,
    pub size: u64,
    pub expires_at_ns: u64,
}

pub async fn publish_artifact(directory: &Path, mut archive: ClusterArchive) -> Result<Artifact> {
    let directory = directory.to_owned();
    tokio::task::spawn_blocking(move || {
        clear_artifacts(&directory, false)?;
        ensure!(
            crate::files::artifact_ids(&directory)?.len() < MAX_ARTIFACTS,
            "archive transfer lease limit reached; release an earlier artifact"
        );
        let id = ulid::Ulid::new().to_string();
        let mut destination = crate::files::artifact_file(&directory, &id, true)?;
        let write = (|| {
            archive.file.rewind()?;
            let size = std::io::copy(&mut archive.file, &mut destination)?;
            destination.sync_all()?;
            Ok::<_, std::io::Error>(size)
        })();
        let size = match write {
            Ok(size) => size,
            Err(error) => {
                if let Err(cleanup) = crate::files::remove_artifact_file(&directory, &id) {
                    return Err(error)
                        .context(format!("archive artifact cleanup failed: {cleanup}"));
                }
                return Err(error).context("publish archive artifact");
            }
        };
        let expires = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .saturating_add(ARTIFACT_LEASE);
        let artifact = Artifact {
            artifact_id: id,
            filename: archive.filename,
            size,
            expires_at_ns: expires.as_nanos().min(u64::MAX as u128) as u64,
        };
        let id = artifact.artifact_id.clone();
        tokio::spawn(async move {
            tokio::time::sleep(ARTIFACT_LEASE).await;
            if let Err(error) = release_artifact(&directory, &id) {
                eprintln!("archive lease cleanup failed: {error:#}");
            }
        });
        Ok::<_, anyhow::Error>(artifact)
    })
    .await
    .context("archive publication worker stopped")?
}

pub fn release_artifact(directory: &Path, artifact_id: &str) -> Result<bool> {
    crate::files::remove_artifact_file(directory, artifact_id)
}

/// Startup clears only positively identified controller artifacts. Other files
/// are left alone; a matching symlink or special file causes an explicit error.
pub fn clear_artifacts(directory: &Path, all: bool) -> Result<usize> {
    let mut removed = 0;
    for id in crate::files::artifact_ids(directory)? {
        let expired = if all {
            true
        } else {
            let metadata = crate::files::artifact_file(directory, &id, false)?.metadata()?;
            metadata
                .modified()?
                .elapsed()
                .is_ok_and(|elapsed| elapsed >= ARTIFACT_LEASE)
        };
        if expired {
            removed += usize::from(release_artifact(directory, &id)?);
        }
    }
    Ok(removed)
}

pub fn read_artifact(directory: &Path, receipt: &Artifact) -> Result<ClusterArchive> {
    crate::model::validate_artifact_id(&receipt.artifact_id)?;
    ensure!(
        Path::new(&receipt.filename).components().count() == 1
            && receipt.filename.starts_with("DST-")
            && receipt.filename.ends_with(".7z"),
        "invalid archive filename in transfer receipt"
    );
    validate_path(Path::new(&receipt.filename))?;
    let mut source = crate::files::artifact_file(directory, &receipt.artifact_id, false)?;
    ensure!(
        source.metadata()?.len() == receipt.size,
        "archive artifact size changed"
    );
    let mut file = tempfile::tempfile().context("create local archive file")?;
    let copied = std::io::copy(&mut source, &mut file).context("copy archive artifact")?;
    ensure!(
        copied == receipt.size,
        "archive artifact changed during transfer"
    );
    file.rewind()?;
    Ok(ClusterArchive {
        filename: receipt.filename.clone(),
        file,
    })
}

/// Export from an online Agent's stopped room, or verify an inactive service and
/// acquire its native RoomLock before reading an offline room.
pub async fn export_host(
    host: &crate::host::Host,
    number: u16,
    options: ExportOptions,
    level: u32,
) -> Result<ClusterArchive> {
    ensure!(
        (1..=22).contains(&level),
        "Zstandard compression level must be between 1 and 22"
    );
    let directory = host.rooms.path(number)?;
    if host
        .systemd
        .status(&crate::host::Host::unit(number))
        .await?
        .running()
    {
        let client = host.connect(number).await?;
        // Once the Agent accepts an export, this worker still copies/releases its
        // artifact if the Python or CLI caller stops waiting.
        return tokio::spawn(async move {
            let outcome = async {
                let request = crate::model::Request::ExportArchive {
                    options: serde_json::to_value(options)?,
                    compression_level: u64::from(level),
                };
                let value = client
                    .call(crate::model::Envelope::new(
                        crate::model::Target::Room,
                        request,
                    )?)
                    .await?;
                let receipt: Artifact =
                    serde_json::from_value(value).context("invalid archive transfer receipt")?;
                let copy_receipt = receipt.clone();
                let copied =
                    tokio::task::spawn_blocking(move || read_artifact(&directory, &copy_receipt))
                        .await
                        .context("archive transfer worker stopped")
                        .and_then(|result| result);
                let release = client
                    .call(crate::model::Envelope::new(
                        crate::model::Target::Room,
                        crate::model::Request::ReleaseArchive {
                            artifact_id: receipt.artifact_id,
                        },
                    )?)
                    .await;
                match (copied, release) {
                    (Ok(archive), Ok(_)) => Ok(archive),
                    (Err(error), Ok(_)) => Err(error),
                    (Ok(_), Err(error)) => {
                        Err(error).context("archive transfer succeeded but artifact cleanup failed")
                    }
                    (Err(error), Err(cleanup)) => Err(error)
                        .context(format!("archive artifact cleanup also failed: {cleanup}")),
                }
            }
            .await;
            let close = client.close().await;
            match (outcome, close) {
                (Ok(archive), Ok(())) => Ok(archive),
                (Err(error), _) => Err(error),
                (Ok(_), Err(error)) => Err(error).context("close archive Agent connection"),
            }
        })
        .await
        .context("archive export worker stopped")?;
    }
    let mut lock = crate::files::RoomLock::try_acquire(&directory)?;
    ensure!(
        !host
            .systemd
            .status(&crate::host::Host::unit(number))
            .await?
            .running(),
        "offline export requires an inactive room service"
    );
    let prepared = tokio::task::spawn_blocking(move || {
        let mut stopped = lock.while_stopped();
        stopped.recover()?;
        prepare(&mut stopped, &options)
    })
    .await
    .context("archive preparation worker stopped")??;
    prepared.compress(level).await
}

/// Dropping this task requests cancellation; its worker still owns multipart cleanup.
pub struct UploadTask {
    cancel: Option<oneshot::Sender<()>>,
    receive: oneshot::Receiver<Result<UploadResult>>,
}

impl UploadTask {
    pub async fn wait(mut self) -> Result<UploadResult> {
        self.wait_mut().await
    }

    pub async fn wait_mut(&mut self) -> Result<UploadResult> {
        (&mut self.receive)
            .await
            .context("archive upload worker stopped")?
    }

    pub async fn wait_until_cancelled(
        mut self,
        cancelled: impl std::future::Future<Output = ()>,
    ) -> Result<UploadResult> {
        tokio::select! {
            result = self.wait_mut() => result,
            _ = cancelled => self.cancel().await,
        }
    }

    pub async fn cancel(mut self) -> Result<UploadResult> {
        if let Some(cancel) = self.cancel.take() {
            let _ = cancel.send(());
        }
        (&mut self.receive)
            .await
            .context("archive upload worker stopped during cleanup")?
    }
}

impl Drop for UploadTask {
    fn drop(&mut self) {
        if let Some(cancel) = self.cancel.take() {
            let _ = cancel.send(());
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct UploadResult {
    pub key: String,
    pub url: Option<String>,
}

/// Deliberately does not implement Debug: these options may contain credentials.
#[derive(Default)]
pub struct S3Options {
    pub bucket: Option<String>,
    pub endpoint: Option<String>,
    pub region: Option<String>,
    pub access_key_id: Option<String>,
    pub secret_access_key: Option<String>,
    pub session_token: Option<String>,
}

impl S3Options {
    /// Explicit settings override environment settings, including the S3-specific endpoint.
    pub fn build(self) -> Result<Arc<dyn ObjectStore>> {
        let mut builder = AmazonS3Builder::from_env();
        if let Some(region) = self.region {
            builder = builder.with_region(region);
        } else if builder
            .get_config_value(&AmazonS3ConfigKey::Region)
            .is_none()
        {
            builder = builder.with_region("auto");
        }
        if let Some(value) = self.bucket {
            builder = builder.with_bucket_name(value);
        }
        if let Some(value) = self.endpoint {
            builder = builder.with_config(AmazonS3ConfigKey::S3Endpoint, value);
        }
        if let Some(value) = self.access_key_id {
            builder = builder.with_access_key_id(value);
        }
        if let Some(value) = self.secret_access_key {
            builder = builder.with_secret_access_key(value);
        }
        if let Some(value) = self.session_token {
            builder = builder.with_token(value);
        }
        Ok(Arc::new(
            builder
                .build()
                .context("configure archive object storage")?,
        ))
    }
}

async fn upload(
    archive: ClusterArchive,
    store: Arc<dyn ObjectStore>,
    location: object_store::path::Path,
    mut cancelled: oneshot::Receiver<()>,
) -> Result<()> {
    let mut file = tokio::fs::File::from_std(archive.file);
    use tokio::io::AsyncSeekExt;
    file.rewind().await.context("rewind archive for upload")?;
    let mut attributes = Attributes::new();
    attributes.insert(Attribute::ContentType, "application/x-7z-compressed".into());
    // Do not drop an in-flight create/part request: its eventual upload ID/part must
    // remain owned until we can explicitly abort it.
    let mut multipart = store
        .put_multipart_opts(
            &location,
            PutMultipartOptions {
                attributes,
                ..Default::default()
            },
        )
        .await
        .context("create archive multipart upload")?;
    let outcome = async {
        loop {
            ensure!(
                matches!(
                    cancelled.try_recv(),
                    Err(oneshot::error::TryRecvError::Empty)
                ),
                "archive upload cancelled"
            );
            let mut bytes = vec![0; PART_BYTES];
            let mut length = 0;
            while length < bytes.len() {
                let read = file
                    .read(&mut bytes[length..])
                    .await
                    .context("read archive upload part")?;
                if read == 0 {
                    break;
                }
                length += read;
            }
            if length == 0 {
                break;
            }
            bytes.truncate(length);
            multipart
                .put_part(bytes.into())
                .await
                .context("upload archive part")?;
        }
        ensure!(
            matches!(
                cancelled.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ),
            "archive upload cancelled"
        );
        multipart
            .complete()
            .await
            .context("complete archive upload")?;
        Ok(())
    }
    .await;
    if let Err(error) = outcome {
        if let Err(cleanup) = multipart.abort().await {
            return Err(error).context(format!("multipart abort failed: {cleanup}"));
        }
        return Err(error);
    }
    Ok(())
}

/// Native files are read only during this synchronous call. The caller can resume
/// the room once it returns, even if compression or upload is later cancelled.
pub fn prepare(room: &mut StoppedRoom<'_>, options: &ExportOptions) -> Result<PreparedArchive> {
    let root = room.directory();
    let room_id = options
        .room_id
        .as_deref()
        .or_else(|| root.file_name()?.to_str())
        .context("archive room ID is required")?;
    ensure!(
        !room_id.is_empty()
            && room_id.len() <= 80
            && room_id.as_bytes()[0].is_ascii_alphanumeric()
            && room_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')),
        "archive room ID must contain 1-80 ASCII letters, digits, underscores or hyphens"
    );
    let topology = configuration::discover(root)?;
    let configuration = match &options.configuration {
        Some(configuration) => configuration.clone(),
        None => ClusterConfig::load_stopped(room)?,
    };
    let resolved = configuration.resolved();
    let expected: BTreeMap<_, _> = resolved["shards"]
        .as_object()
        .context("archive configuration has no shards")?
        .iter()
        .map(|(name, shard)| (name.as_str(), shard["settings"]["is_master"] == true))
        .collect();
    let actual: BTreeMap<_, _> = topology
        .shards
        .iter()
        .map(|shard| (shard.name.as_str(), shard.master))
        .collect();
    ensure!(
        expected == actual,
        "archive configuration must match source shard topology"
    );
    let mut prepared = PreparedArchive {
        staging: tempfile::tempdir().context("create archive staging directory")?,
        filename: format!(
            "DST-{room_id}-{}.7z",
            chrono::Utc::now().format("%Y%m%dT%H%M%SZ")
        ),
        room_id: room_id.to_owned(),
        entries: BTreeSet::new(),
        folded_paths: BTreeSet::new(),
    };
    for (path, mut contents) in configuration.files()? {
        if [
            "cluster_token.txt",
            "adminlist.txt",
            "whitelist.txt",
            "blocklist.txt",
        ]
        .iter()
        .any(|name| path == Path::new(name))
        {
            continue;
        }
        if path.extension().is_some_and(|extension| extension == "ini") {
            let mut ini = configuration::Ini::parse(&path, &contents, None)?.into_sections();
            sanitize_ini(&mut ini);
            if options.encode_user_path && path.file_name().is_some_and(|name| name == "server.ini")
            {
                ini.entry("account".into())
                    .or_default()
                    .insert("encode_user_path".into(), "true".into());
            }
            contents = render_ini(&ini)?;
        }
        prepared.write(&path, contents.as_bytes())?;
    }
    for shard in &topology.shards {
        let base = Path::new(&shard.name);
        let ini = room.read_ini(base.join("server.ini"))?;
        let encoded = match ini
            .get("account")
            .and_then(|section| section.get("encode_user_path"))
            .map(String::as_str)
        {
            Some("true") | None => true,
            Some("false") => false,
            _ => bail!("invalid native player path encoding"),
        };
        ensure!(
            options.encode_user_path
                || resolved["shards"][&shard.name]["settings"]["encode_user_path"] == encoded,
            "archive configuration must preserve source player path encoding"
        );
        let save = base.join("save");
        let entries = match room.list_directory(&save) {
            Ok(entries) => entries,
            Err(error) if missing(&error) => continue,
            Err(error) => return Err(error),
        };
        let names: BTreeSet<_> = entries.iter().map(|(name, _)| name.as_str()).collect();
        ensure!(
            !names.contains("saveindex") || names.contains("shardindex"),
            "legacy saveindex must be migrated by the game before export"
        );
        for filename in ["shardindex", "recipebook", "reforged_achievements_server"] {
            if names.contains(filename) {
                let path = save.join(filename);
                if filename == "shardindex" {
                    let mut bytes = Vec::new();
                    room.open_regular(&path)?
                        .take(crate::files::MAX_FILE_BYTES as u64 + 1)
                        .read_to_end(&mut bytes)?;
                    ensure!(
                        bytes.len() <= crate::files::MAX_FILE_BYTES,
                        "shard index exceeds byte limit"
                    );
                    prepared.write(
                        &path,
                        &sanitize_shard_index(&bytes, options.encode_user_path)?,
                    )?;
                } else {
                    prepared.copy(room, &path, &path)?;
                }
            }
        }
        if names.contains("session") {
            let session = save.join("session");
            let mut players = BTreeMap::new();
            prepared.copy_sessions(
                room,
                &session,
                &session,
                options.encode_user_path && !encoded,
                &mut players,
            )?;
        }
        if names.contains("mod_config_data") {
            let parent = save.join("mod_config_data");
            for (name, _) in room.list_directory(&parent)? {
                if name.starts_with("mod_worldjump_data_") {
                    let path = parent.join(name);
                    prepared.copy(room, &path, &path)?;
                }
            }
        }
    }
    Ok(prepared)
}

impl PreparedArchive {
    pub async fn compress(self, level: u32) -> Result<ClusterArchive> {
        ensure!(
            (1..=22).contains(&level),
            "Zstandard compression level must be between 1 and 22"
        );
        compression_worker(move || {
            let mut writer =
                ArchiveWriter::new(tempfile::tempfile().context("create archive temporary file")?)?;
            writer.set_content_methods(vec![ZstandardOptions::from_level(level).into()]);
            for path in &self.entries {
                let name = format!(
                    "{}/{}",
                    self.room_id,
                    path.to_str().context("archive path is not UTF-8")?
                );
                let source = File::open(self.staging.path().join(path))
                    .context("read staged archive file")?;
                writer
                    .push_archive_entry(ArchiveEntry::new_file(&name), Some(source))
                    .context("compress archive file")?;
            }
            let mut file = writer.finish().context("finish native archive")?;
            file.rewind().context("rewind native archive")?;
            Ok(ClusterArchive {
                filename: self.filename,
                file,
            })
        })
        .await
    }

    fn write(&mut self, path: &Path, bytes: &[u8]) -> Result<()> {
        let target = self.reserve(path)?;
        fs::write(target, bytes).context("write staged archive configuration")
    }

    fn reserve(&mut self, path: &Path) -> Result<PathBuf> {
        validate_path(path)?;
        ensure!(
            self.folded_paths.insert(
                path.to_str()
                    .context("archive path is not UTF-8")?
                    .to_lowercase()
            ),
            "archive paths collide"
        );
        ensure!(
            self.entries.insert(path.to_owned()),
            "archive paths collide"
        );
        let target = self.staging.path().join(path);
        fs::create_dir_all(target.parent().context("archive entry has no parent")?)?;
        Ok(target)
    }

    fn copy(&mut self, room: &StoppedRoom<'_>, source: &Path, target: &Path) -> Result<()> {
        let mut source = room.open_regular(source)?;
        let destination = self.reserve(target)?;
        std::io::copy(&mut source, &mut File::create(destination)?)
            .context("copy native save into archive staging")?;
        Ok(())
    }

    fn copy_sessions(
        &mut self,
        room: &StoppedRoom<'_>,
        root: &Path,
        parent: &Path,
        encode: bool,
        players: &mut BTreeMap<PathBuf, PathBuf>,
    ) -> Result<()> {
        for (name, kind) in room.list_directory(parent)? {
            if sdk_entry(&name) {
                continue;
            }
            let source = parent.join(&name);
            validate_path(&source)?;
            ensure!(
                !kind.is_symlink() && (kind.is_dir() || kind.is_file()),
                "save entry must be a regular file or directory"
            );
            if kind.is_dir() {
                self.copy_sessions(room, root, &source, encode, players)?;
            } else {
                let mut parts: Vec<_> = source
                    .strip_prefix(root)?
                    .iter()
                    .map(|part| {
                        part.to_str()
                            .context("native save path is not UTF-8")
                            .map(str::to_owned)
                    })
                    .collect::<Result<_>>()?;
                if encode && parts.len() >= 3 {
                    if parts[1].starts_with("KU_") {
                        let userid = if parts[1].len() == 12 && parts[1].ends_with('_') {
                            &parts[1][..11]
                        } else {
                            &parts[1]
                        };
                        parts[1] = encode_klei_id(userid)?;
                    }
                    let player = root.join(&parts[0]).join(&parts[1]);
                    let original = root.join(
                        source
                            .strip_prefix(root)?
                            .iter()
                            .take(2)
                            .collect::<PathBuf>(),
                    );
                    if let Some(previous) = players.insert(player, original.clone()) {
                        ensure!(
                            previous == original,
                            "player save directories collide after encoding"
                        );
                    }
                }
                let target = root.join(parts.iter().collect::<PathBuf>());
                self.copy(room, &source, &target)?;
            }
        }
        Ok(())
    }
}

async fn compression_worker<T: Send + 'static>(
    operation: impl FnOnce() -> Result<T> + Send + 'static,
) -> Result<T> {
    let permit = COMPRESSION
        .clone()
        .acquire_owned()
        .await
        .context("archive compressor unavailable")?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        operation()
    })
    .await
    .context("archive compression worker stopped")?
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

fn sanitize_shard_index(source: &[u8], encode: bool) -> Result<Vec<u8>> {
    let mut index = lua::parse_return_table(
        std::str::from_utf8(source).context("shard index must contain valid UTF-8")?,
    )?;
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
    if encode {
        server.insert("encode_user_path".into(), true.into());
    }
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

fn missing(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<std::io::Error>()
            .is_some_and(|io| io.kind() == std::io::ErrorKind::NotFound)
    })
}

fn validate_path(path: &Path) -> Result<()> {
    ensure!(path.components().next().is_some(), "empty archive path");
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

pub fn encode_klei_id(userid: &str) -> Result<String> {
    const ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_";
    const PATH_ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUV";
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files::RoomLock;
    use std::io::Write;
    use std::time::Duration;

    fn payload_archive() -> ClusterArchive {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"archive bytes\0\xff").unwrap();
        ClusterArchive::from_file("DST-001-fixture.7z".into(), file).unwrap()
    }

    #[tokio::test]
    async fn generated_artifact_leases_are_bounded_and_copy_before_release() {
        let directory = tempfile::tempdir().unwrap();
        let receipt = publish_artifact(directory.path(), payload_archive())
            .await
            .unwrap();
        assert_eq!(receipt.size, 15);
        let mut received = read_artifact(directory.path(), &receipt).unwrap();
        assert!(release_artifact(directory.path(), &receipt.artifact_id).unwrap());
        assert!(!release_artifact(directory.path(), &receipt.artifact_id).unwrap());
        let mut bytes = Vec::new();
        received.file_mut().read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes, b"archive bytes\0\xff");
        for _ in 0..MAX_ARTIFACTS {
            publish_artifact(directory.path(), payload_archive())
                .await
                .unwrap();
        }
        assert!(
            publish_artifact(directory.path(), payload_archive())
                .await
                .unwrap_err()
                .to_string()
                .contains("lease limit")
        );
        fs::write(directory.path().join(".dst-archives/keep.txt"), "unowned").unwrap();
        assert_eq!(clear_artifacts(directory.path(), false).unwrap(), 0);
        assert_eq!(
            clear_artifacts(directory.path(), true).unwrap(),
            MAX_ARTIFACTS
        );
        assert_eq!(
            fs::read_to_string(directory.path().join(".dst-archives/keep.txt")).unwrap(),
            "unowned"
        );
        assert!(release_artifact(directory.path(), "../keep").is_err());
        let output = directory.path().join("saved.7z");
        received.save(&output).unwrap();
        assert_eq!(fs::read(&output).unwrap(), bytes);
        assert!(received.save(&output).is_err());
    }

    #[tokio::test]
    async fn artifact_reads_and_cleanup_refuse_symlinks_and_changed_receipts() {
        use std::os::unix::fs::symlink;
        let directory = tempfile::tempdir().unwrap();
        let receipt = publish_artifact(directory.path(), payload_archive())
            .await
            .unwrap();
        let mut changed = receipt.clone();
        changed.size += 1;
        assert!(read_artifact(directory.path(), &changed).is_err());
        changed = receipt.clone();
        changed.filename = "../escape.7z".into();
        assert!(read_artifact(directory.path(), &changed).is_err());
        let file = directory
            .path()
            .join(format!(".dst-archives/{}.7z", receipt.artifact_id));
        fs::remove_file(&file).unwrap();
        let outside = directory.path().join("outside");
        fs::write(&outside, "private token").unwrap();
        symlink(&outside, &file).unwrap();
        assert!(read_artifact(directory.path(), &receipt).is_err());
        assert!(release_artifact(directory.path(), &receipt.artifact_id).is_err());
        assert!(clear_artifacts(directory.path(), true).is_err());
        assert_eq!(fs::read_to_string(&outside).unwrap(), "private token");
        fs::remove_file(file).unwrap();
        fs::remove_dir(directory.path().join(".dst-archives")).unwrap();
        let other = tempfile::tempdir().unwrap();
        symlink(other.path(), directory.path().join(".dst-archives")).unwrap();
        assert!(
            publish_artifact(directory.path(), payload_archive())
                .await
                .is_err()
        );
    }
    use tokio::io::AsyncWriteExt;

    #[tokio::test]
    async fn online_export_copies_large_archive_and_releases_after_caller_cancellation() {
        use crate::driver::DriverOptions;
        use crate::host::{AGENT_SOCKET, Host};
        use crate::model::Request;
        use crate::room::Room;
        use crate::rpc::{self, EventHub, Incoming};
        use std::os::unix::fs::PermissionsExt;
        use std::sync::atomic::{AtomicUsize, Ordering};
        use tokio::sync::{Notify, mpsc};

        tokio::task::LocalSet::new()
            .run_until(async {
                let temporary = tempfile::tempdir().unwrap();
                let source = cluster();
                let root = temporary.path().join("rooms");
                let directory = root.join("001");
                fs::create_dir_all(&root).unwrap();
                fs::rename(source.path(), &directory).unwrap();
                let mut random = 17_u32;
                let bytes = (0..2 * 1024 * 1024)
                    .map(|_| {
                        random ^= random << 13;
                        random ^= random >> 17;
                        random ^= random << 5;
                        random as u8
                    })
                    .collect::<Vec<_>>();
                fs::write(
                    directory.join("forest/save/session/0123456789ABCDEF/000000001"),
                    bytes,
                )
                .unwrap();
                let events = EventHub::default();
                let room = Room::open(
                    &directory,
                    "/bin/false",
                    DriverOptions::default(),
                    events.clone(),
                )
                .unwrap();
                let script = temporary.path().join("systemctl");
                fs::write(&script, "#!/bin/sh\nprintf 'Id=dst-001.service\\nLoadState=loaded\\nActiveState=active\\nSubState=running\\nJob=0\\nResult=success\\n'\n").unwrap();
                fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
                let mut host = Host::new(root, temporary.path().join("units"));
                host.systemd.executable = script;
                let host = Arc::new(host);
                let socket = directory.join(AGENT_SOCKET);
                let (dispatch, mut incoming) = mpsc::channel::<Incoming>(16);
                let (shutdown, stop) = oneshot::channel();
                let serving = socket.clone();
                let server = tokio::task::spawn_local(async move {
                    rpc::serve(&serving, dispatch, events, stop).await
                });
                let accepted = Arc::new(Notify::new());
                let released = Arc::new(AtomicUsize::new(0));
                let actor_accepted = accepted.clone();
                let actor_released = released.clone();
                let actor = tokio::spawn(async move {
                    while let Some(message) = incoming.recv().await {
                        if matches!(message.request.request, Request::ExportArchive { .. }) {
                            actor_accepted.notify_one();
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                        let release = matches!(message.request.request, Request::ReleaseArchive { .. });
                        let result = room.invoke(message.request).await;
                        if release && result.is_ok() {
                            actor_released.fetch_add(1, Ordering::SeqCst);
                        }
                        let _ = message.reply.send(result);
                    }
                });
                tokio::time::timeout(Duration::from_secs(2), async {
                    while !socket.exists() {
                        tokio::time::sleep(Duration::from_millis(1)).await;
                    }
                }).await.unwrap();
                let archive = export_host(&host, 1, options(), 3).await.unwrap();
                assert!(archive.file.metadata().unwrap().len() > 1024 * 1024);
                assert!(crate::files::artifact_ids(&directory).unwrap().is_empty());
                assert_eq!(released.load(Ordering::SeqCst), 1);
                accepted.notified().await;
                let exporting = host.clone();
                let caller = tokio::spawn(async move {
                    export_host(&exporting, 1, options(), 3).await
                });
                accepted.notified().await;
                caller.abort();
                assert!(caller.await.is_err_and(|error| error.is_cancelled()));
                tokio::time::timeout(Duration::from_secs(10), async {
                    while released.load(Ordering::SeqCst) < 2 {
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                }).await.unwrap();
                assert!(crate::files::artifact_ids(&directory).unwrap().is_empty());
                shutdown.send(()).unwrap();
                server.await.unwrap().unwrap();
                actor.await.unwrap();
            })
            .await;
    }

    fn cluster() -> TempDir {
        let root = tempfile::tempdir().unwrap();
        for (name, bytes) in [
            ("cluster.ini", b"[NETWORK]\ncluster_name = Shared\ncluster_password = private-password\n[SHARD]\ncluster_key = private-key\n[STEAM]\nsteam_group_id = 123\nsteam_group_only = true\n".as_slice()),
            ("cluster_token.txt", b"private-token"),
            ("adminlist.txt", b"private-admin"),
            ("mods/modsettings.lua", b"ForceEnableMod(\"workshop-123\")\n"),
            ("mods/dedicated_server_mods_setup.lua", b"ServerModSetup(\"123\")\n"),
            ("forest/server.ini", b"[SHARD]\nis_master = true\ncluster_key = private-key\n[ACCOUNT]\nencode_user_path = false\n"),
            ("forest/worldgenoverride.lua", b"return {override_enabled=true,preset=\"SURVIVAL_TOGETHER\"}"),
            ("forest/modoverrides.lua", b"return {}"),
            ("forest/save/shardindex", b"KLEI     1 return {session_id=\"0123456789ABCDEF\",world={password=\"world-progress\"},server={password=\"private-index-password\",cluster_key=\"private-key\",clan={id=\"123\"},privacy_type=3,encode_user_path=false}}"),
            ("forest/save/session/0123456789ABCDEF/000000001", b"world\0\xff"),
            ("forest/save/session/0123456789ABCDEF/000000001.meta", b"world-meta\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/000000001", b"player\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/savelocation", b"\x81\0\0\0\x01\0\0\0\x02"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/.dst-control.json", b"private-control"),
            ("forest/save/session/0123456789ABCDEF/.dst-maintenance/private", b"private-ignored"),
            ("forest/save/session/0123456789ABCDEF/.world_metadata", b"hidden-world-progress"),
            ("forest/save/recipebook", b"recipe-progress\0\xff"),
            ("forest/save/reforged_achievements_server", b"achievement-progress\0\xff"),
            ("forest/save/mod_config_data/mod_worldjump_data_1", b"worldjump-progress\0\xff"),
            ("forest/save/mod_config_data/cache", b"private-cache"),
            ("forest/save/server_temp/data", b"private-temporary"),
        ] {
            let path = root.path().join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
        root
    }

    fn options() -> ExportOptions {
        ExportOptions {
            room_id: Some("001".into()),
            ..Default::default()
        }
    }

    fn contents(archive: ClusterArchive) -> BTreeMap<String, Vec<u8>> {
        let mut reader =
            sevenz_rust2::ArchiveReader::new(archive.file, sevenz_rust2::Password::empty())
                .unwrap();
        assert!(reader.archive().blocks.iter().all(|block| {
            block
                .coders
                .iter()
                .any(|coder| coder.encoder_method_id() == sevenz_rust2::EncoderMethod::ID_ZSTD)
        }));
        let mut files = BTreeMap::new();
        reader
            .for_each_entries(|entry, bytes| {
                let mut content = Vec::new();
                bytes.read_to_end(&mut content).unwrap();
                assert!(files.insert(entry.name.clone(), content).is_none());
                Ok(true)
            })
            .unwrap();
        files
    }

    #[tokio::test]
    async fn round_trip_preserves_native_progress_and_removes_targeted_credentials() {
        for (encode, source_encoded) in [(false, false), (true, false), (false, true), (true, true)]
        {
            let root = cluster();
            if source_encoded {
                let server = root.path().join("forest/server.ini");
                fs::write(
                    &server,
                    fs::read_to_string(&server)
                        .unwrap()
                        .replace("encode_user_path = false", "encode_user_path = true"),
                )
                .unwrap();
                let session = root.path().join("forest/save/session/0123456789ABCDEF");
                fs::rename(
                    session.join("KU_ABCDEFG__"),
                    session.join(encode_klei_id("KU_ABCDEFG_").unwrap()),
                )
                .unwrap();
                let index = root.path().join("forest/save/shardindex");
                fs::write(
                    &index,
                    fs::read_to_string(&index)
                        .unwrap()
                        .replace("encode_user_path=false", "encode_user_path=true"),
                )
                .unwrap();
            }
            let index_path = root.path().join("forest/save/shardindex");
            let before = fs::read(&index_path).unwrap();
            let mut lock = RoomLock::try_acquire(root.path()).unwrap();
            let mut options = options();
            options.encode_user_path = encode;
            let prepared = prepare(&mut lock.while_stopped(), &options).unwrap();
            // Copies stay consistent if native files change after stopped ownership ends.
            fs::write(
                root.path()
                    .join("forest/save/session/0123456789ABCDEF/000000001"),
                b"new world",
            )
            .unwrap();
            let files = contents(prepared.compress(DEFAULT_COMPRESSION_LEVEL).await.unwrap());
            assert_eq!(fs::read(index_path).unwrap(), before);
            assert_eq!(files.len(), 15);
            assert!(
                files
                    .values()
                    .all(|bytes| !String::from_utf8_lossy(bytes).contains("private-"))
            );
            let base = "001/forest/save/session/0123456789ABCDEF";
            assert_eq!(&files[&format!("{base}/000000001")], b"world\0\xff");
            let player = if encode || source_encoded {
                encode_klei_id("KU_ABCDEFG_").unwrap()
            } else {
                "KU_ABCDEFG__".into()
            };
            assert_eq!(
                &files[&format!("{base}/{player}/000000001")],
                b"player\0\xff"
            );
            assert_eq!(
                &files[&format!("{base}/{player}/savelocation")],
                b"\x81\0\0\0\x01\0\0\0\x02"
            );
            assert_eq!(
                &files["001/forest/save/mod_config_data/mod_worldjump_data_1"],
                b"worldjump-progress\0\xff"
            );
            let index = lua::parse_return_table(
                std::str::from_utf8(&files["001/forest/save/shardindex"]).unwrap(),
            )
            .unwrap();
            assert_eq!(
                index["server"],
                serde_json::json!({"privacy_type":0,"encode_user_path":encode || source_encoded})
            );
            assert_eq!(index["world"]["password"], "world-progress");
            let ini = String::from_utf8_lossy(&files["001/cluster.ini"]);
            assert!(!ini.contains("steam_group"));
            assert!(!ini.contains("cluster_key"));
        }
        assert_eq!(encode_klei_id("KU_ABCDEFG_").unwrap(), "A7H8MC6JHT1V");
    }

    #[tokio::test]
    async fn customized_configuration_preserves_topology_and_native_source_encoding() {
        let root = cluster();
        let mut lock = RoomLock::try_acquire(root.path()).unwrap();
        let mut value = ClusterConfig::load(&lock).unwrap().into_value();
        value["settings"]["cluster_name"] = "Customized archive".into();
        value["shards"]["forest"]["settings"]["encode_user_path"] = true.into();
        let mut options = ExportOptions {
            configuration: Some(ClusterConfig::from_value(value.clone()).unwrap()),
            ..options()
        };
        options.encode_user_path = false;
        let error = prepare(&mut lock.while_stopped(), &options).err().unwrap();
        assert!(
            error
                .to_string()
                .contains("preserve source player path encoding")
        );
        options.encode_user_path = true;
        let files = contents(
            prepare(&mut lock.while_stopped(), &options)
                .unwrap()
                .compress(DEFAULT_COMPRESSION_LEVEL)
                .await
                .unwrap(),
        );
        assert!(String::from_utf8_lossy(&files["001/cluster.ini"]).contains("Customized archive"));
        assert!(
            files
                .contains_key("001/forest/save/session/0123456789ABCDEF/A7H8MC6JHT1V/savelocation")
        );
        assert!(
            !String::from_utf8_lossy(&fs::read(root.path().join("cluster.ini")).unwrap())
                .contains("Customized archive")
        );
        let shard = value["shards"]
            .as_object_mut()
            .unwrap()
            .remove("forest")
            .unwrap();
        value["shards"]["different-world"] = shard;
        options.configuration = Some(ClusterConfig::from_value(value).unwrap());
        let error = prepare(&mut lock.while_stopped(), &options).err().unwrap();
        assert!(error.to_string().contains("source shard topology"));

        fs::remove_file(root.path().join("mods/modsettings.lua")).unwrap();
        fs::remove_file(root.path().join("mods/dedicated_server_mods_setup.lua")).unwrap();
        let prepared = prepare(
            &mut lock.while_stopped(),
            &ExportOptions {
                room_id: Some("001".into()),
                ..Default::default()
            },
        )
        .unwrap();
        assert!(prepared.entries.contains(Path::new("mods/modsettings.lua")));
        assert!(
            prepared
                .entries
                .contains(Path::new("mods/dedicated_server_mods_setup.lua"))
        );
    }

    #[test]
    fn rejects_symlinks_special_files_collisions_unsafe_paths_and_incomplete_indexes() {
        use std::os::unix::fs::symlink;
        for violation in [
            "symlink",
            "directory_symlink",
            "fifo",
            "collision",
            "windows",
            "case",
            "legacy",
            "index",
        ] {
            let root = cluster();
            let session = root.path().join("forest/save/session/0123456789ABCDEF");
            match violation {
                "symlink" => {
                    symlink(root.path().join("cluster_token.txt"), session.join("leak")).unwrap()
                }
                "directory_symlink" => {
                    fs::rename(&session, root.path().join("moved")).unwrap();
                    symlink(root.path().join("moved"), &session).unwrap();
                }
                "fifo" => {
                    use std::os::unix::ffi::OsStrExt;
                    let path = std::ffi::CString::new(session.join("pipe").as_os_str().as_bytes())
                        .unwrap();
                    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
                }
                "collision" => {
                    let path = session.join(encode_klei_id("KU_ABCDEFG_").unwrap());
                    fs::create_dir(&path).unwrap();
                    fs::write(path.join("other-snapshot"), b"other player").unwrap();
                }
                "windows" => fs::write(session.join("COM1.txt"), b"unsafe").unwrap(),
                "case" => {
                    fs::write(session.join("Other"), b"first").unwrap();
                    fs::write(session.join("other"), b"second").unwrap();
                }
                "legacy" => {
                    fs::remove_file(root.path().join("forest/save/shardindex")).unwrap();
                    fs::write(root.path().join("forest/save/saveindex"), b"legacy").unwrap();
                }
                "index" => fs::write(
                    root.path().join("forest/save/shardindex"),
                    b"return {server=execute()}",
                )
                .unwrap(),
                _ => unreachable!(),
            }
            let mut lock = RoomLock::try_acquire(root.path()).unwrap();
            assert!(
                prepare(&mut lock.while_stopped(), &options()).is_err(),
                "{violation}"
            );
        }
    }

    #[tokio::test]
    async fn cancelled_compression_retains_global_permit_until_worker_finishes() {
        let (started, start) = oneshot::channel();
        let (release, released) = std::sync::mpsc::channel();
        let task = tokio::spawn(compression_worker(move || {
            started.send(()).unwrap();
            released.recv().unwrap();
            Ok(())
        }));
        start.await.unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert!(COMPRESSION.try_acquire().is_err());
        release.send(()).unwrap();
        let _permit = tokio::time::timeout(Duration::from_secs(3), COMPRESSION.acquire())
            .await
            .unwrap()
            .unwrap();
    }

    #[derive(Clone, Copy, Debug)]
    enum Scenario {
        Success,
        PartFailure,
        Cancel,
        AbortFailure,
    }

    #[tokio::test]
    async fn multipart_upload_sets_attributes_and_aborts_after_errors_or_cancellation() {
        for scenario in [
            Scenario::Success,
            Scenario::PartFailure,
            Scenario::Cancel,
            Scenario::AbortFailure,
        ] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let endpoint = format!("http://{}", listener.local_addr().unwrap());
            let (part_started, part_seen) = oneshot::channel();
            let (release, released) = oneshot::channel();
            let server = tokio::spawn(async move {
                let mut requests = Vec::new();
                let mut part_started = Some(part_started);
                let mut released = Some(released);
                for _ in 0..3 {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    let mut buffer = Vec::new();
                    let split = loop {
                        let mut chunk = [0; 4096];
                        let read = stream.read(&mut chunk).await.unwrap();
                        assert!(read > 0);
                        buffer.extend_from_slice(&chunk[..read]);
                        if let Some(offset) =
                            buffer.windows(4).position(|bytes| bytes == b"\r\n\r\n")
                        {
                            break offset + 4;
                        }
                    };
                    let headers = String::from_utf8(buffer[..split].to_vec()).unwrap();
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.to_lowercase()
                                .strip_prefix("content-length: ")
                                .map(|length| length.parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    while buffer.len() < split + length {
                        let mut chunk = [0; 4096];
                        let read = stream.read(&mut chunk).await.unwrap();
                        assert!(read > 0);
                        buffer.extend_from_slice(&chunk[..read]);
                    }
                    let first = headers.lines().next().unwrap();
                    let (status, body) = if first.starts_with("POST ") && first.contains("uploads")
                    {
                        assert!(
                            headers
                                .to_lowercase()
                                .contains("content-type: application/x-7z-compressed")
                        );
                        (
                            200,
                            "<InitiateMultipartUploadResult><Bucket>test</Bucket><Key>room.7z</Key><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>",
                        )
                    } else if first.starts_with("PUT ") {
                        assert_eq!(&buffer[split..split + length], b"archive payload");
                        part_started.take().unwrap().send(()).unwrap();
                        if matches!(scenario, Scenario::Cancel) {
                            released.take().unwrap().await.unwrap();
                        }
                        if matches!(scenario, Scenario::PartFailure | Scenario::AbortFailure) {
                            (
                                403,
                                "<Error><Code>AccessDenied</Code><Message>test denial</Message></Error>",
                            )
                        } else {
                            (200, "")
                        }
                    } else if first.starts_with("DELETE ") {
                        if matches!(scenario, Scenario::AbortFailure) {
                            (
                                403,
                                "<Error><Code>AccessDenied</Code><Message>cannot abort</Message></Error>",
                            )
                        } else {
                            (204, "")
                        }
                    } else {
                        assert!(matches!(scenario, Scenario::Success));
                        (
                            200,
                            "<CompleteMultipartUploadResult><Bucket>test</Bucket><Key>room.7z</Key><ETag>etag</ETag></CompleteMultipartUploadResult>",
                        )
                    };
                    requests.push(headers);
                    let response = format!(
                        "HTTP/1.1 {status} result\r\nContent-Length: {}\r\nETag: \"etag\"\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    stream.write_all(response.as_bytes()).await.unwrap();
                    stream.shutdown().await.unwrap();
                }
                requests
            });
            let store = AmazonS3Builder::new()
                .with_bucket_name("test")
                .with_endpoint(endpoint)
                .with_region("auto")
                .with_access_key_id("test-key")
                .with_secret_access_key("test-secret")
                .with_token("test-token")
                .with_allow_http(true)
                .with_retry(object_store::RetryConfig {
                    max_retries: 0,
                    ..Default::default()
                })
                .build()
                .unwrap();
            let mut file = tempfile::tempfile().unwrap();
            file.write_all(b"archive payload").unwrap();
            let archive = ClusterArchive {
                filename: "room.7z".into(),
                file,
            };
            let task = archive
                .start_upload(Arc::new(store), "prefix/pre-", Some("https://public/"))
                .unwrap();
            tokio::time::timeout(Duration::from_secs(5), part_seen)
                .await
                .unwrap()
                .unwrap();
            let outcome = if matches!(scenario, Scenario::Cancel) {
                drop(task);
                release.send(()).unwrap();
                None
            } else {
                Some(task.wait().await)
            };
            let requests = tokio::time::timeout(Duration::from_secs(5), server)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(requests.len(), 3);
            assert!(requests[0].contains("/test/prefix/pre-room.7z?uploads"));
            assert!(
                requests[0]
                    .to_lowercase()
                    .contains("x-amz-security-token: test-token")
            );
            match scenario {
                Scenario::Success => {
                    let result = outcome.unwrap().unwrap();
                    assert_eq!(result.key, "prefix/pre-room.7z");
                    assert_eq!(result.url.as_deref(), Some("https://public/pre-room.7z"));
                    assert!(requests[2].starts_with("POST "));
                }
                Scenario::PartFailure => {
                    assert!(
                        format!("{:#}", outcome.unwrap().unwrap_err())
                            .contains("upload archive part")
                    );
                    assert!(requests[2].starts_with("DELETE "));
                }
                Scenario::Cancel => assert!(requests[2].starts_with("DELETE ")),
                Scenario::AbortFailure => {
                    assert!(
                        format!("{:#}", outcome.unwrap().unwrap_err())
                            .contains("multipart abort failed")
                    );
                    assert!(requests[2].starts_with("DELETE "));
                }
            }
        }
    }
}
