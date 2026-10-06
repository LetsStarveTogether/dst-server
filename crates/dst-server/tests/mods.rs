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
(ugc / 'pid.tmp').write_text(str(os.getpid()))
(ugc / 'pid.tmp').replace(ugc / 'pid')
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

fn configured_maintenance() -> (tempfile::TempDir, RoomLock) {
    use dst_server::{
        mods::UpdateTarget,
        policy::{ModPhase, PolicyState},
        settings::ClusterConfig,
    };
    let directory = tempfile::tempdir().unwrap();
    let mut lock = RoomLock::try_acquire(directory.path()).unwrap();
    let config = ClusterConfig::from_value(json!({
        "settings":{"cluster_key":"maintenance-fixture","master_ip":"127.0.0.1"}, "mod_settings":{},
        "shards":{
            "Master":{"settings":{"is_master":true,"id":1,"name":"Master","server_port":19000},"mods":{"entries":{"workshop-42":{"enabled":true}}}},
            "Caves":{"settings":{"is_master":false,"id":2,"name":"Caves","server_port":19001,"master_server_port":19002},"mods":{"entries":{"workshop-42":{"enabled":true}}}}
        }
    })).unwrap();
    config
        .save(
            &mut lock.while_stopped(),
            dst_server::files::PermissionFiles::Preserve,
        )
        .unwrap();
    let mut state = PolicyState::default();
    state.queue_mod_update(true).unwrap();
    state.mods.phase = ModPhase::Verifying;
    state.mods.attempts = 1;
    state.mods.targets = [(
        42,
        UpdateTarget {
            version: "2".into(),
            shards: ["Master".into(), "Caves".into()].into(),
        },
    )]
    .into();
    state.save(&mut lock).unwrap();
    lock.update_state(|value| {
        value.insert("other".into(), json!("preserved"));
        Ok(())
    })
    .unwrap();
    (directory, lock)
}

#[test]
fn structured_and_raw_saves_reconcile_active_shards_and_shared_enablement() {
    use dst_server::{files::PermissionFiles, policy::PolicyState, settings::ClusterConfig};
    let (directory, mut lock) = configured_maintenance();
    let before = PolicyState::load(&lock).unwrap().mods;
    let mut configuration = ClusterConfig::load(&lock).unwrap().into_value();
    configuration["shards"]["Caves"]["mods"] = json!({});
    let changed = ClusterConfig::from_value(configuration)
        .unwrap()
        .save(&mut lock.while_stopped(), PermissionFiles::Preserve)
        .unwrap();
    assert!(changed.contains(&PathBuf::from("Caves/modoverrides.lua")));
    assert!(!changed.contains(&PathBuf::from(dst_server::files::STATE_FILE)));
    let mut expected = before.clone();
    expected
        .targets
        .get_mut(&42)
        .unwrap()
        .shards
        .remove("Caves");
    assert_eq!(PolicyState::load(&lock).unwrap().mods, expected);

    let server = lock.read_text("Caves/server.ini").unwrap();
    lock.while_stopped()
        .write_files(
            [
                (PathBuf::from("Island/server.ini"), server),
                (
                    PathBuf::from("Island/modoverrides.lua"),
                    "return {['workshop-42']={enabled=true}}\n".into(),
                ),
                (
                    PathBuf::from("mods/modsettings.lua"),
                    "ForceEnableMod('workshop-42')\n".into(),
                ),
                (
                    PathBuf::from("Master/modoverrides.lua"),
                    "return {}\n".into(),
                ),
            ]
            .into(),
            vec![],
            PermissionFiles::Preserve,
        )
        .unwrap();
    assert_eq!(
        PolicyState::load(&lock).unwrap().mods.targets[&42].shards,
        ["Master".into(), "Caves".into(), "Island".into()].into()
    );
    fs::create_dir_all(directory.path().join("Island/save")).unwrap();
    lock.while_stopped()
        .write_files(
            Default::default(),
            vec!["Island/server.ini".into()],
            PermissionFiles::Preserve,
        )
        .unwrap();
    assert!(directory.path().join("Island/save").is_dir());
    assert_eq!(
        PolicyState::load(&lock).unwrap().mods.targets[&42].shards,
        ["Master".into(), "Caves".into()].into()
    );
    lock.while_stopped()
        .write_files(
            [(PathBuf::from("mods/modsettings.lua"), String::new())].into(),
            vec![],
            PermissionFiles::Preserve,
        )
        .unwrap();
    assert_eq!(PolicyState::load(&lock).unwrap().mods, Default::default());
    assert_eq!(lock.read_state().unwrap()["other"], "preserved");

    for was_active in [true, false] {
        let (directory, mut lock) = configured_maintenance();
        fs::remove_file(directory.path().join("Caves/modoverrides.lua")).unwrap();
        if !was_active {
            fs::remove_file(directory.path().join("Caves/server.ini")).unwrap();
        }
        lock.while_stopped()
            .write_files(
                Default::default(),
                vec!["Caves/server.ini".into()],
                PermissionFiles::Preserve,
            )
            .unwrap();
        let required = PolicyState::load(&lock).unwrap().mods.targets[&42]
            .shards
            .clone();
        assert_eq!(
            required.contains("Caves"),
            !was_active,
            "only a newly removed active shard cancels its historical Mod target"
        );
        assert!(required.contains("Master"));
    }
}

#[test]
fn ambiguous_mod_names_preserve_maintenance_without_implying_downloads() {
    use dst_server::{
        files::PermissionFiles,
        policy::PolicyState,
        settings::{ClusterConfig, ModOverrides},
    };
    let canonical = "return {['workshop-42']={enabled=true}}\n";
    let numeric = "return {['42']={enabled=true}}\n";
    let conflicting = "return {['workshop-42']={enabled=true}, ['42']={enabled=false}}\n";
    for structured in [false, true] {
        for (old, new, retained, downloads) in [
            (canonical, numeric, true, vec![]),
            (canonical, conflicting, true, vec![42]),
            (conflicting, "return {}\n", true, vec![]),
            (numeric, "return {}\n", true, vec![]),
            (
                canonical,
                "return {['042']={enabled=true}}\n",
                false,
                vec![],
            ),
        ] {
            let (directory, mut lock) = configured_maintenance();
            for shard in ["Master", "Caves"] {
                fs::write(directory.path().join(shard).join("modoverrides.lua"), old).unwrap();
            }
            let before = PolicyState::load(&lock).unwrap().mods;
            let mut value = ClusterConfig::load(&lock).unwrap().into_value();
            value["downloads"] = json!({});
            for shard in ["Master", "Caves"] {
                value["shards"][shard]["mods"] = ModOverrides::parse(new).unwrap().into_value();
            }
            let configuration = ClusterConfig::from_value(value).unwrap();
            assert_eq!(
                configuration.resolved_downloads().unwrap().data().items(),
                downloads
            );
            if structured {
                configuration
                    .save(&mut lock.while_stopped(), PermissionFiles::Preserve)
                    .unwrap();
            } else {
                lock.while_stopped()
                    .write_files(
                        ["Master", "Caves"]
                            .map(|shard| {
                                (PathBuf::from(shard).join("modoverrides.lua"), new.into())
                            })
                            .into(),
                        vec![],
                        PermissionFiles::Preserve,
                    )
                    .unwrap();
            }
            ClusterConfig::load(&lock).unwrap();
            assert_eq!(
                PolicyState::load(&lock).unwrap().mods,
                if retained { before } else { Default::default() },
                "structured={structured}, {old} -> {new}"
            );
        }
    }
}

#[test]
fn ambiguous_mod_names_allow_proven_shard_changes() {
    use dst_server::{files::PermissionFiles, policy::PolicyState};
    let (_directory, mut lock) = configured_maintenance();
    let mut expected = PolicyState::load(&lock).unwrap().mods;
    lock.while_stopped()
        .write_files(
            [
                (
                    "Master/modoverrides.lua".into(),
                    "return {['42']={enabled=true}}\n".into(),
                ),
                ("Caves/modoverrides.lua".into(), "return {}\n".into()),
                (
                    "Island/server.ini".into(),
                    "[SHARD]\nis_master=false\nid=3\nname=Island\n[NETWORK]\nserver_port=19003\n"
                        .into(),
                ),
                (
                    "Island/modoverrides.lua".into(),
                    "return {['42']={enabled=true}}\n".into(),
                ),
            ]
            .into(),
            vec![],
            PermissionFiles::Preserve,
        )
        .unwrap();
    expected.targets.get_mut(&42).unwrap().shards = ["Master".into()].into();
    assert_eq!(PolicyState::load(&lock).unwrap().mods, expected);

    lock.while_stopped()
        .write_files(
            [(
                "Island/modoverrides.lua".into(),
                "return {['workshop-42']={enabled=true}}\n".into(),
            )]
            .into(),
            vec!["Master/server.ini".into()],
            PermissionFiles::Preserve,
        )
        .unwrap();
    expected.targets.get_mut(&42).unwrap().shards = ["Island".into()].into();
    assert_eq!(PolicyState::load(&lock).unwrap().mods, expected);
}

#[test]
fn structured_save_repairs_legacy_lua_and_preserves_maintenance_targets() {
    use dst_server::{files::PermissionFiles, policy::PolicyState, settings::ClusterConfig};
    for path in ["Caves/modoverrides.lua", "mods/modsettings.lua"] {
        let (directory, mut lock) = configured_maintenance();
        let before = PolicyState::load(&lock).unwrap().mods;
        let mut desired = ClusterConfig::load(&lock).unwrap().into_value();
        desired["shards"]["Caves"]["mods"] = json!({});
        let mut legacy = b"-- old comment \xe9\n".to_vec();
        legacy.extend(fs::read(directory.path().join(path)).unwrap());
        fs::write(directory.path().join(path), &legacy).unwrap();

        if path == "mods/modsettings.lua" {
            let mut omitted = desired.clone();
            omitted.as_object_mut().unwrap().remove("mod_settings");
            ClusterConfig::from_value(omitted)
                .unwrap()
                .save(&mut lock.while_stopped(), PermissionFiles::Preserve)
                .unwrap();
            assert_eq!(fs::read(directory.path().join(path)).unwrap(), legacy);
        }
        let desired = ClusterConfig::from_value(desired).unwrap();
        let written = desired
            .save(&mut lock.while_stopped(), PermissionFiles::Preserve)
            .unwrap();
        assert!(written.contains(&PathBuf::from(path)));
        assert_eq!(
            lock.read_text(path).unwrap(),
            desired.files().unwrap()[Path::new(path)]
        );
        assert_eq!(PolicyState::load(&lock).unwrap().mods, before);
    }
}

#[test]
fn configuration_uncertainty_and_initialization_preserve_existing_targets() {
    use dst_server::{
        files::PermissionFiles,
        policy::{ModPhase, PolicyState},
    };
    for scenario in [
        "opaque",
        "missing",
        "initialization",
        "updating",
        "unrelated",
    ] {
        let (directory, mut lock) = configured_maintenance();
        match scenario {
            "opaque" => fs::write(
                directory.path().join("Caves/modoverrides.lua"),
                "return GetMods()\n",
            )
            .unwrap(),
            "missing" => fs::write(
                directory.path().join("Caves/modoverrides.lua"),
                "return {}\n",
            )
            .unwrap(),
            "initialization" => {
                fs::remove_file(directory.path().join("mods/modsettings.lua")).unwrap()
            }
            "updating" => {
                let mut state = PolicyState::load(&lock).unwrap();
                state.mods.phase = ModPhase::Updating;
                state.save(&mut lock).unwrap();
            }
            _ => {}
        }
        let before = PolicyState::load(&lock).unwrap().mods;
        if scenario == "initialization" {
            prepare_shared(&mut lock).unwrap();
        } else {
            let path = if scenario == "unrelated" {
                "Caves/worldgenoverride.lua"
            } else {
                "Caves/modoverrides.lua"
            };
            lock.while_stopped()
                .write_files(
                    [(PathBuf::from(path), "return {}\n".into())].into(),
                    vec![],
                    PermissionFiles::Preserve,
                )
                .unwrap();
        }
        assert_eq!(PolicyState::load(&lock).unwrap().mods, before, "{scenario}");
    }
}

#[test]
fn partial_configuration_failures_reconcile_only_changes_that_reached_disk() {
    use dst_server::{files::PermissionFiles, policy::PolicyState};
    for blocked in ["Caves", "Master"] {
        let (directory, mut lock) = configured_maintenance();
        fs::create_dir(directory.path().join(blocked).join(".dst-config-write.tmp")).unwrap();
        let error = lock
            .while_stopped()
            .write_files(
                [
                    (
                        PathBuf::from("Caves/modoverrides.lua"),
                        "return {}\n".into(),
                    ),
                    (
                        PathBuf::from("Master/modoverrides.lua"),
                        "return {}\n".into(),
                    ),
                ]
                .into(),
                vec![],
                PermissionFiles::Preserve,
            )
            .unwrap_err();
        assert!(format!("{error:#}").contains(&format!("{blocked}/modoverrides.lua")));
        assert!(
            error.root_cause().to_string().contains("regular"),
            "{error:#}"
        );
        let expected = if blocked == "Caves" {
            ["Caves", "Master"].as_slice()
        } else {
            ["Master"].as_slice()
        };
        assert_eq!(
            PolicyState::load(&lock).unwrap().mods.targets[&42].shards,
            expected.iter().map(|s| s.to_string()).collect()
        );
    }
}

#[test]
fn state_write_failure_reports_applied_configuration_without_claiming_retry_repairs_it() {
    use dst_server::{files::PermissionFiles, policy::PolicyState};
    for config_failure in [false, true] {
        let (directory, mut lock) = configured_maintenance();
        let before = PolicyState::load(&lock).unwrap().mods;
        let root_blocker = directory.path().join(".dst-config-write.tmp");
        fs::create_dir(&root_blocker).unwrap();
        let mut files = [(
            PathBuf::from("Caves/modoverrides.lua"),
            "return {}\n".into(),
        )]
        .into_iter()
        .collect::<dst_server::files::FileChanges>();
        if config_failure {
            fs::create_dir(directory.path().join("Master/.dst-config-write.tmp")).unwrap();
            files.insert("Master/modoverrides.lua".into(), "return {}\n".into());
        }
        let error = lock
            .while_stopped()
            .write_files(files.clone(), vec![], PermissionFiles::Preserve)
            .unwrap_err();
        assert!(format!("{error:#}").contains("maintenance"), "{error:#}");
        if config_failure {
            assert!(format!("{error:#}").contains("Master/modoverrides.lua"));
        }
        assert!(
            error.root_cause().to_string().contains("regular"),
            "{error:#}"
        );
        assert_eq!(
            lock.read_text("Caves/modoverrides.lua").unwrap(),
            "return {}\n"
        );
        assert_eq!(PolicyState::load(&lock).unwrap().mods, before);
        fs::remove_dir(root_blocker).unwrap();
        if config_failure {
            fs::remove_dir(directory.path().join("Master/.dst-config-write.tmp")).unwrap();
        }
        lock.while_stopped()
            .write_files(files, vec![], PermissionFiles::Preserve)
            .unwrap();
        let after = PolicyState::load(&lock).unwrap().mods;
        assert!(
            after.targets[&42].shards.contains("Caves"),
            "the lost enabled delta is not recreated by an identical save"
        );
        assert_eq!(
            after.targets[&42].shards.contains("Master"),
            !config_failure
        );
    }
}

#[test]
fn scan_keeps_dynamic_setup_and_reads_only_top_level_literal_declarations() {
    let setup = scan_setup(
        r#"
-- ServerModSetup("100")
ServerModSetup(""); ServerModSetup('42'); ServerModSetup([[7]])
ServerModSetup(([[17]])); other.ServerModSetup("100"); ServerModSetup("101")()
local id = "99"; ServerModSetup(id)
if false then ServerModSetup("999") end
ServerModSetup("4" .. "2"); OtherSetup("700"); ServerModSetup("42", "43")
ServerModSetup("４２")
return ServerModSetup("8"), ServerModCollectionSetup("99")
"#,
    )
    .unwrap();
    assert_eq!(setup.items, BTreeSet::from([7, 8, 17, 42]));
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
        prepared.update(&|_| {}).await.unwrap()["download_completed"],
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
        prepared.update(&|_| {}).await.unwrap()["download_completed"],
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
        let outcome = update_native(&executable, &ugc, None, &|_| {}).await;
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
        update_native(&executable, &ugc, proxy, &|_| {})
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
    let error = update_native(&executable, &ugc, Some(proxy), &|_| {})
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
(ugc / 'descendant.tmp').write_text(str(child.pid))
(ugc / 'descendant.tmp').replace(ugc / 'descendant')
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
        let task =
            tokio::spawn(async move { update_native(&executable, &task_ugc, None, &|_| {}).await });
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
        if cancel {
            // Aborting the caller only requests cleanup; the supervisor still has to reap.
            wait_until(|| !Path::new(&format!("/proc/{parent}")).exists()).await;
        }
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
        wait_until(|| stopped(descendant)).await;
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
        update_native(&executable, &ugc, None, &|_| {})
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
    let task = tokio::spawn(async move { prepared.update_cancellable(&|_| {}, cancelled).await });
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
