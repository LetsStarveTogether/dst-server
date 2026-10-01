use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    time::Duration,
};

use dst_server::{
    files::RoomLock,
    model::ErrorCode,
    mods::{
        PreparedMods, UPDATE_COMPLETE, download_environment, prepare_shared, scan_setup,
        update_native,
    },
    rpc::EventHub,
};
use serde_json::{Value, json};

fn fixture(directory: &Path, body: &str) -> (PathBuf, PathBuf) {
    let executable = directory.join("install/bin64/updater");
    let ugc = directory.join("room/mods/ugc");
    fs::create_dir_all(executable.parent().unwrap()).unwrap();
    fs::create_dir_all(&ugc).unwrap();
    fs::write(
        &executable,
        format!(
            r#"#!/usr/bin/python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
ugc = Path(sys.argv[sys.argv.index('-ugc_directory') + 1])
(ugc / 'pid').write_text(str(os.getpid()))
counter = ugc / 'attempts'
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
(ugc / 'partial').write_text('retained download')
{body}
"#
        ),
    )
    .unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    (executable, ugc)
}

fn output(lines: &[&str], status: i32) -> String {
    format!(
        "print({}, flush=True)\nsys.exit({status})",
        serde_json::to_string(&lines.join("\n")).unwrap()
    )
}

#[test]
fn scan_keeps_dynamic_setup_and_reads_only_top_level_literal_declarations() {
    let setup = scan_setup(
        r#"
-- ServerModSetup("100")
ServerModSetup(""); ServerModSetup('42'); ServerModSetup([[7]])
local id = "99"; ServerModSetup(id)
if false then ServerModSetup("999") end
ServerModSetup("4" .. "2"); OtherSetup("700"); ServerModSetup("42", "43")
ServerModSetup("４２")
return ServerModSetup("8"), ServerModCollectionSetup("99")
"#,
    )
    .unwrap();
    assert_eq!(setup.items, BTreeSet::from([7, 8, 42]));
    assert_eq!(setup.collections, BTreeSet::from([99]));
    assert!(setup.has_code);
    assert!(!scan_setup("-- ServerModSetup('42')\r\n").unwrap().has_code);
    assert!(
        scan_setup("local id = '42'; ServerModSetup(id)")
            .unwrap()
            .has_code
    );
    for source in [
        "ServerModSetup('0')",
        "ServerModSetup('01')",
        "ServerModSetup('1e3')",
        "ServerModSetup('18446744073709551616')",
        "ServerModCollectionSetup('')",
        "this is not Lua }",
    ] {
        assert!(scan_setup(source).is_err(), "{source}");
    }
    assert_eq!(
        scan_setup("ServerModSetup('18446744073709551615')")
            .unwrap()
            .items,
        BTreeSet::from([u64::MAX])
    );
    assert!(scan_setup(&format!("local x = {}true", "not ".repeat(1000))).is_err());
    assert!(scan_setup(&format!("local x = {}1", "f(1,2) ^ ".repeat(1000))).is_err());
    assert!(
        scan_setup(&format!(
            "{}local x = 1 {}",
            "if true then ".repeat(100),
            "end ".repeat(100)
        ))
        .is_err()
    );
}

#[test]
fn preparation_preserves_native_files_and_rejects_unsafe_paths_before_writing() {
    let directory = tempfile::tempdir().unwrap();
    let room = directory.path().join("room");
    fs::create_dir_all(room.join("mods")).unwrap();
    let mut lock = RoomLock::try_acquire(&room).unwrap();
    let source = "local id = '42'\r\nServerModSetup(id)\r\n";
    fs::write(room.join("mods/dedicated_server_mods_setup.lua"), source).unwrap();
    fs::write(room.join("mods/modsettings.lua"), "NativeModSetting()\n").unwrap();
    assert!(prepare_shared(&mut lock).unwrap().has_code);
    assert_eq!(
        fs::read_to_string(room.join("mods/dedicated_server_mods_setup.lua")).unwrap(),
        source
    );
    assert_eq!(
        fs::read_to_string(room.join("mods/modsettings.lua")).unwrap(),
        "NativeModSetting()\n"
    );
    assert!(room.join("mods/ugc").is_dir());
    fs::remove_dir(room.join("mods/ugc")).unwrap();
    fs::write(
        room.join("mods/dedicated_server_mods_setup.lua"),
        "this is not Lua }",
    )
    .unwrap();
    assert!(prepare_shared(&mut lock).is_err());
    assert!(!room.join("mods/ugc").exists());
    fs::remove_file(room.join("mods/dedicated_server_mods_setup.lua")).unwrap();
    let outside = directory.path().join("outside.lua");
    fs::write(&outside, "ServerModSetup('42')").unwrap();
    symlink(&outside, room.join("mods/dedicated_server_mods_setup.lua")).unwrap();
    assert!(prepare_shared(&mut lock).is_err());
    assert!(!room.join("mods/ugc").exists());
    assert_eq!(fs::read_to_string(outside).unwrap(), "ServerModSetup('42')");
}

#[tokio::test]
async fn prepared_dynamic_setup_uses_shared_mods_and_empty_setup_skips_download() {
    let directory = tempfile::tempdir().unwrap();
    let (executable, ugc) = fixture(directory.path(), &output(&[UPDATE_COMPLETE], 0));
    let room = directory.path().join("room");
    let mut lock = RoomLock::try_acquire(&room).unwrap();
    let prepared = PreparedMods::prepare(&mut lock, &executable, None).unwrap();
    assert_eq!(
        prepared.update(&EventHub::default()).await.unwrap()["updated"],
        false
    );
    assert!(!ugc.join("attempts").exists());
    fs::write(
        room.join("mods/dedicated_server_mods_setup.lua"),
        "local id = '42'; ServerModSetup(id)",
    )
    .unwrap();
    assert!(PreparedMods::prepare(&mut lock, &executable, None).is_err());
    symlink(room.join("mods"), directory.path().join("install/mods")).unwrap();
    let prepared = PreparedMods::prepare(&mut lock, &executable, None).unwrap();
    assert_eq!(
        prepared.update(&EventHub::default()).await.unwrap()["updated"],
        true
    );
    assert_eq!(fs::read_to_string(ugc.join("attempts")).unwrap(), "1");
}

#[tokio::test]
async fn updater_validates_completion_failure_precedence_and_retains_partial_downloads() {
    for (lines, status, error) in [
        (vec![UPDATE_COMPLETE], 0, None),
        (
            vec![
                "[00:00:31]: FinishDownloadingServerMods Complete! Process trying to quit nicely..",
            ],
            0,
            None,
        ),
        (
            vec!["ModIndex: Load sequence finished successfully."],
            0,
            Some("without reporting completion"),
        ),
        (
            vec![
                UPDATE_COMPLETE,
                "[Workshop] ItemQuery failed entirely, unrecoverable.",
            ],
            0,
            Some("ItemQuery failed"),
        ),
        (
            vec![
                "DownloadServerMods timed out with no response from Workshop...",
                "#ERROR: Failure to load dedicated_server_mods_setup.lua:",
                UPDATE_COMPLETE,
            ],
            0,
            Some("Failure to load"),
        ),
        (vec![UPDATE_COMPLETE], 7, Some("exit status: 7")),
    ] {
        let directory = tempfile::tempdir().unwrap();
        let (executable, ugc) = fixture(directory.path(), &output(&lines, status));
        let outcome = update_native(&executable, &ugc, None, &EventHub::default()).await;
        if let Some(error) = error {
            assert!(outcome.unwrap_err().message.contains(error));
        } else {
            outcome.unwrap();
        }
        assert_eq!(fs::read_to_string(ugc.join("attempts")).unwrap(), "1");
        assert_eq!(
            fs::read_to_string(ugc.join("partial")).unwrap(),
            "retained download"
        );
    }
}

#[tokio::test]
async fn proxy_is_explicit_and_native_arguments_use_isolated_storage_and_ports() {
    let directory = tempfile::tempdir().unwrap();
    let source = format!(
        r#"
(ugc / 'environment.json').write_text(json.dumps(dict(os.environ)))
(ugc / 'arguments.json').write_text(json.dumps(sys.argv))
print({})
"#,
        serde_json::to_string(UPDATE_COMPLETE).unwrap()
    );
    let (executable, ugc) = fixture(directory.path(), &source);
    for proxy in [None, Some("http://user:secret@127.0.0.1:1080")] {
        update_native(&executable, &ugc, proxy, &EventHub::default())
            .await
            .unwrap();
        let environment: Value =
            serde_json::from_slice(&fs::read(ugc.join("environment.json")).unwrap()).unwrap();
        let proxies: serde_json::Map<_, _> = environment
            .as_object()
            .unwrap()
            .iter()
            .filter(|(name, _)| {
                [
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "ftp_proxy",
                    "no_proxy",
                ]
                .contains(&name.to_ascii_lowercase().as_str())
            })
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect();
        assert_eq!(
            Value::Object(proxies),
            proxy.map_or_else(
                || json!({}),
                |proxy| json!({"http_proxy": proxy, "https_proxy": proxy})
            )
        );
        let arguments: Vec<String> =
            serde_json::from_slice(&fs::read(ugc.join("arguments.json")).unwrap()).unwrap();
        let argument =
            |key| arguments[arguments.iter().position(|value| value == key).unwrap() + 1].clone();
        assert_ne!(argument("-port"), argument("-steam_master_server_port"));
        assert_ne!(
            argument("-persistent_storage_root"),
            directory.path().join("room").to_string_lossy()
        );
        assert_eq!(
            argument("-monitor_parent_process"),
            std::process::id().to_string()
        );
        assert!(!Path::new(&argument("-persistent_storage_root")).exists());
    }
    for proxy in [
        "",
        "socks5://host",
        "http://host:0",
        "http://host:99999",
        "http://host\n",
    ] {
        assert_eq!(
            download_environment(Some(proxy)).unwrap_err().code,
            ErrorCode::Invalid
        );
    }
    let proxy = "http://user:secret@127.0.0.1:1080";
    let (executable, ugc) = fixture(
        directory.path(),
        &output(
            &[&format!(
                "#ERROR: Failure to load dedicated_server_mods_setup.lua: {proxy}"
            )],
            0,
        ),
    );
    let error = update_native(&executable, &ugc, Some(proxy), &EventHub::default())
        .await
        .unwrap_err();
    assert!(!error.message.contains("secret"));
    assert!(error.message.contains("***"));
}

fn stopped(pid: u32) -> bool {
    fs::read_to_string(format!("/proc/{pid}/stat")).map_or(true, |stat| {
        stat.rsplit_once(')')
            .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z'))
    })
}

async fn wait_until(mut ready: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while !ready() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn cancellation_and_parent_exit_reclaim_the_entire_downloader_group() {
    for cancel in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let source = format!(
            r#"
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()'])
(ugc / 'descendant').write_text(str(child.pid))
print({}, flush=True)
{}
"#,
            serde_json::to_string(UPDATE_COMPLETE).unwrap(),
            if cancel {
                "time.sleep(60)"
            } else {
                "sys.exit(0)"
            }
        );
        let (executable, ugc) = fixture(directory.path(), &source);
        let task_ugc = ugc.clone();
        let task = tokio::spawn(async move {
            update_native(&executable, &task_ugc, None, &EventHub::default()).await
        });
        wait_until(|| ugc.join("descendant").exists()).await;
        if cancel {
            task.abort();
            assert!(task.await.unwrap_err().is_cancelled());
        } else {
            task.await.unwrap().unwrap();
        }
        let parent: u32 = fs::read_to_string(ugc.join("pid"))
            .unwrap()
            .parse()
            .unwrap();
        let descendant: u32 = fs::read_to_string(ugc.join("descendant"))
            .unwrap()
            .parse()
            .unwrap();
        wait_until(|| stopped(parent) && stopped(descendant)).await;
        let mut status = 0;
        // SAFETY: checking that our former child has already been reaped.
        assert_eq!(
            unsafe { libc::waitpid(parent as i32, &mut status, libc::WNOHANG) },
            -1
        );
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ECHILD)
        );
    }
}

#[tokio::test]
async fn missing_output_never_counts_as_a_successful_download() {
    let directory = tempfile::tempdir().unwrap();
    let (executable, ugc) = fixture(
        directory.path(),
        &format!(
            "print('x' * (1024 * 1024 + 1))\n{}",
            output(&[UPDATE_COMPLETE], 0)
        ),
    );
    assert_eq!(
        update_native(&executable, &ugc, None, &EventHub::default())
            .await
            .unwrap_err()
            .code,
        ErrorCode::Overflow
    );
}

#[tokio::test]
async fn explicit_interruption_returns_after_downloader_cleanup() {
    let directory = tempfile::tempdir().unwrap();
    let (executable, ugc) = fixture(directory.path(), "time.sleep(60)");
    let room = directory.path().join("room");
    fs::write(
        room.join("mods/dedicated_server_mods_setup.lua"),
        "ServerModSetup('42')",
    )
    .unwrap();
    symlink(room.join("mods"), directory.path().join("install/mods")).unwrap();
    let mut lock = RoomLock::try_acquire(&room).unwrap();
    let prepared = PreparedMods::prepare(&mut lock, &executable, None).unwrap();
    let (cancel, cancelled) = tokio::sync::watch::channel(0);
    let task = tokio::spawn(async move {
        prepared
            .update_cancellable(&EventHub::default(), cancelled)
            .await
    });
    wait_until(|| ugc.join("pid").exists()).await;
    let pid: u32 = fs::read_to_string(ugc.join("pid"))
        .unwrap()
        .parse()
        .unwrap();
    cancel.send(1).unwrap();
    assert_eq!(task.await.unwrap().unwrap_err().code, ErrorCode::Unknown);
    assert!(stopped(pid));
    let mut status = 0;
    // SAFETY: the updater must be reaped before its stopped-room owner returns.
    assert_eq!(
        unsafe { libc::waitpid(pid as i32, &mut status, libc::WNOHANG) },
        -1
    );
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ECHILD)
    );
}
