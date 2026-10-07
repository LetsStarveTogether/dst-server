use super::*;
use crate::{files::RoomLock, lua};
use ::zip::{ZipWriter, write::SimpleFileOptions};
use std::io::{Read, Write};
use std::time::Duration;
use tempfile::TempDir;

async fn import_to(source: ImportSource, destination: PathBuf) -> Result<ClusterConfig> {
    let imported = import_cluster(source).await?;
    imported.save(destination)?;
    Ok(imported.configuration().clone())
}

fn zip_archive(entries: &[(String, Vec<u8>)]) -> File {
    let mut writer = ZipWriter::new(memory_file().unwrap());
    for (name, bytes) in entries {
        writer
            .start_file(name, SimpleFileOptions::default())
            .unwrap();
        writer.write_all(bytes).unwrap();
    }
    writer.finish().unwrap()
}

fn tree(root: &Path) -> Vec<(String, Vec<u8>)> {
    fn visit(root: &Path, directory: &Path, entries: &mut Vec<(String, Vec<u8>)>) {
        for entry in fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(root, &path, entries);
            } else {
                entries.push((
                    path.strip_prefix(root).unwrap().to_str().unwrap().into(),
                    fs::read(path).unwrap(),
                ));
            }
        }
    }
    let mut entries = Vec::new();
    visit(root, root, &mut entries);
    entries.sort();
    entries
}

#[tokio::test]
async fn apply_import_replaces_cumulative_layers_and_preserves_other_files() {
    use std::os::unix::fs::MetadataExt;
    let source = cluster();
    add_secondary(source.path());
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    for level in [ImportLevel::Data, ImportLevel::Runtime, ImportLevel::All] {
        let target = cluster();
        add_secondary(target.path());
        fs::rename(target.path().join("forest"), target.path().join("Surface")).unwrap();
        fs::rename(
            target.path().join("underground"),
            target.path().join("Below"),
        )
        .unwrap();
        let cluster_ini = fs::read_to_string(target.path().join("cluster.ini"))
            .unwrap()
            .replace("Shared", "Existing");
        fs::write(target.path().join("cluster.ini"), cluster_ini).unwrap();
        fs::write(
            target.path().join("Surface/modoverrides.lua"),
            b"return {local_mod={enabled=true}}",
        )
        .unwrap();
        fs::write(
            target.path().join("Surface/leveldataoverride.lua"),
            b"return {id=\"SAVED\",name=\"world\",desc=\"\",location=\"forest\",overrides={task_set=\"default\",day=\"onlynight\"}}",
        )
        .unwrap();
        fs::write(
            target
                .path()
                .join("Surface/save/session/0123456789ABCDEF/old-world"),
            b"old world",
        )
        .unwrap();
        fs::write(target.path().join(".dst-server.json"), br#"{"policy":{"idle":{}},"template":"custom","agent_run":{},"recovery":{},"policy_state":{}}"#).unwrap();
        drop(RoomLock::try_acquire(target.path()).unwrap());
        let before: BTreeMap<_, _> = tree(target.path()).into_iter().collect();
        let lock_inode = fs::metadata(target.path().join(crate::files::ROOM_LOCK_FILE))
            .unwrap()
            .ino();
        let cache = target
            .path()
            .join("mods/ugc/content/322330/mod/modmain.lua");
        let cache_inode = fs::metadata(&cache).unwrap().ino();

        let configuration = imported.apply(target.path(), level).unwrap();
        assert_eq!(
            configuration.resolved(),
            ClusterConfig::load(&RoomReader::open(target.path()).unwrap())
                .unwrap()
                .resolved()
        );
        for (path, bytes) in before {
            let parts: Vec<_> = path.split('/').collect();
            let save = parts.get(1) == Some(&"save");
            let runtime = path.starts_with("mods/")
                && matches!(
                    parts.last(),
                    Some(&"modsettings.lua" | &"dedicated_server_mods_setup.lua")
                )
                || matches!(
                    parts.get(1),
                    Some(&"modoverrides.lua" | &"worldgenoverride.lua" | &"leveldataoverride.lua")
                );
            let ini = path == "cluster.ini" || parts.get(1) == Some(&"server.ini");
            if !save
                && !(runtime && level >= ImportLevel::Runtime)
                && !(ini && level == ImportLevel::All)
                && path != crate::files::STATE_FILE
            {
                assert_eq!(
                    fs::read(target.path().join(&path)).unwrap(),
                    bytes,
                    "{level:?}: {path}"
                );
            }
        }
        assert_eq!(
            fs::metadata(target.path().join(crate::files::ROOM_LOCK_FILE))
                .unwrap()
                .ino(),
            lock_inode
        );
        assert_eq!(fs::metadata(&cache).unwrap().ino(), cache_inode);
        assert_eq!(
            fs::read(
                target
                    .path()
                    .join("Surface/save/session/0123456789ABCDEF/0000000001")
            )
            .unwrap(),
            b"world\0\xff"
        );
        assert!(
            !target
                .path()
                .join("Surface/save/session/0123456789ABCDEF/old-world")
                .exists()
        );
        assert!(!target.path().join("Surface/save/server_temp").exists());
        let player = if level == ImportLevel::All {
            "KU_ABCDEFG__"
        } else {
            "A7H8MC6JHT1V"
        };
        assert_eq!(
            fs::read(target.path().join(format!(
                "Below/save/session/FEDCBA9876543210/{player}/0000000002"
            )))
            .unwrap(),
            b"cave-player\0\xff"
        );
        let index = lua::parse_return_table(
            &fs::read_to_string(target.path().join("Below/save/shardindex")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            index["server"]["encode_user_path"],
            level != ImportLevel::All
        );
        if level >= ImportLevel::Runtime {
            assert_eq!(
                fs::read(target.path().join("Surface/modoverrides.lua")).unwrap(),
                imported.files()[Path::new("forest/modoverrides.lua")]
            );
            assert!(!target.path().join("Surface/leveldataoverride.lua").exists());
        }
        assert_eq!(
            configuration.resolved()["settings"]["cluster_name"],
            if level == ImportLevel::All {
                "Shared"
            } else {
                "Existing"
            }
        );
        let state: serde_json::Value = serde_json::from_slice(
            &fs::read(target.path().join(crate::files::STATE_FILE)).unwrap(),
        )
        .unwrap();
        assert_eq!(
            state,
            serde_json::json!({"policy":{"idle":{}},"template":"custom"})
        );
    }
}

#[tokio::test]
async fn apply_import_rejects_busy_invalid_and_unsafe_destinations_without_replacing_saves() {
    let source = cluster();
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    let target = cluster();
    let lock = RoomLock::try_acquire(target.path()).unwrap();
    let before = tree(target.path());
    assert!(
        format!(
            "{:#}",
            imported
                .apply(target.path(), ImportLevel::Runtime)
                .unwrap_err()
        )
        .contains("busy")
    );
    assert_eq!(tree(target.path()), before);
    drop(lock);
    let holder = tempfile::tempdir().unwrap();
    assert!(
        imported
            .apply(holder.path().join("missing"), ImportLevel::Runtime)
            .is_err()
    );
    assert!(!holder.path().join("missing").exists());

    std::os::unix::fs::symlink(holder.path(), target.path().join("mods/unsafe")).unwrap();
    let error = imported
        .apply(target.path(), ImportLevel::Runtime)
        .unwrap_err();
    assert!(format!("{error:#}").contains("symlink"));
    fs::remove_file(target.path().join("mods/unsafe")).unwrap();
    assert_eq!(tree(target.path()), before);
    assert_eq!(fs::read_dir(holder.path()).unwrap().count(), 0);

    add_secondary(target.path());
    let before = tree(target.path());
    assert!(
        format!(
            "{:#}",
            imported
                .apply(target.path(), ImportLevel::Runtime)
                .unwrap_err()
        )
        .contains("same number")
    );
    assert_eq!(tree(target.path()), before);

    fs::remove_dir_all(source.path().join("forest/save")).unwrap();
    let empty = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    let target = cluster();
    drop(RoomLock::try_acquire(target.path()).unwrap());
    let before = tree(target.path());
    assert!(
        format!(
            "{:#}",
            empty
                .apply(target.path(), ImportLevel::Runtime)
                .unwrap_err()
        )
        .contains("no game saves")
    );
    assert_eq!(tree(target.path()), before);
}

#[tokio::test]
async fn apply_import_rejects_incomplete_worlds_before_replacing_existing_saves() {
    for (path, contents, with_index) in [
        ("recipebook", b"recipe".as_slice(), true),
        (
            "session/0123456789ABCDEF/0000000001.meta",
            b"metadata",
            true,
        ),
        (
            "session/0123456789ABCDEF/KU_ABCDEFG__/0000000001",
            b"player",
            true,
        ),
        ("session/0123456789ABCDEF/0000000001", b"", true),
        ("session/FEDCBA9876543210/0000000001", b"other world", true),
        ("session/0123456789ABCDEF/0000000001", b"world", false),
    ] {
        let source = cluster();
        let save = source.path().join("forest/save");
        let index = fs::read(save.join("shardindex")).unwrap();
        fs::remove_dir_all(&save).unwrap();
        let partial = save.join(path);
        fs::create_dir_all(partial.parent().unwrap()).unwrap();
        fs::write(partial, contents).unwrap();
        if with_index {
            fs::write(save.join("shardindex"), index).unwrap();
        }
        let imported = import_cluster(ImportSource::Path(source.path().into()))
            .await
            .unwrap();
        let target = cluster();
        drop(RoomLock::try_acquire(target.path()).unwrap());
        let before = tree(target.path());
        let error = imported
            .apply(target.path(), ImportLevel::Data)
            .unwrap_err();
        assert!(
            error.to_string().contains(if with_index {
                "world snapshot"
            } else {
                "missing shardindex"
            }),
            "{error:#}"
        );
        assert_eq!(tree(target.path()), before);
    }
}

#[tokio::test]
async fn apply_import_checks_the_latest_native_world_snapshot() {
    for (name, bytes, keep_older, accepted) in [
        ("0000000002", b"".as_slice(), true, false),
        ("1", b"world", false, false),
        ("000000001", b"world", false, false),
        ("0000000000", b"world", false, false),
        ("9007199254740991", b"world", false, false),
        ("4294967296", b"world", true, false),
        ("9999999999", b"world", true, false),
        ("4294967295", b"world", true, true),
    ] {
        let source = cluster();
        let session = source.path().join("forest/save/session/0123456789ABCDEF");
        if !keep_older {
            fs::remove_dir_all(&session).unwrap();
            fs::create_dir(&session).unwrap();
        }
        fs::write(session.join(name), bytes).unwrap();
        let imported = import_cluster(ImportSource::Path(source.path().into()))
            .await
            .unwrap();
        let target = cluster();
        drop(RoomLock::try_acquire(target.path()).unwrap());
        let before = tree(target.path());
        let result = imported.apply(target.path(), ImportLevel::Data);
        assert_eq!(result.is_ok(), accepted, "{name}: {result:?}");
        if accepted {
            assert_eq!(
                fs::read(
                    target
                        .path()
                        .join("forest/save/session/0123456789ABCDEF")
                        .join(name)
                )
                .unwrap(),
                bytes
            );
        } else {
            assert_eq!(tree(target.path()), before);
        }
    }
}

fn mapping_room(shards: &[(&str, Option<u64>)]) -> TempDir {
    let root = tempfile::tempdir().unwrap();
    let shards: BTreeMap<_, _> = shards
        .iter()
        .enumerate()
        .map(|(position, (name, id))| {
            (
                *name,
                serde_json::json!({"settings": {"name": name, "id": id, "is_master": position == 0,
            "server_port": 11000 + position, "master_server_port": 28000 + position}}),
            )
        })
        .collect();
    let configuration = ClusterConfig::from_value(serde_json::json!({"settings":{"shard_enabled":true,"master_ip":"127.0.0.1","cluster_key":"mapping-fixture"},"shards":shards})).unwrap();
    for (path, text) in configuration.files().unwrap() {
        let path = root.path().join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    for name in shards.keys() {
        let save = root.path().join(name).join("save");
        fs::create_dir_all(save.join("session/0123456789ABCDEF")).unwrap();
        fs::write(save.join("session/0123456789ABCDEF/0000000001"), name).unwrap();
        fs::write(save.join("shardindex"), b"KLEI     1 return {session_id=\"0123456789ABCDEF\",world={options={id=\"CUSTOM\",name=\"world\",desc=\"\",location=\"forest\",overrides={task_set=\"default\"}}},server={},enabled_mods={}}").unwrap();
    }
    drop(RoomLock::try_acquire(root.path()).unwrap());
    root
}

#[tokio::test]
async fn apply_import_does_not_use_generated_ids_to_resolve_ambiguous_shards() {
    let source = mapping_room(&[("Master", None), ("East", None), ("West", None)]);
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    assert_eq!(
        imported.configuration().resolved()["shards"]["East"]["settings"]["id"],
        2
    );
    let target = mapping_room(&[
        ("Surface", Some(1)),
        ("BelowEast", Some(3)),
        ("BelowWest", Some(2)),
    ]);
    let before = tree(target.path());
    assert!(
        imported
            .apply(target.path(), ImportLevel::Data)
            .unwrap_err()
            .to_string()
            .contains("cannot match")
    );
    assert_eq!(tree(target.path()), before);

    let source = mapping_room(&[("Master", Some(1)), ("East", Some(2)), ("West", Some(3))]);
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    imported.apply(target.path(), ImportLevel::Data).unwrap();
    assert_eq!(
        fs::read(
            target
                .path()
                .join("BelowEast/save/session/0123456789ABCDEF/0000000001")
        )
        .unwrap(),
        b"West"
    );
    assert_eq!(
        fs::read(
            target
                .path()
                .join("BelowWest/save/session/0123456789ABCDEF/0000000001")
        )
        .unwrap(),
        b"East"
    );
}

#[tokio::test]
async fn apply_import_matches_explicit_ids_before_remaining_roles_regardless_of_directory_order() {
    let source = mapping_room(&[("Master", Some(1)), ("A", Some(4)), ("B", Some(3))]);
    for renamed in [false, true] {
        if renamed {
            fs::rename(source.path().join("A"), source.path().join("Z")).unwrap();
        }
        let target = mapping_room(&[("Surface", Some(1)), ("D", Some(2)), ("E", Some(3))]);
        let imported = import_cluster(ImportSource::Path(source.path().into()))
            .await
            .unwrap();
        imported.apply(target.path(), ImportLevel::Data).unwrap();
        assert_eq!(
            fs::read(
                target
                    .path()
                    .join("D/save/session/0123456789ABCDEF/0000000001")
            )
            .unwrap(),
            b"A"
        );
        assert_eq!(
            fs::read(
                target
                    .path()
                    .join("E/save/session/0123456789ABCDEF/0000000001")
            )
            .unwrap(),
            b"B"
        );
    }
}

#[tokio::test]
async fn apply_import_only_parses_runtime_configuration_when_preserving_it() {
    let source = cluster();
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    for level in [ImportLevel::Runtime, ImportLevel::All] {
        let target = cluster();
        fs::write(
            target.path().join("forest/worldgenoverride.lua"),
            b"return {overrides={day='onlynight'}}",
        )
        .unwrap();
        fs::write(
            target.path().join("forest/leveldataoverride.lua"),
            b"return unsupported_dynamic_options()",
        )
        .unwrap();
        fs::write(
            target.path().join("mods/dedicated_server_mods_setup.lua"),
            b"for _, id in ipairs(downloads) do ServerModSetup(id) end",
        )
        .unwrap();
        drop(RoomLock::try_acquire(target.path()).unwrap());
        let before = tree(target.path());
        assert!(imported.apply(target.path(), ImportLevel::Data).is_err());
        assert_eq!(tree(target.path()), before);
        let effective = imported.apply(target.path(), level).unwrap();
        assert_eq!(
            effective.resolved(),
            ClusterConfig::load(&RoomReader::open(target.path()).unwrap())
                .unwrap()
                .resolved()
        );
        assert_eq!(
            fs::read(target.path().join("forest/worldgenoverride.lua")).unwrap(),
            imported.files()[Path::new("forest/worldgenoverride.lua")]
        );
        assert!(!target.path().join("forest/leveldataoverride.lua").exists());
    }
}

#[tokio::test]
async fn apply_import_validates_event_configuration_after_merging_runtime_files() {
    for mode in ["quagmire", "lavaarena"] {
        let source = mapping_room(&[("Master", Some(1))]);
        fs::write(
            source.path().join("cluster.ini"),
            format!("[GAMEPLAY]\ngame_mode = {mode}\n"),
        )
        .unwrap();
        fs::write(
            source.path().join("Master/leveldataoverride.lua"),
            format!(
                "return {{id=\"{}\",name=\"event\",desc=\"\",location=\"{mode}\",overrides={{task_set=\"{mode}_taskset\"}}}}",
                mode.to_uppercase()
            ),
        )
        .unwrap();
        let imported = import_cluster(ImportSource::Path(source.path().into()))
            .await
            .unwrap();
        for level in [ImportLevel::Runtime, ImportLevel::All] {
            let holder = tempfile::tempdir().unwrap();
            let target = holder.path().join("room");
            imported.save(&target).unwrap();
            let effective = imported.apply(&target, level).unwrap();
            assert_eq!(effective.resolved()["settings"]["game_mode"], mode);
            assert_eq!(
                effective.resolved(),
                ClusterConfig::load(&RoomReader::open(&target).unwrap())
                    .unwrap()
                    .resolved()
            );
        }
    }
}

#[tokio::test]
async fn apply_import_cleans_read_only_discarded_directories_after_success_and_rejection() {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let source = cluster();
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    for rejected in [false, true] {
        let holder = tempfile::tempdir().unwrap();
        let target = holder.path().join("room");
        imported.save(&target).unwrap();
        let world = target.join("forest/save/session/0123456789ABCDEF/0000000001");
        fs::write(&world, b"existing world").unwrap();
        let cache = target.join("mods/aaa");
        fs::create_dir(&cache).unwrap();
        let file = cache.join("modmain.lua");
        fs::write(&file, b"return {}").unwrap();
        let inode = fs::metadata(&file).unwrap().ino();
        fs::set_permissions(&cache, fs::Permissions::from_mode(0o555)).unwrap();
        if rejected {
            std::os::unix::fs::symlink(&file, target.join("mods/zzz")).unwrap();
        }
        let result = imported.apply(&target, ImportLevel::Data);
        assert_eq!(result.is_err(), rejected, "{result:?}");
        assert_eq!(
            fs::read(&world).unwrap(),
            if rejected {
                b"existing world".as_slice()
            } else {
                b"world\0\xff"
            }
        );
        assert_eq!(
            fs::metadata(&cache).unwrap().permissions().mode() & 0o777,
            0o555
        );
        assert_eq!(fs::metadata(&file).unwrap().ino(), inode);
        assert_eq!(fs::read(&file).unwrap(), b"return {}");
        assert_eq!(fs::read_dir(holder.path()).unwrap().count(), 1);
        fs::set_permissions(&cache, fs::Permissions::from_mode(0o755)).unwrap();
    }
}

#[tokio::test]
async fn apply_import_discards_only_a_stale_agent_socket() {
    let source = cluster();
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    let holder = tempfile::tempdir().unwrap();
    let target = holder.path().join("room");
    imported.save(&target).unwrap();
    let socket = target.join(crate::host::AGENT_SOCKET);
    let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let error = imported.apply(&target, ImportLevel::Data).unwrap_err();
    assert!(error.to_string().contains("socket is still active"));
    assert!(socket.exists());
    assert_eq!(fs::read_dir(holder.path()).unwrap().count(), 1);
    drop(listener);
    imported.apply(&target, ImportLevel::Data).unwrap();
    assert!(!socket.exists());

    let other = target.join("unrelated.sock");
    drop(std::os::unix::net::UnixListener::bind(&other).unwrap());
    assert!(
        imported
            .apply(&target, ImportLevel::Data)
            .unwrap_err()
            .to_string()
            .contains("special file")
    );
    assert!(other.exists());
    assert_eq!(fs::read_dir(holder.path()).unwrap().count(), 1);
}

#[tokio::test]
async fn import_is_in_memory_and_each_parse_gets_a_fresh_shared_key() {
    let source = cluster();
    add_secondary(source.path());
    fs::rename(
        source.path().join("forest"),
        source.path().join("Surface One"),
    )
    .unwrap();
    let before = tree(source.path());
    let first = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    let second = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    assert_eq!(tree(source.path()), before);
    let config = first.configuration().resolved();
    assert_ne!(
        config["settings"]["cluster_key"],
        second.configuration().resolved()["settings"]["cluster_key"]
    );
    assert_eq!(
        config["shards"]["Surface One"]["settings"]["is_master"],
        true
    );
    assert_eq!(
        config["shards"]["underground"]["settings"]["encode_user_path"],
        false
    );
    assert_eq!(
        first.files()
            [Path::new("underground/save/session/FEDCBA9876543210/KU_ABCDEFG__/0000000002")],
        b"cave-player\0\xff"
    );
    let destination = tempfile::tempdir().unwrap();
    for name in ["first", "second"] {
        let path = destination.path().join(name);
        first.save(&path).unwrap();
        assert_eq!(
            ClusterConfig::load(&RoomReader::open(&path).unwrap())
                .unwrap()
                .resolved(),
            first.configuration().resolved()
        );
        assert_eq!(
            tree(&path),
            first
                .files()
                .iter()
                .map(|(path, bytes)| (path.to_str().unwrap().into(), bytes.clone()))
                .collect::<Vec<_>>()
        );
    }
}

#[tokio::test]
async fn native_import_preserves_empty_and_missing_shard_mod_lists() {
    let source = cluster();
    add_secondary(source.path());
    for missing in [false, true] {
        if missing {
            fs::remove_file(source.path().join("underground/save/shardindex")).unwrap();
        }
        let imported = import_cluster(ImportSource::Path(source.path().into()))
            .await
            .unwrap();
        let config = imported.configuration().resolved();
        assert_eq!(
            config["shards"]["underground"]["mods"]["entries"],
            serde_json::json!({})
        );
        assert_eq!(
            config["shards"]["forest"]["mods"]["entries"]["workshop-123"]["enabled"],
            true
        );
    }
}

#[tokio::test]
async fn import_ignores_room_markers_in_excluded_directories() {
    let source = cluster();
    for name in [
        "mods/workshop-999/server.ini",
        "mods/cluster.ini",
        "__MACOSX/cluster.ini",
        ".hidden/server.ini",
    ] {
        let path = source.path().join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, b"ignored").unwrap();
    }
    let entries = tree(source.path());
    let wrapped = entries
        .iter()
        .map(|(path, bytes)| (format!("Room/{path}"), bytes.clone()))
        .collect::<Vec<_>>();
    for input in [
        ImportSource::Path(source.path().into()),
        ImportSource::Archive(zip_archive(&entries)),
        ImportSource::Archive(zip_archive(&wrapped)),
    ] {
        let imported = import_cluster(input).await.unwrap();
        assert_eq!(
            imported.configuration().resolved()["settings"]["cluster_name"],
            "Shared"
        );
        assert!(
            !imported
                .files()
                .contains_key(Path::new("mods/workshop-999/server.ini"))
        );
    }
}

#[tokio::test]
async fn exported_wrappers_preserve_the_full_save_path_depth() {
    let source = cluster();
    let path = format!(
        "forest/save/session/0123456789ABCDEF/{}snapshot",
        "part/".repeat(11)
    );
    assert_eq!(Path::new(&path).components().count(), 16);
    fs::create_dir_all(source.path().join(&path).parent().unwrap()).unwrap();
    fs::write(source.path().join(&path), b"deep snapshot").unwrap();
    for name in ["Room", "mods", "console"] {
        let archive = prepare(
            &RoomReader::open(source.path()).unwrap(),
            7,
            &ExportOptions {
                room_id: Some(name.into()),
                ..Default::default()
            },
        )
        .unwrap()
        .compress(DEFAULT_COMPRESSION_LEVEL)
        .await
        .unwrap();
        let imported = import_cluster(ImportSource::Archive(archive.into_file()))
            .await
            .unwrap();
        assert_eq!(imported.files()[Path::new(&path)], b"deep snapshot");
        let holder = tempfile::tempdir().unwrap();
        imported.save(holder.path().join(name)).unwrap();
        let from_directory = import_cluster(ImportSource::Path(holder.path().into()))
            .await
            .unwrap();
        assert_eq!(from_directory.files()[Path::new(&path)], b"deep snapshot");
    }
}

#[tokio::test]
async fn import_rejects_paths_that_cannot_be_saved() {
    let source = cluster();
    for path in [
        format!("forest/save/session/0123456789ABCDEF/{}", "x".repeat(256)),
        format!(
            "forest/save/session/0123456789ABCDEF/{}snapshot",
            "part/".repeat(13)
        ),
    ] {
        let mut entries = tree(source.path());
        entries.push((path, b"snapshot".to_vec()));
        let result = import_cluster(ImportSource::Archive(zip_archive(&entries))).await;
        assert!(result.is_err());
    }
}

#[tokio::test]
async fn import_rejects_sevenz_deletion_entries_and_reparse_points() {
    for anti in [false, true] {
        let mut writer = ArchiveWriter::new(memory_file().unwrap()).unwrap();
        let mut entry = ArchiveEntry::new_file("Master/save/session/0123456789ABCDEF/link");
        entry.is_anti_item = anti;
        entry.has_windows_attributes = !anti;
        entry.windows_attributes = 0x400;
        writer
            .push_archive_entry(entry, (!anti).then_some(Cursor::new(b"reparse data")))
            .unwrap();
        let error = import_cluster(ImportSource::Archive(writer.finish().unwrap()))
            .await
            .err()
            .unwrap();
        assert!(
            error.to_string().contains("links and deletion"),
            "{error:#}"
        );
    }
}

#[tokio::test]
async fn imports_rar4_and_solid_rar5_with_unicode_names_and_mixed_player_encoding() {
    // Own fixtures: RAR 6.24/7.23, a -ma4/-ma5 -m5 -md1m -ts- -r; RAR5 adds -s.
    // World/player bytes are b"world\0\xff" * 4096 and b"player\0\xff" * 1024.
    for bytes in [
        include_bytes!("../../tests/fixtures/archives/room-rar4.rar").as_slice(),
        include_bytes!("../../tests/fixtures/archives/room-rar5.rar").as_slice(),
    ] {
        let mut file = memory_file().unwrap();
        file.write_all(bytes).unwrap();
        let imported = import_cluster(ImportSource::Archive(file)).await.unwrap();
        assert_eq!(
            imported.configuration().resolved()["settings"]["cluster_name"],
            "RAR fixture"
        );
        let session = Path::new("Surface One/save/session/0123456789ABCDEF");
        assert_eq!(
            imported.files()[&session.join("000000001")],
            b"world\0\xff".repeat(4096)
        );
        assert_eq!(
            imported.files()[&session.join("KU_WRv6AVc8_/000000001")],
            b"player\0\xff".repeat(1024)
        );
        assert_eq!(
            imported.files()[&session.join("世界.meta")],
            b"unicode metadata"
        );
    }
}

#[tokio::test]
async fn import_rejects_rar_volumes_including_the_last_one() {
    // Own fixtures: final volumes from RAR 6.24/7.23, a -r -ds -ma4/-ma5 -m0 -v8k.
    for bytes in [
        include_bytes!("../../tests/fixtures/archives/last-volume-rar4.rar").as_slice(),
        include_bytes!("../../tests/fixtures/archives/last-volume-rar5.rar").as_slice(),
    ] {
        let mut file = memory_file().unwrap();
        file.write_all(bytes).unwrap();
        let error = import_cluster(ImportSource::Archive(file))
            .await
            .err()
            .unwrap();
        assert!(error.to_string().contains("multipart RAR"), "{error:#}");
    }
}

#[tokio::test]
async fn rejects_rar_hardlinks_before_interpreting_the_room() {
    // Own fixture: RAR 7.23, a -ma5 -oh -ts-, with two names for one inode.
    let mut file = memory_file().unwrap();
    file.write_all(include_bytes!(
        "../../tests/fixtures/archives/links-rar5.rar"
    ))
    .unwrap();
    let error = import_cluster(ImportSource::Archive(file))
        .await
        .err()
        .unwrap();
    assert!(
        error.to_string().contains("archive links are unsupported"),
        "{error:#}"
    );
}

#[tokio::test]
async fn import_uses_explicit_settings_and_rejects_ambiguous_master_roles() {
    let source = cluster();
    add_secondary(source.path());
    fs::write(source.path().join("forest/server.ini"), "").unwrap();
    fs::write(source.path().join("underground/server.ini"), "").unwrap();
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    assert_eq!(
        imported.configuration().resolved()["shards"]["forest"]["settings"]["is_master"],
        true
    );
    fs::copy(
        source.path().join("forest/worldgenoverride.lua"),
        source.path().join("underground/worldgenoverride.lua"),
    )
    .unwrap();
    let ambiguous = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .err()
        .unwrap();
    assert!(ambiguous.to_string().contains("master shard"));
    fs::write(
        source.path().join("underground/server.ini"),
        "[SHARD]\nis_master = true\n[NETWORK]\nserver_port = 12000\n",
    )
    .unwrap();
    let imported = import_cluster(ImportSource::Path(source.path().into()))
        .await
        .unwrap();
    let config = imported.configuration().resolved();
    assert_eq!(config["settings"]["cluster_name"], "Shared");
    assert_eq!(config["shards"]["forest"]["settings"]["is_master"], false);
    assert_eq!(
        config["shards"]["underground"]["settings"]["is_master"],
        true
    );
    assert_eq!(
        config["shards"]["underground"]["settings"]["server_port"],
        12000
    );
}

#[tokio::test]
async fn imports_sevenz_compression_methods_including_previous_zstandard_exports() {
    use sevenz_rust2::encoder_options::{LzmaOptions, ZstandardOptions};
    let source = cluster();
    let entries = tree(source.path());
    for method in [
        LzmaOptions::default().into(),
        Lzma2Options::default().into(),
        ZstandardOptions::from_level(19).into(),
    ] {
        let mut writer = ArchiveWriter::new(memory_file().unwrap()).unwrap();
        writer.set_content_methods(vec![method]);
        writer
            .push_archive_entries(
                entries
                    .iter()
                    .map(|(name, _)| ArchiveEntry::new_file(name))
                    .collect(),
                entries
                    .iter()
                    .map(|(_, bytes)| sevenz_rust2::SourceReader::new(Cursor::new(bytes)))
                    .collect(),
            )
            .unwrap();
        let imported = import_cluster(ImportSource::Archive(writer.finish().unwrap()))
            .await
            .unwrap();
        assert_eq!(
            imported.files()[Path::new("forest/save/session/0123456789ABCDEF/0000000001")],
            b"world\0\xff"
        );
        assert_eq!(
            imported.files()
                [Path::new("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/0000000001")],
            b"player\0\xff"
        );
    }
}

#[tokio::test]
async fn imports_native_directories_zip_and_exported_sevenz_with_the_same_rules() {
    for format in [
        "directory",
        "wrapper",
        "zip",
        "wrapped_zip",
        "solid_7z",
        "7z",
        "client_7z",
    ] {
        let source = cluster();
        add_secondary(source.path());
        let before = tree(source.path());
        let target = tempfile::tempdir().unwrap();
        let output = target.path().join("restored");
        let from_client = format == "client_7z";
        let input = match format {
            "directory" => ImportSource::Path(source.path().into()),
            "wrapper" => {
                fs::rename(source.path(), target.path().join("input")).unwrap();
                ImportSource::Path(target.path().into())
            }
            "zip" | "wrapped_zip" => {
                let entries = before
                    .iter()
                    .map(|(name, bytes)| {
                        (
                            if format == "wrapped_zip" {
                                format!("Cluster_7/{name}")
                            } else {
                                name.clone()
                            },
                            bytes.clone(),
                        )
                    })
                    .collect::<Vec<_>>();
                ImportSource::Archive(zip_archive(&entries))
            }
            "solid_7z" => {
                let mut writer = ArchiveWriter::new(memory_file().unwrap()).unwrap();
                writer
                    .push_archive_entries(
                        before
                            .iter()
                            .map(|(name, _)| ArchiveEntry::new_file(name))
                            .collect(),
                        before
                            .iter()
                            .map(|(_, bytes)| sevenz_rust2::SourceReader::new(Cursor::new(bytes)))
                            .collect(),
                    )
                    .unwrap();
                ImportSource::Archive(writer.finish().unwrap())
            }
            _ => {
                let options = if from_client {
                    ExportOptions {
                        client_compatible: true,
                        ..Default::default()
                    }
                } else {
                    options()
                };
                ImportSource::Archive(
                    prepare(&RoomReader::open(source.path()).unwrap(), 7, &options)
                        .unwrap()
                        .compress(DEFAULT_COMPRESSION_LEVEL)
                        .await
                        .unwrap()
                        .into_file(),
                )
            }
        };
        let configuration = import_to(input, output.clone()).await.unwrap();
        let topology = crate::configuration::discover(&output).unwrap();
        assert_eq!(topology.shards.len(), 2);
        let resolved = configuration.resolved();
        assert_eq!(resolved["token"], "");
        assert_eq!(
            resolved["settings"]["cluster_password"],
            serde_json::Value::Null
        );
        assert_eq!(
            resolved["settings"]["cluster_key"].as_str().unwrap().len(),
            64
        );
        let master = if from_client { "Master" } else { "forest" };
        let prefix = output.join(master);
        assert_eq!(
            fs::read(prefix.join("save/session/0123456789ABCDEF/0000000001")).unwrap(),
            b"world\0\xff"
        );
        assert_eq!(
            fs::read(prefix.join("save/session/0123456789ABCDEF/KU_ABCDEFG__/savelocation"))
                .unwrap(),
            b"\x81\0\0\0\x01\0\0\0\x02"
        );
        assert_eq!(
            fs::read(prefix.join("save/reforged_exp_server")).unwrap(),
            b"experience-progress\0\xff"
        );
        assert_eq!(
            resolved["shards"][master]["mods"]["entries"]["workshop-123"]["configuration_options"]
                ["difficulty"],
            "hard"
        );
        assert!(
            fs::read_to_string(output.join("mods/dedicated_server_mods_setup.lua"))
                .unwrap()
                .contains("123")
        );
        assert!(
            tree(&output)
                .iter()
                .all(|(_, bytes)| !String::from_utf8_lossy(bytes).contains("private-"))
        );
        assert!(!prefix.join("save/server_temp").exists());
        assert!(!output.join("mods/ugc").exists());
        if from_client {
            assert_eq!(
                resolved["shards"]["Caves"]["settings"]["encode_user_path"],
                false
            );
            assert_eq!(
                fs::read(
                    output.join("Caves/save/session/FEDCBA9876543210/KU_ABCDEFG__/0000000002")
                )
                .unwrap(),
                b"cave-player\0\xff"
            );
        }
        if format != "wrapper" {
            assert_eq!(tree(source.path()), before);
        }
        assert!(fs::read_dir(target.path()).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".dst-import-")
        }));
    }
}

#[tokio::test]
async fn import_detects_client_configuration_and_restores_player_paths() {
    let players = [
        ("KU_ABCDEFGH_", "A7H8MC6JHT0H"),
        ("KU_abcdefgh_", "A7KIB6JQ56LB"),
        ("KU_WRv6AVc8_", "A7K1NP32JUC8"),
        ("KU_ABCDEFG__", "A7H8MC6JHT1V"),
        ("1234567890__", "1234567890__"),
    ];
    for (caves, encoded, custom) in [
        (false, false, false),
        (false, true, true),
        (true, false, false),
        (true, true, true),
    ] {
        let master = if custom { "Surface One" } else { "Master" };
        let secondary = if custom { "Lower World" } else { "Caves" };
        let input = tempfile::tempdir().unwrap();
        for name in if caves {
            vec![master, secondary]
        } else {
            vec![master]
        } {
            let encoded = if name == master { encoded } else { !encoded };
            let index = serde_json::json!({
                "session_id":"0123456789ABCDEF", "version":5,
                "world":{"options":{"id":if name == master {"SURVIVAL_TOGETHER"} else {"DST_CAVE"}, "name":"Client world", "desc":"Client description", "location":if name == master {"forest"} else {"cave"}, "overrides":{"day":"onlynight", "task_set":if name == master {"default"} else {"cave_default"}}}},
                "server":{"name":"Client room", "description":"Client world", "game_mode":"survival", "server_language":"zh", "max_players":8, "pvp":true, "online_mode":false, "privacy_type":3, "password":"private-password", "clan":{"id":"123"}, "encode_user_path":encoded},
                "enabled_mods": if name == master { serde_json::json!({"workshop-123":{"enabled":true,"configuration_options":{"difficulty":"hard"}}}) } else { serde_json::json!({}) },
            });
            let save = input.path().join(name).join("save");
            let session = save.join("session/0123456789ABCDEF");
            fs::create_dir_all(&session).unwrap();
            fs::write(
                save.join("shardindex"),
                format!("KLEI     1 return {}", lua::render_literal(&index).unwrap()),
            )
            .unwrap();
            fs::write(session.join("0000000001"), b"world\0\xff").unwrap();
            for (raw, path) in players {
                let player = session.join(if encoded { path } else { raw });
                fs::create_dir(&player).unwrap();
                fs::write(player.join("0000000001"), raw.as_bytes()).unwrap();
                fs::write(player.join("0000000001.meta"), b"player-meta\0\xff").unwrap();
                fs::write(player.join("savelocation"), b"location\0\xff").unwrap();
                fs::write(player.join(".DS_Store"), b"private-cache").unwrap();
            }
            fs::write(save.join("profile"), b"private-cache").unwrap();
        }
        let destination = tempfile::tempdir().unwrap();
        let output = destination.path().join("client");
        let config = import_to(
            ImportSource::Archive(zip_archive(&tree(input.path()))),
            output.clone(),
        )
        .await
        .unwrap()
        .resolved();
        let topology = crate::configuration::discover(&output).unwrap();
        assert_eq!(topology.shards.len(), if caves { 2 } else { 1 });
        assert_eq!(config["settings"]["cluster_name"], "Client room");
        assert_eq!(config["settings"]["cluster_language"], "zh");
        assert_eq!(config["settings"]["offline_cluster"], true);
        assert_eq!(config["settings"]["pvp"], true);
        assert_eq!(config["settings"]["max_players"], 8);
        for shard in topology.shards {
            assert_eq!(shard.name, if shard.master { master } else { secondary });
            let expected = &config["shards"][&shard.name];
            assert_eq!(expected["settings"]["name"], shard.name);
            assert_eq!(expected["settings"]["encode_user_path"], false);
            assert_eq!(expected["level"]["overrides"]["values"]["day"], "onlynight");
            assert_eq!(
                expected["mods"]["entries"]["workshop-123"]["configuration_options"]["difficulty"],
                "hard"
            );
            let index = RoomReader::open(&output)
                .unwrap()
                .read_lua_table(Path::new(&shard.name).join("save/shardindex"))
                .unwrap();
            assert_eq!(index["server"]["encode_user_path"], false);
            assert_eq!(index["server"]["privacy_type"], 0);
            assert!(index["server"].get("password").is_none());
            for (raw, _) in players {
                let player = output
                    .join(&shard.name)
                    .join("save/session/0123456789ABCDEF")
                    .join(raw);
                assert_eq!(fs::read(player.join("0000000001")).unwrap(), raw.as_bytes());
                assert_eq!(
                    fs::read(player.join("0000000001.meta")).unwrap(),
                    b"player-meta\0\xff"
                );
                assert_eq!(
                    fs::read(player.join("savelocation")).unwrap(),
                    b"location\0\xff"
                );
            }
        }
        if caves {
            let exported = contents(
                prepare(
                    &RoomReader::open(&output).unwrap(),
                    7,
                    &ExportOptions {
                        client_compatible: true,
                        ..Default::default()
                    },
                )
                .unwrap()
                .compress(DEFAULT_COMPRESSION_LEVEL)
                .await
                .unwrap(),
            );
            for shard in ["Master", "Caves"] {
                for (raw, encoded) in players {
                    assert_eq!(
                        exported[&format!(
                            "Cluster_7/{shard}/save/session/0123456789ABCDEF/{encoded}/0000000001"
                        )],
                        raw.as_bytes()
                    );
                }
            }
        }
        assert!(
            tree(&output)
                .iter()
                .all(|(name, _)| !name.ends_with(".DS_Store") && !name.ends_with("profile"))
        );
    }
}

#[tokio::test]
async fn client_import_rejects_invalid_and_colliding_player_paths_before_writing() {
    let input = cluster();
    add_secondary(input.path());
    let entries = contents(
        prepare(
            &RoomReader::open(input.path()).unwrap(),
            7,
            &ExportOptions {
                client_compatible: true,
                ..Default::default()
            },
        )
        .unwrap()
        .compress(DEFAULT_COMPRESSION_LEVEL)
        .await
        .unwrap(),
    );
    for (player, expected) in [
        ("KU_ABCDEFG__", "collide"),
        ("A7K1NP32JUC!", "encoded Klei ID"),
    ] {
        let mut entries: Vec<_> = entries.clone().into_iter().collect();
        entries.push((
            format!("Cluster_7/Master/save/session/0123456789ABCDEF/{player}/other-snapshot"),
            b"other-player".to_vec(),
        ));
        let directory = tempfile::tempdir().unwrap();
        let output = directory.path().join("restored");
        let error = import_to(ImportSource::Archive(zip_archive(&entries)), output.clone())
            .await
            .err()
            .unwrap();
        assert!(error.to_string().contains(expected), "{error}");
        assert!(!output.exists());
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
    }
}

#[test]
fn player_path_codec_preserves_case_and_rejects_invalid_ids() {
    for (userid, encoded) in [
        ("KU_00000000", "A7G000000000"),
        ("KU_ABCDEFGH", "A7H8MC6JHT0H"),
        ("KU_WRv6AVc8", "A7K1NP32JUC8"),
        ("KU_abcdefgh", "A7KIB6JQ56LB"),
        ("KU_ABCDEFG_", "A7H8MC6JHT1V"),
    ] {
        assert_eq!(encode_klei_id(userid).unwrap(), encoded);
        assert_eq!(native::decode_klei_id(encoded).unwrap(), userid);
    }
    for invalid in ["short", "A7K1NP32JUC!", "a7k1np32juc8", "000000000000"] {
        assert!(native::decode_klei_id(invalid).is_err());
    }
}

#[tokio::test]
async fn import_rejects_ambiguous_unsafe_and_corrupt_inputs_without_publishing() {
    let fixture = cluster();
    let entries = tree(fixture.path());
    for bad in [
        "../outside",
        "/outside",
        "C:/outside",
        "forest/save/session/X/CON.txt",
        "forest/save/session/X/../outside",
        "..\\outside",
        "\\outside",
    ] {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("result");
        let mut invalid = entries.clone();
        invalid.push((bad.into(), b"bad".to_vec()));
        let result = import_to(ImportSource::Archive(zip_archive(&invalid)), output.clone()).await;
        assert!(result.is_err(), "{bad}");
        assert!(!output.exists());
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }
    for additions in [
        vec![("second/cluster.ini", b"[NETWORK]".as_slice())],
        vec![
            ("forest/save/session/X/Player/a", b"a"),
            ("forest/save/session/X/player/b", b"b"),
        ],
        vec![
            ("forest/save/session/X/entry", b"a"),
            ("forest/save/session/X/entry/b", b"b"),
        ],
        vec![
            ("forest/save/session/X/same", b"a"),
            ("forest\\save\\session\\X\\same", b"b"),
        ],
    ] {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("result");
        let mut invalid = entries.clone();
        invalid.extend(
            additions
                .into_iter()
                .map(|(name, bytes)| (name.into(), bytes.to_vec())),
        );
        assert!(
            import_to(ImportSource::Archive(zip_archive(&invalid)), output.clone())
                .await
                .is_err()
        );
        assert!(!output.exists());
    }
    let root = tempfile::tempdir().unwrap();
    let output = root.path().join("result");
    let mut archive = prepare(&RoomReader::open(fixture.path()).unwrap(), 7, &options())
        .unwrap()
        .compress(DEFAULT_COMPRESSION_LEVEL)
        .await
        .unwrap()
        .into_file();
    archive
        .set_len(archive.metadata().unwrap().len() - 10)
        .unwrap();
    archive.rewind().unwrap();
    assert!(
        import_to(ImportSource::Archive(archive), output.clone())
            .await
            .is_err()
    );
    assert!(!output.exists());
    fs::write(
        fixture.path().join("forest/save/shardindex"),
        "return {server = execute()}",
    )
    .unwrap();
    assert!(
        import_to(ImportSource::Path(fixture.path().into()), output.clone())
            .await
            .is_err()
    );
    assert!(!output.exists());
}

#[tokio::test]
async fn import_preserves_existing_destinations_and_refuses_links() {
    use std::os::unix::fs::symlink;
    let source = cluster();
    let root = tempfile::tempdir().unwrap();
    let output = root.path().join("existing");
    fs::create_dir(&output).unwrap();
    fs::write(output.join("keep"), b"existing").unwrap();
    let attempt = |path| import_to(ImportSource::Path(source.path().into()), path);
    assert!(attempt(output.clone()).await.is_err());
    assert_eq!(tree(&output), vec![("keep".into(), b"existing".to_vec())]);
    let link = root.path().join("link");
    symlink(&output, &link).unwrap();
    assert!(attempt(link.clone()).await.is_err());
    assert!(attempt(link.join("new")).await.is_err());
    assert!(!output.join("new").exists());
    let session = source.path().join("forest/save/session/0123456789ABCDEF");
    symlink(
        source.path().join("cluster_token.txt"),
        session.join("link"),
    )
    .unwrap();
    assert!(attempt(root.path().join("new")).await.is_err());
    assert!(!root.path().join("new").exists());
    assert!(fs::read_dir(root.path()).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".dst-import-")
    }));
}

fn payload_archive() -> ClusterArchive {
    let mut file = memory_file().unwrap();
    file.write_all(b"archive bytes\0\xff").unwrap();
    ClusterArchive::from_file("DST-001-fixture.7z".into(), file).unwrap()
}

#[test]
fn archive_streams_share_memory_with_independent_positions() {
    let mut archive = payload_archive();
    assert!(
        fs::read_link(format!("/proc/self/fd/{}", archive.file.as_raw_fd()))
            .unwrap()
            .to_string_lossy()
            .starts_with("/memfd:dst-archive")
    );
    let mut clone = archive.try_clone().unwrap();
    let mut bytes = Vec::new();
    clone.file.read_to_end(&mut bytes).unwrap();
    assert_eq!(bytes, b"archive bytes\0\xff");
    assert_eq!(archive.file.stream_position().unwrap(), 0);
    let directory = tempfile::tempdir().unwrap();
    let output = directory.path().join("saved.7z");
    archive.save(&output).unwrap();
    assert_eq!(fs::read(&output).unwrap(), bytes);
    assert!(archive.save(&output).is_err());
}

#[tokio::test]
async fn export_reads_an_owned_room_without_service_calls_or_disk_artifacts() {
    let temporary = tempfile::tempdir().unwrap();
    let source = cluster();
    let root = temporary.path().join("rooms");
    let directory = root.join("001");
    fs::create_dir_all(&root).unwrap();
    fs::rename(source.path(), &directory).unwrap();
    let _owner = RoomLock::try_acquire(&directory).unwrap();
    fs::write(directory.join(".dst-control.json"), b"private-agent-state").unwrap();
    let mut host = crate::host::Host::new(root, temporary.path().join("units"));
    host.systemd.executable = "/missing-systemctl".into();
    let archive = export_host(&host, 1, options(), DEFAULT_COMPRESSION_LEVEL)
        .await
        .unwrap();
    assert!(!directory.join(".dst-archives").exists());
    assert!(!directory.join(".dst-server.json").exists());
    assert_eq!(
        fs::read(directory.join(".dst-control.json")).unwrap(),
        b"private-agent-state"
    );
    assert_eq!(
        contents(archive)["001/forest/save/session/0123456789ABCDEF/0000000001"],
        b"world\0\xff"
    );
}

use tokio::io::AsyncWriteExt;

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
            ("forest/modoverrides.lua", b"return {[\"workshop-123\"]={enabled=true,configuration_options={difficulty=\"hard\"}}}"),
            ("forest/save/shardindex", b"KLEI     1 return {session_id=\"0123456789ABCDEF\",world={password=\"world-progress\"},server={password=\"private-index-password\",cluster_key=\"private-key\",clan={id=\"123\"},privacy_type=3,encode_user_path=false},enabled_mods={[\"workshop-123\"]={enabled=true,configuration_options={difficulty=\"hard\"}}}}"),
            ("forest/save/session/0123456789ABCDEF/0000000001", b"world\0\xff"),
            ("forest/save/session/0123456789ABCDEF/0000000001.meta", b"world-meta\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/0000000001", b"player\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/0000000001.meta", b"player-meta\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/savelocation", b"\x81\0\0\0\x01\0\0\0\x02"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/.dst-server.json", b"private-control"),
            ("forest/save/session/0123456789ABCDEF/.dst-maintenance/private", b"private-ignored"),
            ("forest/save/session/0123456789ABCDEF/.world_metadata", b"hidden-world-progress"),
            ("forest/save/recipebook", b"recipe-progress\0\xff"),
            ("forest/save/reforged_achievements_server", b"achievement-progress\0\xff"),
            ("forest/save/reforged_exp_server", b"experience-progress\0\xff"),
            ("forest/save/mod_config_data/mod_worldjump_data_1", b"worldjump-progress\0\xff"),
            ("forest/save/mod_config_data/cache", b"private-cache"),
            ("forest/save/server_temp/data", b"private-temporary"),
            ("forest/save/client_temp/data", b"private-client-cache"),
            ("forest/save/shardindex_time", b"private-timestamp"),
            ("forest/save/modindex", b"private-mod-cache"),
            ("forest/save/boot_modindex", b"private-mod-cache"),
            ("forest/save/profile", b"private-profile"),
            ("forest/save/cached_userid", b"private-user-cache"),
            ("forest/save/event_match_stats/event", b"private-event-stats"),
            ("forest/save/world_presets/preset", b"private-preset-cache"),
            ("forest/backup/world", b"private-backup"),
            ("forest/server_log.txt", b"private-log"),
            ("forest/server_chat_log.txt", b"private-chat-log"),
            ("mods/ugc/content/322330/mod/modmain.lua", b"private-downloaded-mod"),
        ] {
            let path = root.path().join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
    root
}

fn add_secondary(root: &Path) {
    let cluster_ini = root.join("cluster.ini");
    fs::write(
        &cluster_ini,
        fs::read_to_string(&cluster_ini).unwrap().replace(
            "[SHARD]\n",
            "[SHARD]\nshard_enabled = true\nmaster_ip = 127.0.0.1\n",
        ),
    )
    .unwrap();
    for (name, bytes) in [
            ("underground/server.ini", b"[SHARD]\nis_master = false\nname = Below\nid = 2\ncluster_key = private-key\n[NETWORK]\nserver_port = 11000\n[STEAM]\nmaster_server_port = 27017\n[ACCOUNT]\nencode_user_path = true\n".as_slice()),
            ("underground/worldgenoverride.lua", b"return {override_enabled=true,preset=\"DST_CAVE\"}"),
            ("underground/save/shardindex", b"KLEI     1 return {session_id=\"FEDCBA9876543210\",version=5,world={options={location=\"cave\"}},server={encode_user_path=true},enabled_mods={}}"),
            ("underground/save/session/FEDCBA9876543210/0000000002", b"cave-world\0\xff"),
            ("underground/save/session/FEDCBA9876543210/A7H8MC6JHT1V/0000000002", b"cave-player\0\xff"),
            ("underground/save/session/FEDCBA9876543210/A7H8MC6JHT1V/savelocation", b"\x81\0\0\0\x02\0\0\0\x02"),
        ] {
            let path = root.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
}

fn options() -> ExportOptions {
    ExportOptions {
        room_id: Some("001".into()),
        ..Default::default()
    }
}

fn contents(archive: ClusterArchive) -> BTreeMap<String, Vec<u8>> {
    let mut reader =
        sevenz_rust2::ArchiveReader::new(archive.file, sevenz_rust2::Password::empty()).unwrap();
    assert!(reader.archive().blocks.iter().all(|block| {
        block.coders.iter().any(|coder| {
            coder.encoder_method_id() == sevenz_rust2::EncoderMethod::ID_LZMA2
                // Preset 9 uses a 64 MiB dictionary, encoded as property 28.
                && coder.properties() == [28]
        })
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
    for (client, source_encoded) in [(false, false), (true, false), (false, true), (true, true)] {
        let root = cluster();
        if client {
            add_secondary(root.path());
        }
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
        let lock = RoomLock::try_acquire(root.path()).unwrap();
        let options = if client {
            ExportOptions {
                client_compatible: true,
                ..Default::default()
            }
        } else {
            options()
        };
        let source_before = tree(root.path());
        let prepared = prepare(&lock, 1, &options).unwrap();
        let configuration = ClusterConfig::from_files(&prepared.entries)
            .unwrap()
            .resolved();
        let key = configuration["settings"]["cluster_key"].as_str().unwrap();
        assert_eq!(key.len(), 64);
        assert!(key.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert!(
            configuration["shards"]
                .as_object()
                .unwrap()
                .values()
                .all(|shard| shard["settings"]["cluster_key"].is_null())
        );
        let repeated = prepare(&lock, 1, &options).unwrap();
        let repeated = ClusterConfig::from_files(&repeated.entries)
            .unwrap()
            .resolved();
        assert_ne!(
            configuration["settings"]["cluster_key"],
            repeated["settings"]["cluster_key"]
        );
        assert_eq!(tree(root.path()), source_before);
        // Compression uses the bytes already captured, even if the game saves again.
        fs::write(
            root.path()
                .join("forest/save/session/0123456789ABCDEF/0000000001"),
            b"new world",
        )
        .unwrap();
        let files = contents(prepared.compress(DEFAULT_COMPRESSION_LEVEL).await.unwrap());
        assert_eq!(fs::read(index_path).unwrap(), before);
        assert_eq!(files.len(), if client { 24 } else { 17 });
        assert!(
            files
                .values()
                .all(|bytes| !String::from_utf8_lossy(bytes).contains("private-"))
        );
        let (directory, shard) = if client {
            ("Cluster_1", "Master")
        } else {
            ("001", "forest")
        };
        let prefix = format!("{directory}/{shard}");
        let base = format!("{prefix}/save/session/0123456789ABCDEF");
        assert_eq!(&files[&format!("{base}/0000000001")], b"world\0\xff");
        let player = if client || source_encoded {
            encode_klei_id("KU_ABCDEFG_").unwrap()
        } else {
            "KU_ABCDEFG__".into()
        };
        assert_eq!(
            &files[&format!("{base}/{player}/0000000001")],
            b"player\0\xff"
        );
        assert_eq!(
            &files[&format!("{base}/{player}/savelocation")],
            b"\x81\0\0\0\x01\0\0\0\x02"
        );
        assert_eq!(
            &files[&format!("{base}/{player}/0000000001.meta")],
            b"player-meta\0\xff"
        );
        assert_eq!(
            &files[&format!("{prefix}/save/mod_config_data/mod_worldjump_data_1")],
            b"worldjump-progress\0\xff"
        );
        assert_eq!(
            &files[&format!("{prefix}/save/reforged_exp_server")],
            b"experience-progress\0\xff"
        );
        let index = lua::parse_return_table(
            std::str::from_utf8(&files[&format!("{prefix}/save/shardindex")]).unwrap(),
        )
        .unwrap();
        assert_eq!(
            index["server"],
            serde_json::json!({"privacy_type":0,"encode_user_path":client || source_encoded})
        );
        assert_eq!(index["world"]["password"], "world-progress");
        assert_eq!(index["enabled_mods"]["workshop-123"]["enabled"], true);
        assert_eq!(
            index["enabled_mods"]["workshop-123"]["configuration_options"]["difficulty"],
            "hard"
        );
        let mods = lua::parse_return_table(
            std::str::from_utf8(&files[&format!("{prefix}/modoverrides.lua")]).unwrap(),
        )
        .unwrap();
        assert_eq!(mods, index["enabled_mods"]);
        let ini = String::from_utf8_lossy(&files[&format!("{directory}/cluster.ini")]);
        assert!(!ini.contains("steam_group"));
        assert!(ini.contains(&format!("cluster_key = {key}")));
        let ini = String::from_utf8_lossy(&files[&format!("{prefix}/server.ini")]);
        assert!(ini.contains(&format!("encode_user_path = {}", client || source_encoded)));
        if client {
            assert!(files.keys().all(|path| path.starts_with("Cluster_1/")));
            assert!(
                !files
                    .keys()
                    .any(|path| path.contains("/forest/") || path.contains("/underground/"))
            );
            let cave = "Cluster_1/Caves/save/session/FEDCBA9876543210";
            assert_eq!(&files[&format!("{cave}/0000000002")], b"cave-world\0\xff");
            assert_eq!(
                &files[&format!("{cave}/A7H8MC6JHT1V/0000000002")],
                b"cave-player\0\xff"
            );
            assert_eq!(
                &files[&format!("{cave}/A7H8MC6JHT1V/savelocation")],
                b"\x81\0\0\0\x02\0\0\0\x02"
            );
            assert!(files.contains_key("Cluster_1/Caves/worldgenoverride.lua"));
        }
    }
    assert_eq!(encode_klei_id("KU_ABCDEFG_").unwrap(), "A7H8MC6JHT1V");
}

#[tokio::test]
async fn customized_configuration_preserves_topology_and_native_source_encoding() {
    let root = cluster();
    add_secondary(root.path());
    let lock = RoomLock::try_acquire(root.path()).unwrap();
    let mut value = ClusterConfig::load(&lock).unwrap().into_value();
    value["settings"]["cluster_name"] = "Customized archive".into();
    value["shards"]["forest"]["settings"]["encode_user_path"] = true.into();
    let mut options = ExportOptions {
        configuration: Some(ClusterConfig::from_value(value.clone()).unwrap()),
        ..options()
    };
    let error = prepare(&lock, 1, &options).err().unwrap();
    assert!(
        error
            .to_string()
            .contains("preserve source player path encoding")
    );
    options.client_compatible = true;
    options.room_id = None;
    let files = contents(
        prepare(&lock, 1, &options)
            .unwrap()
            .compress(DEFAULT_COMPRESSION_LEVEL)
            .await
            .unwrap(),
    );
    assert!(
        String::from_utf8_lossy(&files["Cluster_1/cluster.ini"]).contains("Customized archive")
    );
    assert!(
        files.contains_key(
            "Cluster_1/Master/save/session/0123456789ABCDEF/A7H8MC6JHT1V/savelocation"
        )
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
    let error = prepare(&lock, 1, &options).err().unwrap();
    assert!(error.to_string().contains("source shard topology"));

    fs::remove_file(root.path().join("mods/modsettings.lua")).unwrap();
    fs::remove_file(root.path().join("mods/dedicated_server_mods_setup.lua")).unwrap();
    let prepared = prepare(
        &lock,
        1,
        &ExportOptions {
            room_id: Some("001".into()),
            ..Default::default()
        },
    )
    .unwrap();
    assert!(
        prepared
            .entries
            .contains_key(Path::new("mods/modsettings.lua"))
    );
    assert!(
        prepared
            .entries
            .contains_key(Path::new("mods/dedicated_server_mods_setup.lua"))
    );
}

#[test]
fn rejects_symlinks_special_files_collisions_unsafe_paths_and_incomplete_indexes() {
    use std::os::unix::fs::symlink;
    for violation in [
        "symlink",
        "directory_symlink",
        "fifo",
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
                let path =
                    std::ffi::CString::new(session.join("pipe").as_os_str().as_bytes()).unwrap();
                assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
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
        let lock = RoomLock::try_acquire(root.path()).unwrap();
        assert!(prepare(&lock, 1, &options()).is_err(), "{violation}");
    }
}

#[test]
fn client_exports_require_two_shards_and_reject_ambiguous_destinations() {
    let root = cluster();
    let reader = RoomReader::open(root.path()).unwrap();
    let mut options = ExportOptions {
        client_compatible: true,
        ..Default::default()
    };
    assert!(
        prepare(&reader, 7, &options)
            .err()
            .unwrap()
            .to_string()
            .contains("exactly two shards")
    );
    add_secondary(root.path());
    options.room_id = Some("custom".into());
    assert!(
        prepare(&reader, 7, &options)
            .err()
            .unwrap()
            .to_string()
            .contains("room_id cannot be overridden")
    );
    options.room_id = None;
    fs::remove_file(root.path().join("cluster_token.txt")).unwrap();
    assert_eq!(prepare(&reader, 7, &options).unwrap().room_id, "Cluster_7");

    let collision = root
        .path()
        .join("forest/save/session/0123456789ABCDEF/A7H8MC6JHT1V");
    fs::create_dir(&collision).unwrap();
    fs::write(collision.join("different-snapshot"), b"second player").unwrap();
    assert!(
        prepare(&reader, 7, &options)
            .err()
            .unwrap()
            .to_string()
            .contains("player save directories collide")
    );
    fs::remove_dir_all(collision).unwrap();

    fs::create_dir(root.path().join("third")).unwrap();
    fs::write(root.path().join("third/server.ini"), "[SHARD]\nis_master = false\nname = Third\nid = 3\ncluster_key = private-key\n[NETWORK]\nserver_port = 11001\n[STEAM]\nmaster_server_port = 27018\n").unwrap();
    assert!(
        prepare(&reader, 7, &options)
            .err()
            .unwrap()
            .to_string()
            .contains("exactly two shards")
    );
    options.client_compatible = false;
    options.room_id = Some("007".into());
    assert!(prepare(&reader, 7, &options).is_ok());
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
                    if let Some(offset) = buffer.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
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
                let (status, body) = if first.starts_with("POST ") && first.contains("uploads") {
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
        let mut file = memory_file().unwrap();
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
                    format!("{:#}", outcome.unwrap().unwrap_err()).contains("upload archive part")
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
