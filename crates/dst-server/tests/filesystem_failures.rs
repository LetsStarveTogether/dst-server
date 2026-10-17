//! Real write failures through the production configuration API.
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

fn failed_partial_update(root: &Path, expected: i32) -> u64 {
    setup(root);
    let mut lock = RoomLock::try_acquire(root).unwrap();
    let error = lock
        .while_stopped()
        .write_files(
            changes(4096, 128 * 1024),
            Vec::new(),
            PermissionFiles::Preserve,
        )
        .unwrap_err();
    assert_errno(&error, expected);
    let partial_bytes = fs::metadata(root.join("Master/.dst-config-write.tmp"))
        .unwrap()
        .len();
    assert!(partial_bytes > 0 && partial_bytes < 128 * 1024);
    assert!(!root.join(".dst-config-transaction").exists());
    assert_eq!(fs::read_to_string(root.join(FIRST)).unwrap(), lua(4096));
    assert_eq!(fs::read_to_string(root.join(SECOND)).unwrap(), OLD);
    preserved(root);
    drop(lock);
    // Reopening the room does not complete a partial configuration update.
    let lock = RoomLock::try_acquire(root).unwrap();
    assert_eq!(lock.read_text(FIRST).unwrap(), lua(4096));
    assert_eq!(lock.read_text(SECOND).unwrap(), OLD);
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
    let partial_bytes = failed_partial_update(root.path(), libc::EFBIG);
    assert_eq!(partial_bytes, 4096);
    let mut lock = RoomLock::try_acquire(root.path()).unwrap();
    assert_eq!(
        lock.while_stopped()
            .write_files(changes(128, 128), Vec::new(), PermissionFiles::Preserve)
            .unwrap()
            .len(),
        2
    );
    preserved(root.path());
    println!(
        "FILESYSTEM_FAILURE_REPORT {}",
        json!({"case":"partial_update_efbig", "errno":libc::EFBIG, "partial_bytes":partial_bytes, "reapplied":true})
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

    let root = tempfile::tempdir_in(&mount).unwrap();
    let partial_bytes = failed_partial_update(root.path(), libc::ENOSPC);
    let mut lock = RoomLock::try_acquire(root.path()).unwrap();
    let intended = changes(128, 128);
    assert_eq!(
        lock.while_stopped()
            .write_files(intended.clone(), Vec::new(), PermissionFiles::Preserve)
            .unwrap()
            .len(),
        2
    );
    for (path, contents) in intended {
        assert_eq!(lock.read_text(path).unwrap(), contents);
    }
    assert!(!root.path().join("Master/.dst-config-write.tmp").exists());
    preserved(root.path());
    println!(
        "FILESYSTEM_FAILURE_REPORT {}",
        json!({
            "case":"tmpfs_enospc", "errno":libc::ENOSPC, "capacity_bytes":64 * 1024,
            "partial_bytes":partial_bytes, "first_replacement_applied":true,
            "second_replacement_kept_original":true, "reapplied":true,
            "saved_game_and_permissions_preserved":true
        })
    );
}
