import os
import re
import struct
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, BinaryIO
from weakref import ref

import pytest
from obstore.exceptions import PermissionDeniedError
from py7zr import SevenZipFile
from pydantic import SecretStr, ValidationError

from dst_server import archive
from dst_server.configuration.files import load_lua_table
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardSettings,
)
from dst_server.configuration.presets import FOREST_CAVES
from dst_server.klei_id import encode_klei_id


def test_export_releases_file_catalog_before_yielding(
    saved_cluster: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FileMap(dict[Path, tuple[Path, tuple[int, ...]]]):
        pass

    catalogs: list[ref[FileMap]] = []
    scan = archive._save_files

    def track(
        directory: Path, configuration: ClusterConfig, encode_user_path: bool
    ) -> FileMap:
        files = FileMap(scan(directory, configuration, encode_user_path))
        catalogs.append(ref(files))
        return files

    monkeypatch.setattr(archive, "_save_files", track)
    with archive.export_cluster(saved_cluster) as exported:
        assert exported.stream.read(6) == b"7z\xbc\xaf\x27\x1c"
        assert len(catalogs) == 2
        assert all(catalog() is None for catalog in catalogs)


@pytest.fixture
def saved_cluster(tmp_path: Path) -> Path:
    root = tmp_path / "001"
    configuration = FOREST_CAVES.build(
        token=SecretStr("private-token"),
        cluster_key=SecretStr("private-shard-key"),
        settings=ClusterSettings(
            cluster_password=SecretStr("private-password"),
            steam_group_id=12345678,
            steam_group_only=True,
            steam_group_admins=True,
        ),
    )
    configuration.replace(
        shards={
            name: shard.replace(
                settings=shard.settings.replace(
                    cluster_key=configuration.settings.cluster_key,
                    encode_user_path=False,
                )
            )
            for name, shard in configuration.shards.items()
        }
    ).save(root)
    for shard in ("forest", "cave"):
        session = root / shard / "save/session/0123456789ABCDEF"
        session.mkdir(parents=True)
        (session / "0000000001").write_bytes(b"world\x00\xff")
        (session / "0000000001.meta").write_bytes(b"world metadata")
        player = session / "KU_ABCDEFG__"
        player.mkdir()
        (player / "0000000001").write_bytes(b"player\x00\xff")
        (player / "savelocation").write_bytes(b"\x81" + struct.pack(">II", 1, 2))
        (root / shard / "save/shardindex").write_text(
            'KLEI     1 return {session_id="0123456789ABCDEF",world={},'
            'server={password="private-index-password",encode_user_path=false,'
            'clan={id="12345678",only=true,admin=true},privacy_type=3}}',
            encoding="utf-8",
        )
    for name in (
        "recipebook",
        "reforged_achievements_server",
        "mod_config_data/mod_worldjump_data_1",
    ):
        progress = root / "forest/save" / name
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_bytes(b"persistent progress\x00\xff")
    for name in (
        "adminlist.txt",
        "whitelist.txt",
        "blocklist.txt",
        "mainlist.txt",
        "mods/workshop-123/modmain.lua",
        "mods/ugc/content/322330/123/modmain.lua",
        "forest/backup/server_log.txt",
        "forest/server_log.txt",
        "forest/server_chat_log.txt",
        "forest/save/server_temp/data",
        "forest/save/client_temp/data",
        "forest/save/saveindex",
        "forest/save/shardindex_time",
        "forest/save/cached_userid",
        "forest/save/modindex",
        "forest/save/mod_config_data/cache",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded", encoding="utf-8")
    os.mkfifo(root / "console")
    return root


@pytest.mark.parametrize("encode_user_path", [True, False])
@pytest.mark.parametrize("source_encoded", [True, False])
def test_export_round_trip_and_cleanup(
    saved_cluster: Path, tmp_path: Path, encode_user_path: bool, source_encoded: bool
) -> None:
    if source_encoded:
        configuration = ClusterConfig.load(saved_cluster)
        configuration.replace(
            shards={
                name: shard.replace(
                    settings=shard.settings.replace(encode_user_path=True)
                )
                for name, shard in configuration.shards.items()
            }
        ).save(saved_cluster)
        for shard in configuration.shards:
            session = saved_cluster / shard / "save/session/0123456789ABCDEF"
            (session / "KU_ABCDEFG__").rename(session / encode_klei_id("KU_ABCDEFG_"))
            index = saved_cluster / shard / "save/shardindex"
            index.write_bytes(archive._export_shard_index(index.read_bytes()))
    encoded = source_encoded or encode_user_path
    original = {
        path.relative_to(saved_cluster): path.read_bytes()
        for path in saved_cluster.rglob("*")
        if path.is_file()
    }
    configuration = ClusterConfig.load(saved_cluster)
    destination = tmp_path / "extracted"
    with archive.export_cluster(
        saved_cluster, configuration=configuration, encode_user_path=encode_user_path
    ) as exported:
        assert re.fullmatch(r"DST-001-\d{8}T\d{6}Z\.7z", exported.filename)
        assert exported.stream.read(6) == b"7z\xbc\xaf\x27\x1c"
        exported.stream.seek(0)
        with SevenZipFile(exported.stream) as compressed:
            assert compressed.archiveinfo().method_names == ["ZStandard"]
            compressed.extractall(destination)
        assert not exported.stream.closed
    assert exported.stream.closed
    root = destination / "001"
    player = encode_klei_id("KU_ABCDEFG_") if encoded else "KU_ABCDEFG__"
    names = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    assert names == {
        "cluster.ini",
        "mods/modsettings.lua",
        "mods/dedicated_server_mods_setup.lua",
        "forest/save/recipebook",
        "forest/save/reforged_achievements_server",
        "forest/save/mod_config_data/mod_worldjump_data_1",
        "forest/save/shardindex",
        "cave/save/shardindex",
        *(
            f"{shard}/{name}"
            for shard in ("forest", "cave")
            for name in ("server.ini", "worldgenoverride.lua", "modoverrides.lua")
        ),
        *(
            f"{shard}/save/session/0123456789ABCDEF/{name}"
            for shard in ("forest", "cave")
            for name in (
                "0000000001",
                "0000000001.meta",
                f"{player}/0000000001",
                f"{player}/savelocation",
            )
        ),
    }
    for path in root.rglob("*"):
        if path.is_file():
            assert b"private-" not in path.read_bytes()
    settings = ClusterSettings.load(root / "cluster.ini")
    assert settings.cluster_password is None
    assert settings.cluster_key is None
    assert settings.steam_group_id == 0
    assert settings.steam_group_only is False
    assert settings.steam_group_admins is False
    assert settings.model_fields_set.isdisjoint({
        "steam_group_id",
        "steam_group_only",
        "steam_group_admins",
    })
    assert "[STEAM]" not in (root / "cluster.ini").read_text(encoding="utf-8")
    for shard in ("forest", "cave"):
        shard_settings = ShardSettings.load(root / shard / "server.ini")
        assert shard_settings.encode_user_path is encoded
        assert shard_settings.cluster_key is None
        index = load_lua_table(root / shard / "save/shardindex", "test")
        assert index["session_id"] == "0123456789ABCDEF"
        assert index["server"] == {"encode_user_path": encoded, "privacy_type": 0}
        base = root / shard / "save/session/0123456789ABCDEF"
        assert (base / "0000000001").read_bytes() == b"world\x00\xff"
        assert (base / player / "0000000001").read_bytes() == b"player\x00\xff"
        assert (base / player / "savelocation").read_bytes() == b"\x81" + struct.pack(
            ">II", 1, 2
        )
    progress = root / "forest/save/mod_config_data/mod_worldjump_data_1"
    assert progress.read_bytes() == b"persistent progress\x00\xff"
    assert original == {
        path.relative_to(saved_cluster): path.read_bytes()
        for path in saved_cluster.rglob("*")
        if path.is_file()
    }


def test_export_generates_keys_only_when_recipient_saves(
    saved_cluster: Path, tmp_path: Path
) -> None:
    with (
        archive.export_cluster(saved_cluster) as exported,
        SevenZipFile(exported.stream) as compressed,
    ):
        compressed.extractall(tmp_path / "extracted")
    root = tmp_path / "extracted/001"
    (root / "cluster_token.txt").write_text("recipient-token\n", encoding="utf-8")
    recipient = ClusterConfig.load(root)
    assert recipient.settings.cluster_key is None
    assert all(
        shard.settings.cluster_key is None for shard in recipient.shards.values()
    )
    keys = set()
    for name in ("recipient-a", "recipient-b"):
        destination = tmp_path / name
        recipient.save(destination)
        saved = ClusterConfig.load(destination)
        key = saved.settings.cluster_key
        assert key is not None
        assert key.get_secret_value() != "private-shard-key"
        assert saved.token == SecretStr("recipient-token")
        keys.add(key.get_secret_value())
        recipient.save(destination)
        assert ClusterSettings.load(destination / "cluster.ini").cluster_key == key
    assert len(keys) == 2
    assert recipient.settings.cluster_key is None


@pytest.mark.parametrize("source_encoded", [True, False])
def test_export_uses_source_encoding_for_player_paths(
    saved_cluster: Path, monkeypatch: pytest.MonkeyPatch, source_encoded: bool
) -> None:
    configuration = ClusterConfig.load(saved_cluster)
    configuration.replace(
        shards={
            name: shard.replace(
                settings=shard.settings.replace(encode_user_path=source_encoded)
            )
            for name, shard in configuration.shards.items()
        }
    ).save(saved_cluster)
    configuration = configuration.replace(
        shards={
            name: shard.replace(
                settings=shard.settings.replace(encode_user_path=not source_encoded)
            )
            for name, shard in configuration.shards.items()
        }
    )

    def encode(userid: str) -> str:
        assert not source_encoded, "already-encoded shards must preserve player paths"
        return encode_klei_id(userid)

    monkeypatch.setattr(archive, "encode_klei_id", encode)
    with (
        archive.export_cluster(saved_cluster, configuration=configuration) as exported,
        SevenZipFile(exported.stream) as compressed,
    ):
        players = {
            name for name in compressed.getnames() if name.endswith("/savelocation")
        }
    player = "KU_ABCDEFG__" if source_encoded else encode_klei_id("KU_ABCDEFG_")
    assert players == {
        f"001/{shard}/save/session/0123456789ABCDEF/{player}/savelocation"
        for shard in configuration.shards
    }


def test_export_rejects_player_directory_collision(saved_cluster: Path) -> None:
    encoded = (
        saved_cluster
        / "forest/save/session/0123456789ABCDEF"
        / encode_klei_id("KU_ABCDEFG_")
    )
    encoded.mkdir()
    (encoded / "different-snapshot").write_bytes(b"must not merge two player saves")
    with (
        pytest.raises(ValueError, match="directories collide"),
        archive.export_cluster(saved_cluster),
    ):
        pytest.fail("colliding player directories were exported")


@pytest.mark.parametrize("special", ["symlink", "fifo"])
def test_export_rejects_special_save_entries(saved_cluster: Path, special: str) -> None:
    target = saved_cluster / "forest/save/session/unsafe"
    if special == "symlink":
        target.symlink_to(saved_cluster / "cluster_token.txt")
    else:
        os.mkfifo(target)
    with (
        pytest.raises(ValueError, match="regular file"),
        archive.export_cluster(saved_cluster),
    ):
        pytest.fail("special save entry was exported")


def test_export_closes_temporary_file_on_failures(
    saved_cluster: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = archive.TemporaryFile(mode="w+b")
    monkeypatch.setattr(archive, "TemporaryFile", lambda **_: temporary)
    writef = SevenZipFile.writef

    def change_save(self: SevenZipFile, bio: BinaryIO, arcname: str) -> None:
        writef(self, bio, arcname)
        with (saved_cluster / "forest/save/session/0123456789ABCDEF/0000000001").open(
            "ab"
        ) as stream:
            stream.write(b"changed")

    monkeypatch.setattr(SevenZipFile, "writef", change_save)
    with (
        pytest.raises(RuntimeError, match="changed during export"),
        archive.export_cluster(saved_cluster),
    ):
        pytest.fail("changing saves were exported")
    assert temporary.closed


def test_consumer_failure_closes_export(saved_cluster: Path) -> None:
    message = "consumer failed"
    with (
        pytest.raises(RuntimeError, match="consumer"),
        archive.export_cluster(saved_cluster) as exported,
    ):
        raise RuntimeError(message)
    assert exported.stream.closed


@pytest.mark.parametrize(
    ("object_prefix", "url_prefix"),
    [
        ("", None),
        ("rooms/exports/", "https://downloads.example.test/"),
        ("private/room-", "https://public.example.test/download?name="),
    ],
)
@pytest.mark.parametrize("configuration_source", ["environment", "explicit", "mixed"])
def test_upload_configuration_and_stream_cleanup(
    saved_cluster: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_prefix: str,
    url_prefix: str | None,
    configuration_source: str,
) -> None:
    requests: list[tuple[str, bytes, dict[str, str]]] = []
    fail_upload = False

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((
                self.path,
                body,
                {key.lower(): value for key, value in self.headers.items()},
            ))
            self.send_response(403 if fail_upload else 200)
            self.send_header("Content-Length", "0")
            self.send_header("ETag", '"test-etag"')
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    for name in os.environ:
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        environment = {
            "AWS_ENDPOINT": endpoint,
            "AWS_BUCKET": "archive-test",
            "AWS_ACCESS_KEY_ID": "test-access",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
            "AWS_SESSION_TOKEN": "test-session",
            "AWS_REGION": "unused-region",
        }
        settings: dict[str, Any] = {}
        region = "auto"
        if configuration_source != "environment":
            region = "test-region"
            settings = {
                "endpoint": endpoint,
                "bucket": "archive-test",
                "region": region,
                "access_key_id": SecretStr("test-access"),
                "secret_access_key": SecretStr("test-secret"),
                "session_token": SecretStr("test-session"),
            }
            environment = (
                {
                    **dict.fromkeys(environment, "unused-value"),
                    "AWS_ENDPOINT": "http://127.0.0.1:1",
                    "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:1",
                }
                if configuration_source == "mixed"
                else {}
            )
        monkeypatch.setenv("AWS_ALLOW_HTTP", "true")
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with archive.export_cluster(saved_cluster) as exported:
                expected = exported.stream.read()
                exported.stream.seek(6)
                first = exported.upload(
                    object_prefix=object_prefix, url_prefix=url_prefix, **settings
                )
                monkeypatch.setenv("AWS_SESSION_TOKEN", "test-session")
                settings["session_token"] = None
                second = exported.upload(
                    object_prefix=object_prefix, url_prefix=url_prefix, **settings
                )
                assert first == second
                for result in (first, second):
                    assert result.key == object_prefix + exported.filename
                    assert result.url == (
                        None
                        if url_prefix is None
                        else url_prefix + result.key.rsplit("/", 1)[-1]
                    )
                assert [path for path, _, _ in requests] == [
                    f"/archive-test/{result.key}" for result in (first, second)
                ]
                for invalid in (
                    {"object_prefix": 123},
                    {"url_prefix": False},
                    {"object_prefix": "/private/"},
                    {"access_key_id": "private-raw-key"},
                    {"secret_access_key": "private-raw-secret"},
                    {"session_token": "private-raw-token"},
                ):
                    with pytest.raises(ValidationError) as error:
                        exported.upload(**invalid)  # ty: ignore[invalid-argument-type]
                    assert "private-raw-" not in str(error.value)
                assert len(requests) == 2
                assert not exported.stream.closed
            assert exported.stream.closed
            fail_upload = True
            with (
                pytest.raises(PermissionDeniedError, match="403"),
                archive.export_cluster(saved_cluster) as failed,
            ):
                failed.upload(
                    object_prefix=object_prefix, url_prefix=url_prefix, **settings
                )
            assert failed.stream.closed
            assert len(requests) == 3
            assert [body for _, body, _ in requests[:2]] == [expected, expected]
            for _, body, headers in requests:
                assert body.startswith(b"7z\xbc\xaf\x27\x1c")
                assert headers["content-type"] == "application/x-7z-compressed"
                assert re.search(
                    rf"Credential=test-access/\d{{8}}/{region}/s3/aws4_request",
                    headers["authorization"],
                )
                assert headers["x-amz-security-token"] == "test-session"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize(
    "name", [r"..\..\outside", "snapshot:stream", "CON", "snapshot."]
)
def test_export_rejects_unsafe_windows_paths(saved_cluster: Path, name: str) -> None:
    (saved_cluster / "forest/save/session" / name).write_bytes(b"unsafe")
    with (
        pytest.raises(ValueError, match="unsafe on Windows"),
        archive.export_cluster(saved_cluster),
    ):
        pytest.fail("unsafe archive name was exported")


def test_export_rejects_configuration_that_loses_saved_shards(
    saved_cluster: Path,
) -> None:
    configuration = ClusterConfig.load(saved_cluster)
    subset = configuration.replace(shards={"forest": configuration.shards["forest"]})
    with (
        pytest.raises(ValueError, match="shard topology"),
        archive.export_cluster(saved_cluster, configuration=subset),
    ):
        pytest.fail("an existing shard was silently omitted")


def test_export_rejects_inconsistent_preserved_encoding(saved_cluster: Path) -> None:
    configuration = ClusterConfig.load(saved_cluster)
    changed = configuration.replace(
        shards={
            name: shard.replace(settings=shard.settings.replace(encode_user_path=True))
            for name, shard in configuration.shards.items()
        }
    )
    with (
        pytest.raises(ValueError, match=r"preserve.*encoding"),
        archive.export_cluster(
            saved_cluster, configuration=changed, encode_user_path=False
        ),
    ):
        pytest.fail("player files were exported with incompatible settings")


@pytest.mark.parametrize(
    ("convert", "original", "expected", "privacy"),
    [
        (True, False, True, None),
        (True, False, True, 0),
        (True, True, True, 1),
        (False, False, False, 2),
        (False, True, True, 3),
    ],
)
def test_export_shard_index_preserves_world_and_removes_credentials(
    tmp_path: Path, convert: bool, original: bool, expected: bool, privacy: int | None
) -> None:
    source = tmp_path / "shardindex"
    privacy_field = "" if privacy is None else f"privacy_type={privacy},"
    source.write_text(
        "KLEI     1 return {\n"
        'version=5, session_id="0123456789ABCDEF",\n'
        'world={options={overrides={day="default"},name="世界"}},\n'
        'enabled_mods={ ["workshop-1"]={enabled=true,configuration_options={\n'
        'password="mod setting", values={1,2,3}}}},\n'
        "server={\n"
        f"encode_user_path={str(original).lower()},\n"
        f"{privacy_field}\n"
        'password="private-password", cluster_password="private-cluster-password",\n'
        'cluster_key="private-cluster-key", cluster_token="private-cluster-token",\n'
        'token="private-token", online_mode=true, name="Room",\n'
        'clan={id="123",only=true,admin=true}}}\n',
        encoding="utf-8",
    )
    original_bytes = source.read_bytes()
    before = load_lua_table(source, "test shard index")

    exported = archive._export_shard_index(
        source.read_bytes(), encode_user_path=convert
    )

    assert source.read_bytes() == original_bytes
    assert exported.startswith(b"KLEI     1 return ")
    assert b"private-" not in exported
    target = tmp_path / "exported"
    target.write_bytes(exported)
    after = load_lua_table(target, "test exported shard index")
    assert after == before | {
        "server": {
            "encode_user_path": expected,
            "online_mode": True,
            "name": "Room",
            **(
                {}
                if privacy is None
                else {"privacy_type": 0 if privacy == 3 else privacy}
            ),
        }
    }


@pytest.mark.parametrize(
    "contents",
    [
        b"KLEI     2 \x00\xffcompressed",
        b"KLEI     1 return {server=",
        b"KLEI     1 return {world={},server=false}",
        b"KLEI     1 return {world={}}",
        b'KLEI     1 return {server={},world=require("untrusted")}',
        b"KLEI     1 return {server={},server={}}",
    ],
)
def test_export_shard_index_rejects_unreadable_or_executable_data(
    tmp_path: Path, contents: bytes
) -> None:
    source = tmp_path / "shardindex"
    source.write_bytes(contents)

    with pytest.raises(ValueError, match="shard index"):
        archive._export_shard_index(source.read_bytes())

    assert source.read_bytes() == contents


def test_export_does_not_follow_shard_index_symlinks_or_create_missing_index(
    saved_cluster: Path,
    tmp_path: Path,
) -> None:
    index = saved_cluster / "cave" / "save" / "shardindex"
    index.unlink()
    with archive.export_cluster(saved_cluster):
        pass
    assert not index.exists()

    source = tmp_path / "shardindex"
    source.write_bytes(b"KLEI     1 return {server={}}")
    index.symlink_to(source)
    with (
        pytest.raises(ValueError, match="regular file"),
        archive.export_cluster(saved_cluster),
    ):
        pass
    assert source.read_bytes() == b"KLEI     1 return {server={}}"
