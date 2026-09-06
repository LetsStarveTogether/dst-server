import os
import re
import struct
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import BinaryIO

import pytest
from obstore.exceptions import PermissionDeniedError
from py7zr import SevenZipFile
from pydantic import SecretStr

from dst_server.cluster import archive
from dst_server.cluster.config import ClusterConfig, ClusterSettings, ShardSettings
from dst_server.cluster.overrides import _literal_return_table
from dst_server.cluster.presets import FOREST_CAVES
from dst_server.klei_id import encode_klei_id


@pytest.fixture
def saved_cluster(tmp_path: Path) -> Path:
    root = tmp_path / "001"
    configuration = FOREST_CAVES.build(
        token=SecretStr("private-token"),
        cluster_key=SecretStr("private-shard-key"),
        settings=ClusterSettings(cluster_password=SecretStr("private-password")),
    )
    configuration.replace(
        shards={
            name: shard.replace(settings=shard.settings.replace(encode_user_path=False))
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
            'server={password="private-index-password",encode_user_path=false}}',
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
            index.write_bytes(archive._export_shard_index(index))
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
    assert ClusterSettings.load(root / "cluster.ini").cluster_password is None
    for shard in ("forest", "cave"):
        assert (
            ShardSettings.load(root / shard / "server.ini").encode_user_path is encoded
        )
        index = _literal_return_table(root / shard / "save/shardindex", "test")
        assert index["session_id"] == "0123456789ABCDEF"
        assert index["server"] == {"encode_user_path": encoded}
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


def test_upload_uses_aws_environment_and_closes_stream_on_failure(
    saved_cluster: Path, monkeypatch: pytest.MonkeyPatch
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
        for name, value in {
            "AWS_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            "AWS_BUCKET": "archive-test",
            "AWS_ACCESS_KEY_ID": "test-access",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
            "AWS_REGION": "unused-region",
            "AWS_ALLOW_HTTP": "true",
        }.items():
            monkeypatch.setenv(name, value)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with archive.export_cluster(saved_cluster) as exported:
                expected = exported.stream.read()
                exported.stream.seek(6)
                first = exported.upload()
                second = exported.upload()
                assert first != second
                for key in (first, second):
                    assert re.fullmatch(
                        rf"[0-9A-HJKMNP-TV-Z]{{26}}/{re.escape(exported.filename)}",
                        key,
                    )
                assert [path for path, _, _ in requests] == [
                    f"/archive-test/{key}" for key in (first, second)
                ]
                assert not exported.stream.closed
            assert exported.stream.closed
            fail_upload = True
            with (
                pytest.raises(PermissionDeniedError, match="403"),
                archive.export_cluster(saved_cluster) as failed,
            ):
                failed.upload()
            assert failed.stream.closed
            assert len(requests) == 3
            assert [body for _, body, _ in requests[:2]] == [expected, expected]
            for _, body, headers in requests:
                assert body.startswith(b"7z\xbc\xaf\x27\x1c")
                assert headers["content-type"] == "application/x-7z-compressed"
                assert re.search(
                    r"Credential=test-access/\d{8}/auto/s3/aws4_request",
                    headers["authorization"],
                )
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
    ("convert", "original", "expected"),
    [
        (True, False, True),
        (True, True, True),
        (False, False, False),
        (False, True, True),
    ],
)
def test_export_shard_index_preserves_world_and_removes_credentials(
    tmp_path: Path, convert: bool, original: bool, expected: bool
) -> None:
    source = tmp_path / "shardindex"
    source.write_text(
        "KLEI     1 return {\n"
        'version=5, session_id="0123456789ABCDEF",\n'
        'world={options={overrides={day="default"},name="世界"}},\n'
        'enabled_mods={ ["workshop-1"]={enabled=true,configuration_options={\n'
        'password="mod setting", values={1,2,3}}}},\n'
        "server={\n"
        f"encode_user_path={str(original).lower()},\n"
        'password="private-password", cluster_password="private-cluster-password",\n'
        'cluster_key="private-cluster-key", cluster_token="private-cluster-token",\n'
        'token="private-token", online_mode=true, name="Room",\n'
        'clan={id="123",only=false,admin=false}}}\n',
        encoding="utf-8",
    )
    original_bytes = source.read_bytes()
    before = _literal_return_table(source, "test shard index")

    exported = archive._export_shard_index(source, encode_user_path=convert)

    assert source.read_bytes() == original_bytes
    assert exported.startswith(b"KLEI     1 return ")
    assert b"private-" not in exported
    target = tmp_path / "exported"
    target.write_bytes(exported)
    after = _literal_return_table(target, "test exported shard index")
    assert after == before | {
        "server": {
            "encode_user_path": expected,
            "online_mode": True,
            "name": "Room",
            "clan": {"id": "123", "only": False, "admin": False},
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
        archive._export_shard_index(source)

    assert source.read_bytes() == contents


def test_export_shard_index_does_not_follow_symlinks_or_create_missing_index(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="regular file"):
        archive._export_shard_index(missing)
    assert not missing.exists()

    source = tmp_path / "shardindex"
    source.write_bytes(b"KLEI     1 return {server={}}")
    linked = tmp_path / "linked"
    linked.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        archive._export_shard_index(linked)
