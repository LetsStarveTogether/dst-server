//! Native save imports, exports in memory, and cancellable multipart uploads.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Cursor, Seek};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};

use anyhow::{Context, Result, ensure};
use object_store::aws::{AmazonS3Builder, AmazonS3ConfigKey};
use object_store::{Attribute, Attributes, ObjectStore, PutMultipartOptions};
use serde::{Deserialize, Serialize};
use sevenz_rust2::encoder_options::ZstandardOptions;
use sevenz_rust2::{ArchiveEntry, ArchiveWriter};
use tokio::io::AsyncReadExt;
use tokio::sync::{Semaphore, oneshot};

use crate::files::RoomReader;
use crate::settings::ClusterConfig;
mod client;
mod importing;
mod native;
pub(crate) mod zip;

pub use importing::{ImportOptions, ImportSource, import_cluster};
pub use native::encode_klei_id;
use native::validate_path;

pub const DEFAULT_COMPRESSION_LEVEL: u32 = 22;
const PART_BYTES: usize = 8 * 1024 * 1024;
static COMPRESSION: LazyLock<Arc<Semaphore>> = LazyLock::new(|| Arc::new(Semaphore::new(1)));

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct ExportOptions {
    pub room_id: Option<String>,
    pub client_compatible: bool,
    pub configuration: Option<ClusterConfig>,
}

/// Native files captured in memory while the game may continue saving.
pub struct PreparedArchive {
    filename: String,
    room_id: String,
    entries: BTreeMap<PathBuf, Vec<u8>>,
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

/// Read a room directly; exports do not need its Agent, service or ownership lock.
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
    compression_worker(move || {
        prepare(&RoomReader::open(directory)?, number, &options)?.compress_sync(level)
    })
    .await
}

/// An anonymous RAM file keeps Rust and Python seekable streams on the same data.
pub fn memory_file() -> Result<File> {
    // SAFETY: the name is NUL terminated; a successful descriptor is owned below.
    let fd = unsafe { libc::memfd_create(c"dst-archive".as_ptr(), libc::MFD_CLOEXEC) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("create archive in memory");
    }
    // SAFETY: memfd_create returned a new descriptor owned by this File.
    Ok(unsafe { File::from_raw_fd(fd) })
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

/// Capture persistent files once in RAM without stopping the game or forcing a save.
pub fn prepare(room: &RoomReader, number: u16, options: &ExportOptions) -> Result<PreparedArchive> {
    let root = room.directory();
    let room_id = if options.client_compatible {
        ensure!(
            options.room_id.is_none(),
            "client exports use Cluster_<number>; room_id cannot be overridden"
        );
        format!("Cluster_{number}")
    } else {
        options
            .room_id
            .as_deref()
            .or_else(|| root.file_name()?.to_str())
            .context("archive room ID is required")?
            .to_owned()
    };
    ensure!(
        !room_id.is_empty()
            && room_id.len() <= 80
            && room_id.as_bytes()[0].is_ascii_alphanumeric()
            && room_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')),
        "archive room ID must contain 1-80 ASCII letters, digits, underscores or hyphens"
    );
    let files = native::NativeFiles::read_directory(room)?;
    let configuration = files.configuration()?;
    let files = files.normalize(
        configuration,
        options.configuration.as_ref(),
        if options.client_compatible {
            native::Conversion::ToClient
        } else {
            native::Conversion::None
        },
    )?;
    Ok(PreparedArchive {
        filename: format!(
            "DST-{room_id}-{}.7z",
            chrono::Utc::now().format("%Y%m%dT%H%M%SZ")
        ),
        room_id,
        entries: files.entries,
    })
}

impl PreparedArchive {
    pub async fn compress(self, level: u32) -> Result<ClusterArchive> {
        compression_worker(move || self.compress_sync(level)).await
    }

    fn compress_sync(self, level: u32) -> Result<ClusterArchive> {
        ensure!(
            (1..=22).contains(&level),
            "Zstandard compression level must be between 1 and 22"
        );
        let mut writer = ArchiveWriter::new(memory_file()?)?;
        writer.set_content_methods(vec![ZstandardOptions::from_level(level).into()]);
        for (path, bytes) in self.entries {
            let name = format!(
                "{}/{}",
                self.room_id,
                path.to_str().context("archive path is not UTF-8")?
            );
            writer
                .push_archive_entry(ArchiveEntry::new_file(&name), Some(Cursor::new(bytes)))
                .context("compress archive file")?;
        }
        let mut file = writer.finish().context("finish native archive")?;
        file.rewind().context("rewind native archive")?;
        Ok(ClusterArchive {
            filename: self.filename,
            file,
        })
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

#[cfg(test)]
mod tests;
