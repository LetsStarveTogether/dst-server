//! Shared native Mod files and one supervised downloader attempt.
//! The Agent owns room exclusion, the retry budget and any restart afterwards.

use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    net::UdpSocket,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::Duration,
};

use full_moon::ast::{Expression, FunctionCall, LastStmt, Stmt};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    files::{self, PermissionFiles, RoomLock},
    lua,
    model::{Error, ErrorCode, Result},
    process::{EventKind, GameProcess, ProcessOutput, ProcessSpec, StreamKind},
    telemetry::LogEvent,
};

pub(crate) mod monitor;

const MAX_VERSION_BYTES: usize = 1024;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateTarget {
    pub version: String,
    pub shards: BTreeSet<String>,
}

pub type UpdateTargets = BTreeMap<u64, UpdateTarget>;

pub(crate) fn valid_version(value: &str) -> bool {
    !value.is_empty() && value.len() <= MAX_VERSION_BYTES && !value.chars().any(char::is_control)
}

pub(crate) fn validate_targets(targets: &UpdateTargets) -> anyhow::Result<()> {
    anyhow::ensure!(
        targets.iter().all(|(id, target)| *id != 0
            && valid_version(&target.version)
            && !target.shards.is_empty()
            && target.shards.iter().all(|shard| !shard.is_empty())),
        "invalid Mod update target"
    );
    Ok(())
}

pub const EXECUTABLE: &str = "bin64/dontstarve_dedicated_server_nullrenderer_x64";
pub const UPDATE_PROCESS_TIMEOUT: Duration = Duration::from_secs(30 * 60);
pub const UPDATE_COMPLETE: &str =
    "FinishDownloadingServerMods Complete! Process trying to quit nicely..";
const SETUP: &str = "mods/dedicated_server_mods_setup.lua";
const SETTINGS: &str = "mods/modsettings.lua";
const SETUP_FAILURE: &str = "#ERROR: Failure to load dedicated_server_mods_setup.lua:";
const DOWNLOAD_TIMEOUT: &str = "DownloadServerMods timed out with no response from Workshop...";
const DOWNLOAD_FAILURES: [&str; 4] = [
    "[Workshop] ItemQuery failed entirely, unrecoverable.",
    "[Workshop] CollectionQuery failed entirely, unrecoverable.",
    "[Workshop] ODPF failed entirely: ",
    "[Workshop] FAILED: DownloadPublishedFile [",
];

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Setup {
    pub items: BTreeSet<u64>,
    pub collections: BTreeSet<u64>,
    pub has_code: bool,
}

/// Extract only direct, top-level declarations. Dynamic Lua remains executable
/// by DST itself and is never interpreted by the controller.
pub fn scan_setup(source: &str) -> Result<Setup> {
    let ast = lua::parse_script(source)
        .map_err(|error| Error::invalid("mods.setup", error.to_string()))?;
    let mut setup = Setup {
        has_code: ast.nodes().stmts().next().is_some() || ast.nodes().last_stmt().is_some(),
        ..Setup::default()
    };
    for statement in ast.nodes().stmts() {
        if let Stmt::FunctionCall(call) = statement {
            collect_call(call, &mut setup)?;
        }
    }
    if let Some(LastStmt::Return(statement)) = ast.nodes().last_stmt() {
        for expression in statement.returns() {
            if let Expression::FunctionCall(call) = expression {
                collect_call(call, &mut setup)?;
            }
        }
    }
    Ok(setup)
}

fn collect_call(call: &FunctionCall, setup: &mut Setup) -> Result<()> {
    let Some((name, value)) = lua::string_call(call) else {
        return Ok(());
    };
    let items = match name {
        "ServerModSetup" if !value.is_empty() => &mut setup.items,
        "ServerModCollectionSetup" => &mut setup.collections,
        _ => return Ok(()),
    };
    if value.is_ascii() {
        let id = crate::settings::workshop_id(&value)
            .map_err(|error| Error::invalid("mods.setup", error.to_string()))?;
        items.insert(id);
    }
    Ok(())
}

/// The caller must hold the room stopped for preparation and the entire update.
/// This owned value permits releasing a synchronous RoomLock mutex before await.
pub struct PreparedMods {
    executable: PathBuf,
    ugc: PathBuf,
    proxy: Option<String>,
    pub setup: Setup,
}

impl PreparedMods {
    pub fn prepare(room: &mut RoomLock, executable: &Path, proxy: Option<String>) -> Result<Self> {
        validate_proxy(proxy.as_deref())?;
        let executable = fs::canonicalize(executable).map_err(io_error)?;
        if !executable.is_file() {
            return Err(Error::invalid(
                "executable",
                "DST executable must be a regular file",
            ));
        }
        let setup = prepare_shared(room)?;
        let ugc = room.directory().join("mods/ugc");
        if setup.has_code {
            activate(&executable, room.directory())?;
        }
        Ok(Self {
            executable,
            ugc,
            proxy,
            setup,
        })
    }

    pub async fn update(&self, emit: &(impl Fn(LogEvent) + Sync)) -> Result<Value> {
        let (_keep_open, cancelled) = tokio::sync::watch::channel(0);
        self.update_cancellable(emit, cancelled).await
    }

    /// An interruption returns only after the updater's process group has exited
    /// and all native output has drained, so the room may safely reopen afterward.
    pub async fn update_cancellable(
        &self,
        emit: &(impl Fn(LogEvent) + Sync),
        cancelled: tokio::sync::watch::Receiver<u64>,
    ) -> Result<Value> {
        if cancelled.has_changed().unwrap_or(true) {
            return Err(Error::shutdown(ErrorCode::Unknown));
        }
        let process = self.spawn_update()?;
        self.finish_update(process, emit, cancelled).await
    }

    pub(crate) fn spawn_update(&self) -> Result<Option<UpdateProcess>> {
        self.setup
            .has_code
            .then(|| UpdateProcess::spawn(&self.executable, &self.ugc, self.proxy.as_deref()))
            .transpose()
    }

    pub(crate) async fn finish_update(
        &self,
        process: Option<UpdateProcess>,
        emit: &(impl Fn(LogEvent) + Sync),
        cancelled: tokio::sync::watch::Receiver<u64>,
    ) -> Result<Value> {
        if let Some(process) = process {
            process
                .wait(
                    self.proxy.as_deref(),
                    emit,
                    UPDATE_PROCESS_TIMEOUT,
                    cancelled,
                )
                .await?;
        }
        Ok(
            json!({"download_completed": self.setup.has_code, "items": self.setup.items, "collections": self.setup.collections}),
        )
    }
}

/// Validate every existing managed file before creating any shared Mod files.
/// Existing setup and settings scripts are preserved byte for byte.
pub fn prepare_shared(room: &mut RoomLock) -> Result<Setup> {
    let setup_source = room.read_optional_text(SETUP).map_err(file_error)?;
    let settings = room.read_optional_text(SETTINGS).map_err(file_error)?;
    let ugc = room.directory().join("mods/ugc");
    if let Ok(metadata) = fs::symlink_metadata(&ugc)
        && !metadata.file_type().is_dir()
    {
        return Err(Error::invalid(
            "mods.ugc",
            "shared UGC must be a directory without symlinks",
        ));
    }
    let setup = scan_setup(setup_source.as_deref().unwrap_or_default())?;
    files::create_directory(&ugc).map_err(file_error)?;
    let mut changes = BTreeMap::new();
    if setup_source.is_none() {
        changes.insert(SETUP.into(), String::new());
    }
    if settings.is_none() {
        changes.insert(SETTINGS.into(), String::new());
    }
    room.while_stopped()
        .write_files(changes, Vec::new(), PermissionFiles::Preserve)
        .map_err(file_error)?;
    Ok(setup)
}

/// The room container mounts the same Mod directory at /install/mods and inside
/// its native cluster. Require that shared directory before invoking DST.
pub fn activate(executable: &Path, room: &Path) -> Result<()> {
    let executable = fs::canonicalize(executable).map_err(io_error)?;
    let room = fs::canonicalize(room).map_err(io_error)?;
    let install = executable.parent().and_then(Path::parent).ok_or_else(|| {
        Error::invalid(
            "executable",
            "DST executable must reside in its bin64 directory",
        )
    })?;
    let install_mods = install.join("mods");
    let room_mods = room.join("mods");
    if install_mods == room_mods
        || install_mods.starts_with(&room_mods)
        || room_mods.starts_with(&install_mods)
    {
        return Err(Error::invalid(
            "mods",
            "install and room Mod paths cannot contain each other",
        ));
    }
    let source = fs::metadata(&room_mods).map_err(io_error)?;
    let target = fs::metadata(&install_mods).map_err(io_error)?;
    if !source.is_dir()
        || !target.is_dir()
        || source.dev() != target.dev()
        || source.ino() != target.ino()
    {
        return Err(Error::new(
            ErrorCode::Conflict,
            "install/mods must mount the room's shared Mod directory",
        ));
    }
    Ok(())
}

pub(crate) fn proxy_from_env() -> Result<Option<String>> {
    let proxy = std::env::var("DST_SERVER_MOD_PROXY")
        .ok()
        .filter(|proxy| !proxy.is_empty());
    validate_proxy(proxy.as_deref())?;
    Ok(proxy)
}

pub fn validate_proxy(proxy: Option<&str>) -> Result<()> {
    let Some(proxy) = proxy else {
        return Ok(());
    };
    let valid = !proxy.is_empty()
        && !proxy.chars().any(char::is_control)
        && reqwest::Url::parse(proxy).is_ok_and(|url| {
            matches!(url.scheme(), "http" | "https")
                && url.host_str().is_some()
                && url.port().is_none_or(|port| port > 0)
        });
    if !valid {
        return Err(Error::invalid(
            "proxy",
            "download proxy must be an HTTP(S) URL with a valid host and port",
        ));
    }
    Ok(())
}

pub fn download_environment(proxy: Option<&str>) -> Result<Vec<(OsString, OsString)>> {
    validate_proxy(proxy)?;
    let mut environment: Vec<_> = std::env::vars_os()
        .filter(|(name, _)| {
            !matches!(
                name.to_string_lossy().to_ascii_lowercase().as_str(),
                "http_proxy" | "https_proxy" | "all_proxy" | "ftp_proxy" | "no_proxy"
            )
        })
        .collect();
    if let Some(proxy) = proxy {
        environment.extend([
            ("http_proxy".into(), proxy.into()),
            ("https_proxy".into(), proxy.into()),
        ]);
    }
    Ok(environment)
}

/// Execute exactly one download attempt. Partial downloads remain available for
/// the Agent's next attempt, and a completion marker never overrides a failure.
pub async fn update_native(
    executable: &Path,
    ugc: &Path,
    proxy: Option<&str>,
    emit: &(impl Fn(LogEvent) + Sync),
) -> Result<()> {
    let (_keep_open, cancelled) = tokio::sync::watch::channel(0);
    update_once(
        executable,
        ugc,
        proxy,
        emit,
        UPDATE_PROCESS_TIMEOUT,
        cancelled,
    )
    .await
}

async fn update_once(
    executable: &Path,
    ugc: &Path,
    proxy: Option<&str>,
    emit: &(impl Fn(LogEvent) + Sync),
    timeout: Duration,
    cancelled: tokio::sync::watch::Receiver<u64>,
) -> Result<()> {
    UpdateProcess::spawn(executable, ugc, proxy)?
        .wait(proxy, emit, timeout, cancelled)
        .await
}

pub(crate) struct UpdateProcess {
    process: GameProcess,
    output: ProcessOutput,
    _temporary: tempfile::TempDir,
}

impl UpdateProcess {
    fn spawn(executable: &Path, ugc: &Path, proxy: Option<&str>) -> Result<Self> {
        let environment = download_environment(proxy)?;
        let executable = fs::canonicalize(executable).map_err(io_error)?;
        let ugc = fs::canonicalize(ugc).map_err(io_error)?;
        let temporary = tempfile::Builder::new()
            .prefix("dst-mod-update-")
            .tempdir()
            .map_err(io_error)?;
        fs::create_dir_all(temporary.path().join("conf/cluster/shard")).map_err(io_error)?;
        // Hold both sockets until their distinct ephemeral port numbers are chosen.
        let game_socket = UdpSocket::bind(("0.0.0.0", 0)).map_err(io_error)?;
        let master_socket = UdpSocket::bind(("0.0.0.0", 0)).map_err(io_error)?;
        let game_port = game_socket.local_addr().map_err(io_error)?.port();
        let master_port = master_socket.local_addr().map_err(io_error)?.port();
        let args = vec![
            "-only_update_server_mods".into(),
            "-monitor_parent_process".into(),
            std::process::id().to_string().into(),
            "-port".into(),
            game_port.to_string().into(),
            "-steam_master_server_port".into(),
            master_port.to_string().into(),
            "-ugc_directory".into(),
            ugc.into_os_string(),
            "-persistent_storage_root".into(),
            temporary.path().as_os_str().to_owned(),
            "-conf_dir".into(),
            "conf".into(),
            "-cluster".into(),
            "cluster".into(),
            "-shard".into(),
            "shard".into(),
        ];
        drop((game_socket, master_socket));
        let (process, output) = GameProcess::spawn_with_environment(
            ProcessSpec {
                cwd: executable
                    .parent()
                    .expect("canonical executable has a parent")
                    .into(),
                program: executable,
                args,
            },
            environment,
        )
        .map_err(io_error)?;
        Ok(Self {
            process,
            output,
            _temporary: temporary,
        })
    }

    async fn wait(
        mut self,
        proxy: Option<&str>,
        emit: &(impl Fn(LogEvent) + Sync),
        timeout: Duration,
        mut cancelled: tokio::sync::watch::Receiver<u64>,
    ) -> Result<()> {
        let process = &self.process;
        let outcome = tokio::select! {
            outcome = tokio::time::timeout(timeout, observe(process, &mut self.output, proxy, emit)) => outcome,
            _ = cancelled.changed() => {
                process.kill().await.map_err(io_error)?;
                return Err(Error::new(ErrorCode::Unknown, "DST Mod update was interrupted after downloader cleanup"));
            }
        };
        match outcome {
            Ok(result) => result,
            Err(_) => {
                process.kill().await.map_err(io_error)?;
                Err(Error::new(ErrorCode::Timeout, "DST Mod updater timed out"))
            }
        }
    }
}

async fn observe(
    process: &GameProcess,
    output: &mut ProcessOutput,
    proxy: Option<&str>,
    emit: &(impl Fn(LogEvent) + Sync),
) -> Result<()> {
    let mut completed = false;
    let mut failure = None;
    while let Some(event) = output.next_event().await {
        if let EventKind::Line(bytes) = event.kind {
            if !matches!(event.stream, StreamKind::Stdout | StreamKind::Stderr) {
                continue;
            }
            let line = String::from_utf8_lossy(&bytes);
            let line = line.trim_end_matches(['\r', '\n']);
            let line = proxy.map_or_else(|| line.to_owned(), |proxy| line.replace(proxy, "***"));
            let mut record = LogEvent::new("dst.mod_update", json!(line), "INFO");
            record.attributes.insert(
                "stream".into(),
                json!(if event.stream == StreamKind::Stderr {
                    "stderr"
                } else {
                    "stdout"
                }),
            );
            emit(record);
            let message = line
                .split_once("]: ")
                .map_or(line.as_str(), |(_, message)| message)
                .trim_end();
            completed |= message == UPDATE_COMPLETE;
            if message.starts_with(SETUP_FAILURE)
                || failure.is_none()
                    && (message == DOWNLOAD_TIMEOUT
                        || DOWNLOAD_FAILURES
                            .iter()
                            .any(|prefix| message.starts_with(prefix)))
            {
                failure = Some(message.chars().take(4096).collect::<String>());
            }
        }
    }
    let report = process.wait().await.map_err(io_error)?;
    if !report.status.success() {
        return Err(Error::new(
            ErrorCode::Internal,
            format!("DST Mod updater exited with {}", report.status),
        ));
    }
    if !report.output_drained
        || report.protocol_error.is_some()
        || report
            .stats
            .dropped
            .iter()
            .chain(&report.stats.oversized)
            .chain(&report.stats.read_errors)
            .any(|count| *count != 0)
    {
        return Err(Error::new(
            ErrorCode::Overflow,
            "DST Mod updater output was incomplete",
        ));
    }
    if let Some(failure) = failure {
        return Err(Error::new(
            ErrorCode::Internal,
            format!("DST Mod updater failed: {failure}"),
        ));
    }
    if !completed {
        return Err(Error::new(
            ErrorCode::Internal,
            "DST Mod updater exited without reporting completion",
        ));
    }
    Ok(())
}

fn io_error(error: std::io::Error) -> Error {
    Error::new(
        if error.kind() == std::io::ErrorKind::NotFound {
            ErrorCode::NotFound
        } else {
            ErrorCode::Internal
        },
        format!("Mod file or process error: {error}"),
    )
}

fn file_error(error: anyhow::Error) -> Error {
    Error::invalid("mods", error.to_string())
}
