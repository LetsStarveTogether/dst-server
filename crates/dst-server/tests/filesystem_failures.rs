//! Real write failures through the production transaction API.
//!
//! The ENOSPC worker runs explicitly inside a disposable 64 KiB tmpfs container.
//! The ordinary test uses a child process with RLIMIT_FSIZE, affecting no sibling.

use std::{
    ffi::CString,
    fs,
    os::unix::ffi::OsStrExt,
    path::{Path, PathBuf},
    process::Command,
};

use dst_server::files::{FileChanges, PermissionFiles, RoomLock};
use serde_json::json;

const FIRST: &str = "Master/modoverrides.lua";
const SECOND: &str = "Master/worldgenoverride.lua";
const SAVE: &str = "Master/save/session/KEEP";
const OLD: &str = "return {old = true}\n";

fn setup(root: &Path) {
    fs::create_dir_all(root.join("Master/save/session")).unwrap();
    fs::write(root.join(FIRST), OLD).unwrap();
    fs::write(root.join(SECOND), OLD).unwrap();
    fs::write(root.join("adminlist.txt"), "KU_KEEP\n").unwrap();
    fs::write(root.join(SAVE), b"save-must-survive\0\xff").unwrap();
}

fn lua(size: usize) -> String {
    let prefix = "return {value = \"";
    let suffix = "\"}\n";
    format!(
        "{prefix}{}{suffix}",
        "a".repeat(size - prefix.len() - suffix.len())
    )
}

fn changes(first: usize, second: usize) -> FileChanges {
    [
        (PathBuf::from(FIRST), lua(first)),
        (PathBuf::from(SECOND), lua(second)),
    ]
    .into_iter()
    .collect()
}

fn preserved(root: &Path) {
    assert_eq!(fs::read(root.join("adminlist.txt")).unwrap(), b"KU_KEEP\n");
    assert_eq!(
        fs::read(root.join(SAVE)).unwrap(),
        b"save-must-survive\0\xff"
    );
}

fn assert_errno(error: &anyhow::Error, expected: i32) {
    let actual = error.chain().find_map(|error| {
        error
            .downcast_ref::<std::io::Error>()
            .and_then(std::io::Error::raw_os_error)
    });
    assert_eq!(actual, Some(expected), "{error:#}");
}

fn failed_before_publication(root: &Path, expected: i32) -> u64 {
    setup(root);
    let mut lock = RoomLock::try_acquire(root).unwrap();
    let error = lock
        .while_stopped()
        .commit(changes(4096, 128 * 1024), PermissionFiles::Preserve)
        .unwrap_err();
    assert_errno(&error, expected);
    let staging = root.join(".dst-config-transaction");
    let partial_bytes = fs::metadata(staging.join(".manifest.tmp")).unwrap().len();
    assert!(partial_bytes > 0 && partial_bytes < 128 * 1024);
    assert!(!staging.join("manifest.json").exists());
    assert_eq!(fs::read_to_string(root.join(FIRST)).unwrap(), OLD);
    assert_eq!(fs::read_to_string(root.join(SECOND)).unwrap(), OLD);
    preserved(root);
    drop(lock);
    // Recovery can remove an unpublished partial manifest even on a full disk.
    let mut lock = RoomLock::try_acquire(root).unwrap();
    assert!(lock.while_stopped().recover().unwrap().is_empty());
    assert!(!staging.exists());
    preserved(root);
    partial_bytes
}

#[test]
fn actual_partial_writes_fail_with_efbig_in_an_isolated_child() {
    let result = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "file_size_worker", "--ignored", "--nocapture"])
        .env("DST_FILE_SIZE_WORKER", "1")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains("\"errno\":27"));
}

#[test]
#[ignore = "run only through the isolated child test"]
fn file_size_worker() {
    assert_eq!(std::env::var("DST_FILE_SIZE_WORKER").as_deref(), Ok("1"));
    let root = tempfile::tempdir().unwrap();
    let mut limit = std::mem::MaybeUninit::<libc::rlimit>::uninit();
    // SAFETY: getrlimit receives writable storage for its complete output.
    assert_eq!(
        unsafe { libc::getrlimit(libc::RLIMIT_FSIZE, limit.as_mut_ptr()) },
        0
    );
    // SAFETY: successful getrlimit initialized the complete structure.
    let mut limit = unsafe { limit.assume_init() };
    assert!(limit.rlim_max >= 4096);
    limit.rlim_cur = 4096;
    // SAFETY: only this disposable child process changes its signal disposition
    // and file-size limit; stdout/stderr are pipes and the test writes temp files.
    unsafe {
        assert_ne!(libc::signal(libc::SIGXFSZ, libc::SIG_IGN), libc::SIG_ERR);
        assert_eq!(libc::setrlimit(libc::RLIMIT_FSIZE, &limit), 0);
    }
    let partial_bytes = failed_before_publication(root.path(), libc::EFBIG);
    assert_eq!(partial_bytes, 4096);
    let mut lock = RoomLock::try_acquire(root.path()).unwrap();
    assert_eq!(
        lock.while_stopped()
            .commit(changes(128, 128), PermissionFiles::Preserve)
            .unwrap()
            .len(),
        2
    );
    preserved(root.path());
    println!(
        "FILESYSTEM_FAILURE_REPORT {}",
        json!({"case":"partial_manifest_efbig", "errno":libc::EFBIG, "partial_bytes":partial_bytes, "recovered":true})
    );
}

#[test]
#[ignore = "requires a new container with DST_TEST_TMPFS on a 64 KiB tmpfs"]
fn tmpfs_enospc_worker() {
    let mount = PathBuf::from(std::env::var_os("DST_TEST_TMPFS").expect("DST_TEST_TMPFS"));
    let name = CString::new(mount.as_os_str().as_bytes()).unwrap();
    let mut filesystem = std::mem::MaybeUninit::<libc::statfs>::uninit();
    // SAFETY: statfs receives a terminated path and writable output storage.
    assert_eq!(
        unsafe { libc::statfs(name.as_ptr(), filesystem.as_mut_ptr()) },
        0
    );
    // SAFETY: successful statfs initialized the complete structure.
    let filesystem = unsafe { filesystem.assume_init() };
    assert_eq!(filesystem.f_type, libc::TMPFS_MAGIC);
    assert_eq!(filesystem.f_blocks * filesystem.f_bsize as u64, 64 * 1024);

    let before_publication = tempfile::tempdir_in(&mount).unwrap();
    let manifest_partial_bytes = failed_before_publication(before_publication.path(), libc::ENOSPC);
    drop(before_publication);

    let root = tempfile::tempdir_in(&mount).unwrap();
    setup(root.path());
    // Leave enough room for the complete manifest and first atomic replacement;
    // the second replacement then performs a real short write before ENOSPC.
    fs::write(root.path().join("ballast"), vec![0; 24 * 1024]).unwrap();
    let intended = changes(4096, 12 * 1024);
    let mut lock = RoomLock::try_acquire(root.path()).unwrap();
    let error = lock
        .while_stopped()
        .commit(intended.clone(), PermissionFiles::Preserve)
        .unwrap_err();
    assert_errno(&error, libc::ENOSPC);
    assert!(
        root.path()
            .join(".dst-config-transaction/manifest.json")
            .is_file()
    );
    assert_eq!(lock.read_text(FIRST).unwrap(), intended[Path::new(FIRST)]);
    assert_eq!(lock.read_text(SECOND).unwrap(), OLD);
    let replacement_partial_bytes = fs::metadata(root.path().join("Master/.dst-config-write.tmp"))
        .unwrap()
        .len();
    assert!(replacement_partial_bytes > 0 && replacement_partial_bytes < 12 * 1024);
    preserved(root.path());
    drop(lock);

    // Restarted ownership still cannot roll forward until actual capacity returns.
    let mut lock = RoomLock::try_acquire(root.path()).unwrap();
    assert_errno(&lock.while_stopped().recover().unwrap_err(), libc::ENOSPC);
    assert!(
        root.path()
            .join(".dst-config-transaction/manifest.json")
            .is_file()
    );
    fs::remove_file(root.path().join("ballast")).unwrap();
    assert_eq!(lock.while_stopped().recover().unwrap().len(), 2);
    for (path, contents) in intended {
        assert_eq!(lock.read_text(path).unwrap(), contents);
    }
    assert!(lock.while_stopped().recover().unwrap().is_empty());
    assert!(!root.path().join(".dst-config-transaction").exists());
    assert!(!root.path().join("Master/.dst-config-write.tmp").exists());
    preserved(root.path());
    println!(
        "FILESYSTEM_FAILURE_REPORT {}",
        json!({
            "case":"tmpfs_enospc", "errno":libc::ENOSPC, "capacity_bytes":64 * 1024,
            "manifest_partial_bytes":manifest_partial_bytes, "replacement_partial_bytes":replacement_partial_bytes,
            "first_replacement_committed":true, "second_replacement_kept_original":true,
            "retry_while_full_failed":true, "recovered_after_space_released":true,
            "saved_game_and_permissions_preserved":true
        })
    );
}
