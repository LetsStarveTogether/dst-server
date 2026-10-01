//! Shared native Mod files and one supervised downloader attempt.
//! The Agent owns room exclusion, the retry budget and any restart afterwards.

use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    net::UdpSocket,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use full_moon::ast::{
    Call, Expression, FunctionArgs, FunctionCall, LastStmt, Prefix, Stmt, Suffix,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    files::{self, PermissionFiles, RoomLock},
    lua,
    model::{Error, ErrorCode, Result},
    process::{EventKind, GameProcess, ProcessSpec, StreamKind},
    rpc::EventHub,
};

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
    let Prefix::Name(name) = call.prefix() else {
        return Ok(());
    };
    let name = name.token().to_string();
    let collection = match name.as_str() {
        "ServerModSetup" => false,
        "ServerModCollectionSetup" => true,
        _ => return Ok(()),
    };
    let mut suffixes = call.suffixes();
    let Some(Suffix::Call(Call::AnonymousCall(arguments))) = suffixes.next() else {
        return Ok(());
    };
    if suffixes.next().is_some() {
        return Ok(());
    }
    let string = match arguments {
        FunctionArgs::Parentheses { arguments, .. } if arguments.len() == 1 => {
            match arguments.iter().next() {
                Some(Expression::String(string)) => string,
                _ => return Ok(()),
            }
        }
        FunctionArgs::String(string) => string,
        _ => return Ok(()),
    };
    // Native Lua strings are bytes; non-UTF-8/non-ASCII IDs are not declarations.
    let Ok(value) = lua::string(string) else {
        return Ok(());
    };
    if !value.is_ascii() || value.is_empty() && !collection {
        return Ok(());
    }
    if value.is_empty()
        || value.starts_with('0')
        || !value.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(Error::invalid(
            "mods.setup",
            "Workshop IDs must be positive decimal integers without leading zeros",
        ));
    }
    let item = value
        .parse::<u64>()
        .map_err(|_| Error::invalid("mods.setup", "Workshop ID exceeds uint64"))?;
    if collection {
        setup.collections.insert(item);
    } else {
        setup.items.insert(item);
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

    pub async fn update(&self, events: &EventHub) -> Result<Value> {
        let (_keep_open, cancelled) = tokio::sync::watch::channel(0);
        self.update_cancellable(events, cancelled).await
    }

    /// An interruption returns only after the updater's process group has exited
    /// and all native output has drained, so the room may safely reopen afterward.
    pub async fn update_cancellable(
        &self,
        events: &EventHub,
        cancelled: tokio::sync::watch::Receiver<u64>,
    ) -> Result<Value> {
        if self.setup.has_code {
            update_once(
                &self.executable,
                &self.ugc,
                self.proxy.as_deref(),
                events,
                UPDATE_PROCESS_TIMEOUT,
                cancelled,
            )
            .await?;
        }
        Ok(
            json!({"updated": self.setup.has_code, "items": self.setup.items, "collections": self.setup.collections}),
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
        .commit(changes, PermissionFiles::Preserve)
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
    events: &EventHub,
) -> Result<()> {
    let (_keep_open, cancelled) = tokio::sync::watch::channel(0);
    update_once(
        executable,
        ugc,
        proxy,
        events,
        UPDATE_PROCESS_TIMEOUT,
        cancelled,
    )
    .await
}

async fn update_once(
    executable: &Path,
    ugc: &Path,
    proxy: Option<&str>,
    events: &EventHub,
    timeout: Duration,
    mut cancelled: tokio::sync::watch::Receiver<u64>,
) -> Result<()> {
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
    let process = GameProcess::spawn_with_environment(
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
    let outcome = tokio::select! {
        outcome = tokio::time::timeout(timeout, observe(&process, proxy, events)) => outcome,
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

async fn observe(process: &GameProcess, proxy: Option<&str>, events: &EventHub) -> Result<()> {
    let mut completed = false;
    let mut failure = None;
    while let Some(event) = process.next_event().await {
        if let EventKind::Line(bytes) = event.kind {
            if !matches!(event.stream, StreamKind::Stdout | StreamKind::Stderr) {
                continue;
            }
            let line = String::from_utf8_lossy(&bytes);
            let line = line.trim_end_matches(['\r', '\n']);
            let line = proxy.map_or_else(|| line.to_owned(), |proxy| line.replace(proxy, "***"));
            events.publish("logs", &json!({
                "event_name": "dst.mod_update", "body": line,
                "severity_text": "INFO", "observed_timestamp_ns": timestamp(),
                "attributes": {"stream": if event.stream == StreamKind::Stderr {"stderr"} else {"stdout"}},
            }));
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

fn timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64
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
