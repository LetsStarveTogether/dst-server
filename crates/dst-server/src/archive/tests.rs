use super::*;
use crate::{files::RoomLock, lua};
use ::zip::{ZipWriter, write::SimpleFileOptions};
use std::io::{Read, Write};
use std::time::Duration;
use tempfile::TempDir;

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
        let configuration = import_cluster(input, output.clone(), ImportOptions { from_client })
            .await
            .unwrap();
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
        let prefix = output.join("forest");
        assert_eq!(
            fs::read(prefix.join("save/session/0123456789ABCDEF/000000001")).unwrap(),
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
            resolved["shards"]["forest"]["mods"]["entries"]["workshop-123"]["configuration_options"]
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
                resolved["shards"]["cave"]["settings"]["encode_user_path"],
                false
            );
            assert_eq!(
                fs::read(output.join("cave/save/session/FEDCBA9876543210/KU_ABCDEFG__/000000002"))
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
async fn client_import_rebuilds_configuration_and_restores_native_names_and_player_paths() {
    let players = [
        ("KU_ABCDEFGH_", "A7H8MC6JHT0H"),
        ("KU_abcdefgh_", "A7KIB6JQ56LB"),
        ("KU_WRv6AVc8_", "A7K1NP32JUC8"),
        ("KU_ABCDEFG__", "A7H8MC6JHT1V"),
        ("1234567890__", "1234567890__"),
    ];
    for (caves, encoded) in [(false, false), (false, true), (true, false), (true, true)] {
        let input = tempfile::tempdir().unwrap();
        for name in if caves {
            vec!["Master", "Caves"]
        } else {
            vec!["Master"]
        } {
            let encoded = if name == "Master" { encoded } else { !encoded };
            let index = serde_json::json!({
                "session_id":"0123456789ABCDEF", "version":5,
                "world":{"options":{"id":if name == "Master" {"SURVIVAL_TOGETHER"} else {"DST_CAVE"}, "name":"Client world", "desc":"Client description", "location":if name == "Master" {"forest"} else {"cave"}, "overrides":{"day":"onlynight", "task_set":if name == "Master" {"default"} else {"cave_default"}}}},
                "server":{"name":"Client room", "description":"Client world", "game_mode":"survival", "max_players":8, "pvp":true, "online_mode":false, "privacy_type":3, "password":"private-password", "clan":{"id":"123"}, "encode_user_path":encoded},
                "enabled_mods": if name == "Master" { serde_json::json!({"workshop-123":{"enabled":true,"configuration_options":{"difficulty":"hard"}}}) } else { serde_json::json!({}) },
            });
            let save = input.path().join(name).join("save");
            let session = save.join("session/0123456789ABCDEF");
            fs::create_dir_all(&session).unwrap();
            fs::write(
                save.join("shardindex"),
                format!("KLEI     1 return {}", lua::render_literal(&index).unwrap()),
            )
            .unwrap();
            fs::write(session.join("000000001"), b"world\0\xff").unwrap();
            for (raw, path) in players {
                let player = session.join(if encoded { path } else { raw });
                fs::create_dir(&player).unwrap();
                fs::write(player.join("000000001"), raw.as_bytes()).unwrap();
                fs::write(player.join("000000001.meta"), b"player-meta\0\xff").unwrap();
                fs::write(player.join("savelocation"), b"location\0\xff").unwrap();
                fs::write(player.join(".DS_Store"), b"private-cache").unwrap();
            }
            fs::write(save.join("profile"), b"private-cache").unwrap();
        }
        let destination = tempfile::tempdir().unwrap();
        let output = destination.path().join("client");
        let config = import_cluster(
            ImportSource::Archive(zip_archive(&tree(input.path()))),
            output.clone(),
            ImportOptions { from_client: true },
        )
        .await
        .unwrap()
        .resolved();
        let topology = crate::configuration::discover(&output).unwrap();
        assert_eq!(topology.shards.len(), if caves { 2 } else { 1 });
        assert_eq!(config["settings"]["cluster_name"], "Client room");
        assert_eq!(config["settings"]["offline_cluster"], true);
        assert_eq!(config["settings"]["pvp"], true);
        assert_eq!(config["settings"]["max_players"], 8);
        assert!(!output.join("Master").exists());
        assert!(!output.join("Caves").exists());
        for shard in topology.shards {
            assert_eq!(shard.name, if shard.master { "forest" } else { "cave" });
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
                assert_eq!(fs::read(player.join("000000001")).unwrap(), raw.as_bytes());
                assert_eq!(
                    fs::read(player.join("000000001.meta")).unwrap(),
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
                            "Cluster_7/{shard}/save/session/0123456789ABCDEF/{encoded}/000000001"
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
        let error = import_cluster(
            ImportSource::Archive(zip_archive(&entries)),
            output.clone(),
            ImportOptions { from_client: true },
        )
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
        "forest\\save\\world",
    ] {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("result");
        let mut invalid = entries.clone();
        invalid.push((bad.into(), b"bad".to_vec()));
        let result = import_cluster(
            ImportSource::Archive(zip_archive(&invalid)),
            output.clone(),
            ImportOptions::default(),
        )
        .await;
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
            import_cluster(
                ImportSource::Archive(zip_archive(&invalid)),
                output.clone(),
                ImportOptions::default()
            )
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
        import_cluster(
            ImportSource::Archive(archive),
            output.clone(),
            ImportOptions::default()
        )
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
        import_cluster(
            ImportSource::Path(fixture.path().into()),
            output.clone(),
            ImportOptions::default()
        )
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
    let attempt = |path| {
        import_cluster(
            ImportSource::Path(source.path().into()),
            path,
            ImportOptions::default(),
        )
    };
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
        contents(archive)["001/forest/save/session/0123456789ABCDEF/000000001"],
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
            ("forest/save/session/0123456789ABCDEF/000000001", b"world\0\xff"),
            ("forest/save/session/0123456789ABCDEF/000000001.meta", b"world-meta\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/000000001", b"player\0\xff"),
            ("forest/save/session/0123456789ABCDEF/KU_ABCDEFG__/000000001.meta", b"player-meta\0\xff"),
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
            ("underground/save/session/FEDCBA9876543210/000000002", b"cave-world\0\xff"),
            ("underground/save/session/FEDCBA9876543210/A7H8MC6JHT1V/000000002", b"cave-player\0\xff"),
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
        let prepared = prepare(&lock, 1, &options).unwrap();
        // Compression uses the bytes already captured, even if the game saves again.
        fs::write(
            root.path()
                .join("forest/save/session/0123456789ABCDEF/000000001"),
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
        assert_eq!(&files[&format!("{base}/000000001")], b"world\0\xff");
        let player = if client || source_encoded {
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
            &files[&format!("{base}/{player}/000000001.meta")],
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
        assert!(!ini.contains("cluster_key"));
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
            assert_eq!(&files[&format!("{cave}/000000002")], b"cave-world\0\xff");
            assert_eq!(
                &files[&format!("{cave}/A7H8MC6JHT1V/000000002")],
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
