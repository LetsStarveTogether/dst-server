//! Native configuration I/O and room ownership.
//! Offline edits validate their inputs, then replace each file independently.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{CString, OsStr};
use std::fs::{File, TryLockError};
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value};

use crate::{configuration, lua};

pub const ROOM_LOCK_FILE: &str = ".dst-room.lock";
pub const STATE_FILE: &str = ".dst-server.json";
pub const MAX_FILE_BYTES: usize = 1024 * 1024;
pub const MAX_CONFIG_FILES: usize = 1024;
pub const MAX_CONFIG_BYTES: usize = 16 * 1024 * 1024;
const WRITE_TEMPORARY: &str = ".dst-config-write.tmp";
const PERMISSION_FILES: [&str; 3] = ["adminlist.txt", "whitelist.txt", "blocklist.txt"];

pub type IniDocument = BTreeMap<String, BTreeMap<String, String>>;
pub type FileChanges = BTreeMap<PathBuf, String>;

/// Native blocklist records separate their user ID and metadata with byte 0xBA.
/// The remaining fields can contain arbitrary bytes and are never text decoded.
fn permission_userid(record: &[u8]) -> Option<&str> {
    let userid = record
        .split(|byte| matches!(byte, b'\n' | b'\r' | 0xba))
        .next()?;
    std::str::from_utf8(userid)
        .ok()
        .filter(|userid| !userid.is_empty())
}

pub(crate) fn permission_users(source: &[u8]) -> Vec<String> {
    source
        .split(|byte| *byte == b'\n')
        .filter_map(permission_userid)
        .map(str::to_owned)
        .collect()
}

/// Preserve every unrelated record, including native ban timestamps and names.
pub(crate) fn edit_permission(source: &[u8], userid: &str, remove: bool) -> Vec<u8> {
    let mut found = false;
    let mut output = Vec::with_capacity(source.len() + userid.len() + 1);
    for record in source.split_inclusive(|byte| *byte == b'\n') {
        if permission_userid(record) == Some(userid) {
            found = true;
            if remove {
                continue;
            }
        }
        output.extend_from_slice(record);
    }
    if !remove && !found {
        if !output.is_empty() && !output.ends_with(b"\n") {
            output.push(b'\n');
        }
        output.extend_from_slice(userid.as_bytes());
        output.push(b'\n');
    }
    output
}

/// Read native files without taking ownership of the room or following symlinks.
pub struct RoomReader {
    directory: PathBuf,
    root: Directory,
}

impl RoomReader {
    pub fn open(directory: impl AsRef<Path>) -> Result<Self> {
        let directory = directory.as_ref();
        Ok(Self {
            directory: directory.to_owned(),
            root: Directory::open(directory)?,
        })
    }

    pub fn directory(&self) -> &Path {
        &self.directory
    }

    pub fn read_text(&self, relative: impl AsRef<Path>) -> Result<String> {
        let relative = relative.as_ref();
        let (parent, name) = self.root.parent(relative, false)?;
        parent.read_text(&name, MAX_FILE_BYTES)
    }

    pub fn read_optional_text(&self, relative: impl AsRef<Path>) -> Result<Option<String>> {
        self.read_optional_bytes(relative)?
            .map(String::from_utf8)
            .transpose()
            .context("read UTF-8 configuration text")
    }

    pub fn read_optional_bytes(&self, relative: impl AsRef<Path>) -> Result<Option<Vec<u8>>> {
        let Some((parent, name)) = self.root.existing_parent(relative.as_ref())? else {
            return Ok(None);
        };
        if parent.kind(&name)?.is_none() {
            return Ok(None);
        }
        parent.read_bytes(&name, MAX_FILE_BYTES).map(Some)
    }

    pub(crate) fn open_regular(&self, relative: impl AsRef<Path>) -> Result<File> {
        let (parent, name) = self.root.parent(relative.as_ref(), false)?;
        parent.open_file(&name, libc::O_RDONLY, 0)
    }

    pub(crate) fn list_directory(
        &self,
        relative: impl AsRef<Path>,
    ) -> Result<Vec<(String, std::fs::FileType)>> {
        let relative = relative.as_ref();
        if relative.as_os_str().is_empty() {
            return self.root.entries();
        }
        let (parent, name) = self.root.parent(relative, false)?;
        parent.child(&name, false, 0o755)?.entries()
    }

    /// Immediate directory names, excluding controller and shared Mod directories.
    pub fn shard_directories(&self) -> Result<Vec<String>> {
        self.root.directory_names(true)
    }

    pub fn read_ini(&self, relative: impl AsRef<Path>) -> Result<IniDocument> {
        let relative = relative.as_ref();
        let source = self.read_text(relative)?;
        Ok(configuration::Ini::parse(relative, &source, ini_kind(relative))?.into_sections())
    }

    /// Static data only: executable Lua produces an error here without affecting
    /// topology discovery or the game's native loading of the same file.
    pub fn read_lua_table(&self, relative: impl AsRef<Path>) -> Result<Value> {
        lua::parse_return_table(&self.read_text(relative)?)
    }
}

/// Hold this lock for the Agent's complete lifetime, including running games.
/// Mutating Host operations use the same lock and fail while the Agent owns it.
/// The lock file must never be removed or replaced while the room exists.
pub struct RoomLock {
    reader: RoomReader,
    _lock: File,
    owner_pid: u32,
}

impl std::ops::Deref for RoomLock {
    type Target = RoomReader;
    fn deref(&self) -> &RoomReader {
        &self.reader
    }
}

impl Drop for RoomLock {
    fn drop(&mut self) {
        if self.owner_pid == std::process::id() {
            // Forked children share this open file description until exec. Close
            // alone can retain the lock; only the acquiring process may unlock it.
            let _ = self._lock.unlock();
        }
    }
}

impl RoomLock {
    pub fn try_acquire(directory: impl AsRef<Path>) -> Result<Self> {
        let directory = directory.as_ref();
        let root = Directory::open(directory)?;
        let lock = root.open_file(ROOM_LOCK_FILE, libc::O_RDWR | libc::O_CREAT, 0o600)?;
        lock.try_lock()
            .map_err(|error| match error {
                TryLockError::WouldBlock => std::io::Error::from_raw_os_error(libc::EWOULDBLOCK),
                TryLockError::Error(error) => error,
            })
            .context("room is busy or cannot be locked")?;
        let room = Self {
            reader: RoomReader {
                directory: directory.to_owned(),
                root,
            },
            _lock: lock,
            owner_pid: std::process::id(),
        };
        let legacy = ".dst-control.json";
        if room.root.kind(legacy)?.is_some() {
            room.root.require_regular_or_absent(legacy)?;
            ensure!(
                room.root.kind(STATE_FILE)?.is_none(),
                "both old and new room state files exist"
            );
            room.root.rename(legacy, STATE_FILE)?;
            room.root.sync()?;
        }
        Ok(room)
    }

    /// The caller must ensure every game process has exited and its output has
    /// drained before creating this handle, and keep games stopped until it drops.
    /// This is an ownership contract, not process detection or a stop operation.
    pub fn while_stopped(&mut self) -> StoppedRoom<'_> {
        StoppedRoom { room: self }
    }

    /// Admin lists have no native mutation API; the Agent reloads permissions
    /// after this replacement while holding its operation and room locks.
    pub(crate) fn replace_adminlist(&mut self, contents: &[u8]) -> Result<()> {
        self.replace_permission("adminlist.txt", contents)
    }

    fn replace_permission(&mut self, name: &str, contents: &[u8]) -> Result<()> {
        ensure!(
            PERMISSION_FILES.contains(&name),
            "invalid permission filename"
        );
        ensure!(
            contents.len() <= MAX_FILE_BYTES,
            "permission file exceeds the byte limit"
        );
        self.root.replace(name, contents, 0o600)
    }

    /// The native storage API can read a shard's parent, but rejects room-level traversal.
    pub(crate) fn write_driver_options(&self, encoded: &[u8]) -> Result<()> {
        for shard in self.shard_directories()? {
            let directory = self.root.child(&shard, false, 0o755)?;
            if directory.kind("server.ini")?.is_some() {
                directory.replace("dst_server_driver.json", encoded, 0o600)?;
            }
        }
        Ok(())
    }

    pub fn read_state(&self) -> Result<Map<String, Value>> {
        if self.root.kind(STATE_FILE)?.is_none() {
            return Ok(Map::new());
        }
        let source = self.root.read_text(STATE_FILE, MAX_FILE_BYTES)?;
        let value: Value = serde_json::from_str(&source)
            .map_err(|_| anyhow::anyhow!("invalid room state JSON"))?;
        match value {
            Value::Object(object) => Ok(object),
            _ => bail!("room state JSON must be an object"),
        }
    }

    /// Re-read the SDK state file before changing one part of it so recovery,
    /// policy and activity updates preserve each other's fields.
    pub fn update_state<T>(
        &mut self,
        update: impl FnOnce(&mut Map<String, Value>) -> Result<T>,
    ) -> Result<T> {
        let mut value = self.read_state()?;
        let original = value.clone();
        let result = update(&mut value)?;
        if value == original {
            return Ok(result);
        }
        let encoded = serde_json::to_vec(&value)?;
        ensure!(
            encoded.len() <= MAX_FILE_BYTES,
            "room state JSON exceeds the byte limit"
        );
        self.root.replace(STATE_FILE, &encoded, 0o600)?;
        Ok(result)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum PermissionFiles {
    /// General configuration must preserve game-written permission lists.
    #[default]
    Preserve,
    /// The caller explicitly requested these replacements with all games stopped.
    ReplaceOffline,
}

/// Mutably borrowing the room prevents the owner from accessing the same guard
/// while an offline configuration operation is in progress.
pub struct StoppedRoom<'a> {
    room: &'a mut RoomLock,
}

impl StoppedRoom<'_> {
    pub fn directory(&self) -> &Path {
        self.room.directory()
    }

    pub fn read_text(&self, relative: impl AsRef<Path>) -> Result<String> {
        self.room.read_text(relative)
    }

    pub fn read_optional_text(&self, relative: impl AsRef<Path>) -> Result<Option<String>> {
        self.room.read_optional_text(relative)
    }

    pub fn read_optional_bytes(&self, relative: impl AsRef<Path>) -> Result<Option<Vec<u8>>> {
        self.room.read_optional_bytes(relative)
    }

    pub(crate) fn replace_permission(&mut self, name: &str, contents: &[u8]) -> Result<()> {
        self.room.replace_permission(name, contents)
    }

    pub fn read_ini(&self, relative: impl AsRef<Path>) -> Result<IniDocument> {
        self.room.read_ini(relative)
    }

    pub fn shard_directories(&self) -> Result<Vec<String>> {
        self.room.shard_directories()
    }

    /// Replace files in order, stopping at the first I/O error.
    /// Earlier writes remain applied; reapply the configuration after fixing the error.
    pub fn write_files(
        &mut self,
        files: FileChanges,
        removed: Vec<PathBuf>,
        permissions: PermissionFiles,
    ) -> Result<Vec<PathBuf>> {
        validate_changes(&files, &removed, permissions)?;
        for path in files.keys().chain(&removed) {
            self.room.root.check_target(path)?;
        }
        let mut written = Vec::with_capacity(files.len() + removed.len());
        for (path, contents) in files {
            let (parent, name) = self.room.root.parent(&path, true)?;
            let private = ["cluster.ini", "cluster_token.txt", "server.ini"]
                .contains(&name.as_str())
                || PERMISSION_FILES.contains(&name.as_str());
            parent
                .replace(
                    &name,
                    contents.as_bytes(),
                    if private { 0o600 } else { 0o644 },
                )
                .with_context(|| format!("write configuration {}", path.display()))?;
            written.push(path);
        }
        for path in removed {
            if let Some((parent, name)) = self.room.root.existing_parent(&path)? {
                parent.remove_regular_if_present(&name)?;
                parent.sync()?;
            }
            written.push(path);
        }
        Ok(written)
    }
}

fn validate_changes(
    files: &FileChanges,
    removed: &[PathBuf],
    permissions: PermissionFiles,
) -> Result<()> {
    ensure!(
        files.len() + removed.len() <= MAX_CONFIG_FILES,
        "configuration update file count is outside its limit"
    );
    let mut paths: BTreeSet<_> = files.keys().cloned().collect();
    let mut bytes = 0_usize;
    for (path, contents) in files {
        let components = relative_components(path)?;
        ensure!(
            !components.iter().any(|name| name.starts_with(".dst-")),
            "configuration update cannot replace controller files"
        );
        ensure!(
            contents.len() <= MAX_FILE_BYTES,
            "configuration file exceeds the byte limit"
        );
        ensure!(
            !contents.contains('\0'),
            "configuration text cannot contain NUL"
        );
        bytes = bytes
            .checked_add(contents.len())
            .context("configuration update size overflow")?;
        ensure!(
            bytes <= MAX_CONFIG_BYTES,
            "configuration update exceeds the byte limit"
        );
        if components.len() == 1 && PERMISSION_FILES.contains(&components[0]) {
            ensure!(
                permissions == PermissionFiles::ReplaceOffline,
                "permission files require an explicit offline replacement"
            );
            ensure!(
                !contents.contains('\r'),
                "permission files cannot contain CR"
            );
        }
        if path.extension().is_some_and(|extension| extension == "ini") {
            configuration::Ini::parse(path, contents, ini_kind(path))?;
        }
    }
    for path in removed {
        let parts = relative_components(path)?;
        ensure!(
            parts.len() == 2
                && !parts[0].starts_with('.')
                && parts[0] != "mods"
                && [
                    "server.ini",
                    "worldgenoverride.lua",
                    "leveldataoverride.lua"
                ]
                .contains(&parts[1]),
            "only shard configuration files can be removed"
        );
        ensure!(
            paths.insert(path.clone()),
            "duplicate configuration update path"
        );
    }
    for path in &paths {
        ensure!(
            !path
                .ancestors()
                .skip(1)
                .any(|ancestor| paths.contains(ancestor)),
            "a configuration file cannot be another file's parent directory"
        );
    }
    Ok(())
}

pub fn render_ini(document: &IniDocument) -> Result<String> {
    let mut source = String::new();
    for (section, fields) in document {
        ensure!(identifier(section), "invalid INI section name");
        source.push_str(&format!("[{section}]\n"));
        for (key, value) in fields {
            ensure!(identifier(key), "invalid INI option name");
            ensure!(
                !value.contains(['\0', '\r', '\n']) && value.trim() == value,
                "INI value contains unsupported control or surrounding whitespace"
            );
            source.push_str(&format!("{key} = {value}\n"));
            ensure!(
                source.len() <= MAX_FILE_BYTES,
                "INI configuration exceeds the byte limit"
            );
        }
        source.push('\n');
    }
    configuration::Ini::parse(Path::new("configuration.ini"), &source, None)?;
    Ok(source)
}

pub fn render_lua_table(value: &Value) -> Result<String> {
    ensure!(value.is_object(), "Lua configuration must be an object");
    let source = format!("return {}\n", lua::render_literal(value)?);
    lua::parse_return_table(&source)?;
    Ok(source)
}

fn ini_kind(path: &Path) -> Option<bool> {
    match path.file_name().and_then(OsStr::to_str) {
        Some("cluster.ini") => Some(false),
        Some("server.ini") => Some(true),
        _ => None,
    }
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

fn relative_components(path: &Path) -> Result<Vec<&str>> {
    let text = path.to_str().context("configuration path must be UTF-8")?;
    ensure!(
        !text.is_empty() && text.len() <= 4096 && !text.contains(['\0', '\r', '\n', '\\']),
        "unsafe relative configuration path"
    );
    let parts: Vec<_> = text.split('/').collect();
    ensure!(
        parts.len() <= 16
            && parts
                .iter()
                .all(|part| !part.is_empty() && *part != "." && *part != ".." && part.len() <= 255),
        "unsafe relative configuration path"
    );
    Ok(parts)
}

/// Operations are relative to open directory descriptors. O_NOFOLLOW applies to
/// every traversed component, including existing targets and temporary files.
struct Directory(File);

/// Create a room path while refusing symlinks in every existing component.
pub fn create_directory(path: impl AsRef<Path>) -> Result<()> {
    Directory::open_with_creation(path.as_ref(), true).map(|_| ())
}

/// Write a new native tree once and publish it without replacing any existing path.
pub(crate) fn write_new_directory(
    destination: &Path,
    files: &BTreeMap<PathBuf, Vec<u8>>,
) -> Result<()> {
    let name = destination
        .file_name()
        .context("destination must name a new directory")?;
    let parent = Directory::open_with_creation(
        destination
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or(Path::new(".")),
        true,
    )?;
    let temporary = tempfile::Builder::new()
        .prefix(".dst-import-")
        .tempdir_in(format!("/proc/self/fd/{}", parent.0.as_raw_fd()))?;
    let temporary_name = temporary.path().file_name().unwrap();
    let root = parent.child_os(temporary_name, false, 0o700)?;
    for (path, bytes) in files {
        let (directory, filename) = root.parent(path, true)?;
        let mut file = directory.open_file(
            &filename,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            0o600,
        )?;
        file.write_all(bytes)
            .context("write imported native file")?;
        file.sync_all()?;
        directory.sync()?;
    }
    root.sync()?;
    let source = CString::new(temporary_name.as_bytes())?;
    let destination = CString::new(name.as_bytes())?;
    // SAFETY: both names are NUL terminated and parent owns the directory descriptor.
    if unsafe {
        libc::renameat2(
            parent.0.as_raw_fd(),
            source.as_ptr(),
            parent.0.as_raw_fd(),
            destination.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    } != 0
    {
        return Err(std::io::Error::last_os_error())
            .context("publish imported directory (must not already exist)");
    }
    parent.sync()?;
    Ok(())
}

pub fn directory_names(path: impl AsRef<Path>) -> Result<Vec<String>> {
    Directory::open(path.as_ref())?.directory_names(false)
}

impl Directory {
    fn open(path: &Path) -> Result<Self> {
        Self::open_with_creation(path, false)
    }

    fn open_with_creation(path: &Path, create: bool) -> Result<Self> {
        let absolute = if path.is_absolute() {
            path.to_owned()
        } else {
            std::env::current_dir()?.join(path)
        };
        let root = File::open("/").context("open filesystem root")?;
        let mut current = Self(root);
        for component in absolute.components() {
            match component {
                Component::RootDir | Component::CurDir => {}
                Component::Normal(name) => current = current.child_os(name, create, 0o755)?,
                _ => bail!("room directory cannot contain parent traversal"),
            }
        }
        Ok(current)
    }

    fn duplicate(&self) -> Result<Self> {
        Ok(Self(self.0.try_clone()?))
    }

    fn directory_names(&self, shards_only: bool) -> Result<Vec<String>> {
        let mut names = Vec::new();
        for (name, kind) in self.entries()? {
            if shards_only && (name.starts_with('.') || name == "mods") {
                continue;
            }
            ensure!(
                !kind.is_symlink(),
                "managed directory entries cannot be symlinks"
            );
            if kind.is_dir() {
                names.push(name);
            }
        }
        Ok(names)
    }

    fn entries(&self) -> Result<Vec<(String, std::fs::FileType)>> {
        // procfs resolves our owned descriptor, not an untrusted filesystem path.
        let mut entries = Vec::new();
        for entry in std::fs::read_dir(format!("/proc/self/fd/{}", self.0.as_raw_fd()))? {
            let entry = entry?;
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| anyhow::anyhow!("directory name must be UTF-8"))?;
            let kind = entry.file_type()?;
            entries.push((name, kind));
        }
        entries.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(entries)
    }

    fn existing_parent(&self, relative: &Path) -> Result<Option<(Self, String)>> {
        let mut parts = relative_components(relative)?;
        let name = parts.pop().expect("validated path").to_owned();
        let mut directory = self.duplicate()?;
        for part in parts {
            if directory.kind(part)?.is_none() {
                return Ok(None);
            }
            directory = directory.child(part, false, 0o755)?;
        }
        Ok(Some((directory, name)))
    }

    fn child(&self, name: &str, create: bool, mode: libc::mode_t) -> Result<Self> {
        self.child_os(OsStr::new(name), create, mode)
    }

    fn child_os(&self, name: &OsStr, create: bool, mode: libc::mode_t) -> Result<Self> {
        let name = CString::new(name.as_bytes()).context("directory name contains NUL")?;
        let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
        // SAFETY: name is NUL-terminated and self owns a directory descriptor.
        let mut descriptor = unsafe { libc::openat(self.0.as_raw_fd(), name.as_ptr(), flags) };
        if descriptor < 0
            && std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound
            && create
        {
            // SAFETY: same descriptor and name validity as openat above.
            if unsafe { libc::mkdirat(self.0.as_raw_fd(), name.as_ptr(), mode) } != 0 {
                return Err(std::io::Error::last_os_error())
                    .context("create configuration directory");
            }
            self.sync()?;
            // SAFETY: same descriptor and name validity as openat above.
            descriptor = unsafe { libc::openat(self.0.as_raw_fd(), name.as_ptr(), flags) };
        }
        if descriptor < 0 {
            return Err(std::io::Error::last_os_error())
                .context("cannot open configuration directory without following symlinks");
        }
        // SAFETY: successful openat returns a newly owned descriptor.
        Ok(Self(unsafe { File::from_raw_fd(descriptor) }))
    }

    fn parent(&self, relative: &Path, create: bool) -> Result<(Self, String)> {
        let mut parts = relative_components(relative)?;
        let name = parts.pop().expect("validated non-empty path").to_owned();
        let mut directory = self.duplicate()?;
        for part in parts {
            directory = directory.child(part, create, 0o755)?;
        }
        Ok((directory, name))
    }

    fn check_target(&self, relative: &Path) -> Result<()> {
        let mut parts = relative_components(relative)?;
        let name = parts.pop().expect("validated non-empty path");
        let mut directory = self.duplicate()?;
        for part in parts {
            match directory.kind(part)? {
                None => return Ok(()),
                Some(kind) => ensure!(
                    kind == libc::S_IFDIR,
                    "configuration parent must be a directory without symlinks"
                ),
            }
            directory = directory.child(part, false, 0o755)?;
        }
        directory.require_regular_or_absent(name)
    }

    fn kind(&self, name: &str) -> Result<Option<libc::mode_t>> {
        let name = CString::new(name).context("file name contains NUL")?;
        let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
        // SAFETY: stat writes to the provided properly sized output buffer.
        let result = unsafe {
            libc::fstatat(
                self.0.as_raw_fd(),
                name.as_ptr(),
                metadata.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if result < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::NotFound {
                return Ok(None);
            }
            return Err(error).context("inspect configuration path");
        }
        // SAFETY: fstatat initialized the output on success.
        Ok(Some(
            unsafe { metadata.assume_init() }.st_mode & libc::S_IFMT,
        ))
    }

    fn require_regular_or_absent(&self, name: &str) -> Result<()> {
        if let Some(kind) = self.kind(name)? {
            ensure!(
                kind == libc::S_IFREG,
                "configuration target must be a regular file without symlinks"
            );
        }
        Ok(())
    }

    fn open_file(&self, name: &str, flags: libc::c_int, mode: libc::mode_t) -> Result<File> {
        self.require_regular_or_absent(name)?;
        let name = CString::new(name).context("file name contains NUL")?;
        // SAFETY: the descriptor, name and flags are valid; mode is provided for O_CREAT.
        let descriptor = unsafe {
            libc::openat(
                self.0.as_raw_fd(),
                name.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
                mode,
            )
        };
        if descriptor < 0 {
            return Err(std::io::Error::last_os_error())
                .context("cannot open configuration file without following symlinks");
        }
        // SAFETY: successful openat returns a newly owned descriptor.
        let file = unsafe { File::from_raw_fd(descriptor) };
        ensure!(
            file.metadata()?.is_file(),
            "configuration must be a regular file"
        );
        Ok(file)
    }

    fn read_bytes(&self, name: &str, limit: usize) -> Result<Vec<u8>> {
        let file = self.open_file(name, libc::O_RDONLY, 0)?;
        ensure!(
            file.metadata()?.len() <= limit as u64,
            "configuration file exceeds the byte limit"
        );
        let mut source = Vec::new();
        file.take(limit as u64 + 1)
            .read_to_end(&mut source)
            .context("read native configuration bytes")?;
        ensure!(
            source.len() <= limit,
            "configuration file exceeds the byte limit"
        );
        Ok(source)
    }

    fn read_text(&self, name: &str, limit: usize) -> Result<String> {
        String::from_utf8(self.read_bytes(name, limit)?).context("read UTF-8 configuration text")
    }

    fn replace(&self, name: &str, contents: &[u8], mode: u32) -> Result<()> {
        self.require_regular_or_absent(name)?;
        self.remove_regular_if_present(WRITE_TEMPORARY)?;
        let mut temporary = self.open_file(
            WRITE_TEMPORARY,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            0o600,
        )?;
        temporary
            .write_all(contents)
            .context("write configuration replacement")?;
        temporary.set_permissions(std::fs::Permissions::from_mode(mode))?;
        temporary
            .sync_all()
            .context("sync configuration replacement")?;
        self.require_regular_or_absent(name)?;
        self.rename(WRITE_TEMPORARY, name)?;
        self.sync()
    }

    fn rename(&self, from: &str, to: &str) -> Result<()> {
        let from = CString::new(from)?;
        let to = CString::new(to)?;
        // SAFETY: both names and directory descriptors remain live for the call.
        if unsafe {
            libc::renameat(
                self.0.as_raw_fd(),
                from.as_ptr(),
                self.0.as_raw_fd(),
                to.as_ptr(),
            )
        } != 0
        {
            return Err(std::io::Error::last_os_error())
                .context("publish configuration replacement");
        }
        Ok(())
    }

    fn remove_regular_if_present(&self, name: &str) -> Result<()> {
        if self.kind(name)?.is_none() {
            return Ok(());
        }
        self.require_regular_or_absent(name)?;
        let name = CString::new(name)?;
        // SAFETY: name and the owned directory descriptor are valid.
        if unsafe { libc::unlinkat(self.0.as_raw_fd(), name.as_ptr(), 0) } != 0 {
            return Err(std::io::Error::last_os_error())
                .context("remove configuration staging file");
        }
        Ok(())
    }

    fn sync(&self) -> Result<()> {
        self.0.sync_all().context("sync configuration directory")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::{net::UnixStream, process::CommandExt};
    use std::process::Command;
    use std::time::Duration;

    fn paused_before_exec(
        mut action: impl FnMut() -> std::io::Result<()> + Send + Sync + 'static,
    ) -> (
        UnixStream,
        std::thread::JoinHandle<std::process::ExitStatus>,
    ) {
        let (mut parent, child) = UnixStream::pair().unwrap();
        parent
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let parent_fd = parent.as_raw_fd();
        let launched = std::thread::spawn(move || {
            let mut command = Command::new("/bin/true");
            // SAFETY: these test actions and callbacks use only async-signal-safe
            // descriptor calls, with no allocation or inherited mutex access.
            unsafe {
                command.pre_exec(move || {
                    libc::close(parent_fd);
                    action()?;
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
        (parent, launched)
    }

    #[test]
    fn dropping_room_lock_releases_it_before_an_unrelated_child_executes() {
        let root = tempfile::tempdir().unwrap();
        let lock = RoomLock::try_acquire(root.path()).unwrap();
        // CLOEXEC closes the child's copy only after exec, not when fork returns.
        // SAFETY: the lock owns the queried descriptor; F_GETFD has no side effects.
        assert_ne!(
            unsafe { libc::fcntl(lock._lock.as_raw_fd(), libc::F_GETFD) } & libc::FD_CLOEXEC,
            0
        );
        let (mut parent, launched) = paused_before_exec(|| Ok(()));
        drop(lock);
        let reacquired = RoomLock::try_acquire(root.path());
        parent.write_all(&[1]).unwrap();
        assert!(launched.join().unwrap().success());
        assert!(reacquired.is_ok(), "{:#}", reacquired.err().unwrap());
    }

    #[test]
    fn dropping_an_inherited_room_lock_cannot_unlock_its_parent() {
        let root = tempfile::tempdir().unwrap();
        let lock = RoomLock::try_acquire(root.path()).unwrap();
        let owner_pid = lock.owner_pid;
        let directory_fd = lock.root.0.as_raw_fd();
        let lock_fd = lock._lock.as_raw_fd();
        let (mut parent, launched) = paused_before_exec(move || {
            // Reconstruct the inherited guard with an empty path so Drop performs
            // no allocation/deallocation in the child. dup, getpid, flock and close
            // are async-signal-safe and refer to the same inherited lock description.
            // SAFETY: the parent retains both descriptors throughout this callback.
            let directory_fd = unsafe { libc::dup(directory_fd) };
            if directory_fd < 0 {
                return Err(std::io::Error::last_os_error());
            }
            // SAFETY: dup returned a newly owned descriptor.
            let directory = unsafe { File::from_raw_fd(directory_fd) };
            // SAFETY: the parent retains the source lock descriptor.
            let lock_fd = unsafe { libc::dup(lock_fd) };
            if lock_fd < 0 {
                return Err(std::io::Error::last_os_error());
            }
            drop(RoomLock {
                reader: RoomReader {
                    directory: PathBuf::new(),
                    root: Directory(directory),
                },
                // SAFETY: dup returned a newly owned descriptor.
                _lock: unsafe { File::from_raw_fd(lock_fd) },
                owner_pid,
            });
            Ok(())
        });
        let second = RoomLock::try_acquire(root.path());
        parent.write_all(&[1]).unwrap();
        assert!(launched.join().unwrap().success());
        assert!(second.is_err(), "child Drop released its parent's lock");
        drop(lock);
        assert!(RoomLock::try_acquire(root.path()).is_ok());
    }
    use serde_json::json;
    use std::fs;
    use std::os::unix::fs::{MetadataExt, symlink};

    fn changes() -> FileChanges {
        BTreeMap::from([
            (
                PathBuf::from("cluster.ini"),
                "[GAMEPLAY]\nmax_players = 12\n".to_owned(),
            ),
            (
                PathBuf::from("A/server.ini"),
                "[SHARD]\nis_master = true\n".to_owned(),
            ),
            (
                PathBuf::from("B/modoverrides.lua"),
                "return {enabled=true}\n".to_owned(),
            ),
        ])
    }

    #[test]
    fn native_data_reads_and_offline_writes_preserve_permissions() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("adminlist.txt"), "KU_new_game_write\n").unwrap();
        let mut lock = RoomLock::try_acquire(root.path()).unwrap();
        let error = RoomLock::try_acquire(root.path())
            .err()
            .unwrap()
            .downcast::<std::io::Error>()
            .unwrap();
        assert_eq!(error.kind(), std::io::ErrorKind::WouldBlock);
        assert_eq!(error.raw_os_error(), Some(libc::EWOULDBLOCK));
        let written = lock
            .while_stopped()
            .write_files(changes(), Vec::new(), PermissionFiles::Preserve)
            .unwrap();
        assert_eq!(written.len(), 3);
        assert_eq!(
            lock.read_ini("cluster.ini").unwrap()["gameplay"]["max_players"],
            "12"
        );
        assert_eq!(
            lock.read_lua_table("B/modoverrides.lua").unwrap(),
            json!({"enabled": true})
        );
        assert_eq!(
            lock.read_text("adminlist.txt").unwrap(),
            "KU_new_game_write\n"
        );
        assert_eq!(
            fs::metadata(root.path().join("A/server.ini"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        let ini = lock.read_ini("cluster.ini").unwrap();
        assert_eq!(
            configuration::Ini::parse(Path::new("cluster.ini"), &render_ini(&ini).unwrap(), None)
                .unwrap()
                .into_sections(),
            ini
        );
        assert_eq!(
            lua::parse_return_table(
                &render_lua_table(&json!({"value": [false, 4, "玩家"]})).unwrap()
            )
            .unwrap(),
            json!({"value": [false, 4, "玩家"]})
        );
        fs::write(
            root.path().join("B/modoverrides.lua"),
            "return dynamic_settings()",
        )
        .unwrap();
        assert!(lock.read_lua_table("B/modoverrides.lua").is_err());
        drop(lock);
        assert!(RoomLock::try_acquire(root.path()).is_ok());
    }

    #[test]
    fn deactivation_removes_only_configuration_and_rejects_save_deletion() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir_all(root.path().join("Caves/save")).unwrap();
        fs::write(
            root.path().join("Caves/server.ini"),
            "[SHARD]\nis_master=false\n",
        )
        .unwrap();
        fs::write(root.path().join("Caves/save/world"), "saved world").unwrap();
        let mut lock = RoomLock::try_acquire(root.path()).unwrap();
        assert_eq!(
            lock.while_stopped()
                .write_files(
                    FileChanges::new(),
                    vec![PathBuf::from("Caves/server.ini")],
                    PermissionFiles::Preserve,
                )
                .unwrap(),
            vec![PathBuf::from("Caves/server.ini")]
        );
        assert!(!root.path().join("Caves/server.ini").exists());
        assert_eq!(
            fs::read_to_string(root.path().join("Caves/save/world")).unwrap(),
            "saved world"
        );
        assert!(
            lock.while_stopped()
                .write_files(
                    FileChanges::new(),
                    vec![PathBuf::from("Caves/save/world")],
                    PermissionFiles::Preserve
                )
                .is_err()
        );
        assert!(lock.read_optional_text("missing/child").unwrap().is_none());
        assert_eq!(lock.shard_directories().unwrap(), vec!["Caves"]);
    }

    #[test]
    fn invalid_input_and_permission_replacement_fail_before_writing() {
        let root = tempfile::tempdir().unwrap();
        let mut lock = RoomLock::try_acquire(root.path()).unwrap();
        for path in [
            "../outside",
            "/outside",
            "a/../outside",
            "a//b",
            "a/./b",
            ".dst-room.lock",
            ".dst-server.json",
            "adminlist.txt",
        ] {
            assert!(
                lock.while_stopped()
                    .write_files(
                        BTreeMap::from([(PathBuf::from(path), "value".to_owned())]),
                        Vec::new(),
                        PermissionFiles::Preserve
                    )
                    .is_err(),
                "{path}"
            );
        }
        let permissions =
            BTreeMap::from([(PathBuf::from("adminlist.txt"), "KU_explicit\n".to_owned())]);
        lock.while_stopped()
            .write_files(permissions, Vec::new(), PermissionFiles::ReplaceOffline)
            .unwrap();
        assert_eq!(lock.read_text("adminlist.txt").unwrap(), "KU_explicit\n");
        assert!(
            lock.while_stopped()
                .write_files(
                    BTreeMap::from([(
                        PathBuf::from("oversized.lua"),
                        "x".repeat(MAX_FILE_BYTES + 1)
                    )]),
                    Vec::new(),
                    PermissionFiles::Preserve
                )
                .is_err()
        );
        assert!(
            lock.while_stopped()
                .write_files(
                    BTreeMap::from([
                        (PathBuf::from("a"), "file".to_owned()),
                        (PathBuf::from("a/b"), "child".to_owned())
                    ]),
                    Vec::new(),
                    PermissionFiles::Preserve
                )
                .is_err()
        );
    }

    #[test]
    fn symlinks_are_rejected_during_read_and_write() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join("target"), "outside").unwrap();
        symlink(outside.path(), root.path().join("linked")).unwrap();
        symlink(outside.path().join("target"), root.path().join("file-link")).unwrap();
        let mut lock = RoomLock::try_acquire(root.path()).unwrap();
        assert!(lock.read_text("linked/target").is_err());
        assert!(lock.read_text("file-link").is_err());
        for path in ["linked/target", "file-link"] {
            assert!(
                lock.while_stopped()
                    .write_files(
                        BTreeMap::from([(PathBuf::from(path), "changed".to_owned())]),
                        Vec::new(),
                        PermissionFiles::Preserve
                    )
                    .is_err()
            );
        }
        assert_eq!(
            fs::read_to_string(outside.path().join("target")).unwrap(),
            "outside"
        );
        let links = tempfile::tempdir().unwrap();
        symlink(root.path(), links.path().join("room")).unwrap();
        assert!(RoomLock::try_acquire(links.path().join("room")).is_err());
    }

    #[test]
    fn state_migration_preserves_recovery_and_rejects_conflicting_files() {
        let root = tempfile::tempdir().unwrap();
        let legacy = root.path().join(".dst-control.json");
        let current = root.path().join(STATE_FILE);
        let state = r#"{"policy":{"idle_regeneration":false},"recovery":{"restarts_used":2}}"#;
        fs::write(&legacy, state).unwrap();
        let mut room = RoomLock::try_acquire(root.path()).unwrap();
        assert!(!legacy.exists());
        assert_eq!(fs::read_to_string(&current).unwrap(), state);
        let shard = root.path().join("Caves");
        fs::create_dir(&shard).unwrap();
        fs::write(shard.join("server.ini"), "[SHARD]\nis_master=false\n").unwrap();
        fs::write(shard.join("dst_server_driver.json"), "old launch").unwrap();
        crate::driver::DriverOptions::default()
            .save(&mut room)
            .unwrap();
        assert_eq!(room.read_state().unwrap()["recovery"]["restarts_used"], 2);
        assert_eq!(
            room.read_state().unwrap()["policy"]["idle_regeneration"],
            false
        );
        let launch: Value =
            serde_json::from_slice(&fs::read(shard.join("dst_server_driver.json")).unwrap())
                .unwrap();
        assert_eq!(launch["driver"], room.read_state().unwrap()["driver"]);
        assert!(launch.get("recovery").is_none());
        drop(room);
        fs::write(&legacy, "second state").unwrap();
        assert!(RoomLock::try_acquire(root.path()).is_err());
        assert_eq!(fs::read_to_string(&legacy).unwrap(), "second state");
        assert!(
            fs::read_to_string(&current)
                .unwrap()
                .contains("restarts_used")
        );
        fs::remove_file(&legacy).unwrap();
        fs::remove_file(&current).unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        symlink(outside.path(), &legacy).unwrap();
        assert!(RoomLock::try_acquire(root.path()).is_err());
        assert!(!current.exists());
    }

    #[test]
    fn state_updates_preserve_other_fields_and_report_bad_data() {
        let root = tempfile::tempdir().unwrap();
        let mut lock = RoomLock::try_acquire(root.path()).unwrap();
        lock.update_state(|control| {
            control.insert("policies".into(), json!({"open": true}));
            Ok(())
        })
        .unwrap();
        lock.update_state(|control| {
            control.insert("recovery".into(), json!({"attempts": 1}));
            Ok(())
        })
        .unwrap();
        assert_eq!(
            Value::Object(lock.read_state().unwrap()),
            json!({"policies": {"open": true}, "recovery": {"attempts": 1}})
        );
        let inode = fs::metadata(root.path().join(STATE_FILE)).unwrap().ino();
        lock.update_state(|_| Ok(())).unwrap();
        assert_eq!(
            fs::metadata(root.path().join(STATE_FILE)).unwrap().ino(),
            inode
        );
        fs::write(root.path().join(STATE_FILE), "{broken PRIVATE_SECRET").unwrap();
        let error = lock.read_state().unwrap_err().to_string();
        assert!(!error.contains("PRIVATE_SECRET"));
        assert!(lock.update_state(|_| Ok(())).is_err());
        assert_eq!(
            fs::read_to_string(root.path().join(STATE_FILE)).unwrap(),
            "{broken PRIVATE_SECRET"
        );
    }
}
