"""Exercise archive streams in memory, room exports and awaited S3 cleanup."""

# Addresses, programs and credentials below belong to disposable local fixtures.
# ruff: file-ignore[hardcoded-password-string, hardcoded-password-func-arg]

import asyncio
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from dst_server.archive import (
    ArchiveUploadResult,
    ClusterArchive,
    ImportedCluster,
    export_cluster,
    import_cluster,
)
from dst_server.host import Host
from dst_server.settings import Room, RoomStore, build_template


async def check_export(directory: Path) -> None:
    root = directory / "rooms"
    systemctl = directory / "systemctl"
    systemctl.write_text(
        f"#!{sys.executable}\n"
        "print('Id=dst-003.service\\nLoadState=loaded\\nActiveState=inactive\\n'\n"
        "      'SubState=dead\\nJob=0\\nResult=success')\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    RoomStore(root).save(
        Room({
            "number": 3,
            "cluster": build_template(
                "pure_survival", number=3, token="archive-fixture-token"
            ).dump(secrets=True),
        })
    )
    token = (root / "003/cluster_token.txt").read_bytes()
    async with Host(root, directory / "units", systemctl=systemctl) as host:
        async with export_cluster(host, 3, room_id="shared") as archive:
            assert archive.filename.startswith("DST-shared-")
            assert archive.stream.seekable()
            assert str(
                await asyncio.to_thread(
                    Path(f"/proc/self/fd/{archive.stream.fileno()}").readlink
                )
            ).startswith("/memfd:dst-archive")
            contents = archive.stream.read()
            assert contents.startswith(b"7z\xbc\xaf\x27\x1c")
            output = directory / "saved.7z"
            await archive.save(output)
            assert output.read_bytes() == contents
            await check_import(directory, archive, output, len(contents))
            try:
                await archive.save(output)
            except FileExistsError:
                pass
            else:
                message = "existing archive was overwritten"
                raise AssertionError(message)
        assert archive.stream.closed
        async with export_cluster(host, 3, client_compatible=True) as archive:
            assert archive.filename.startswith("DST-Cluster_3-")
            assert archive.stream.read(6) == b"7z\xbc\xaf\x27\x1c"
            assert str(
                await asyncio.to_thread(
                    Path(f"/proc/self/fd/{archive.stream.fileno()}").readlink
                )
            ).startswith("/memfd:dst-archive")
        assert not (root / "003/Cluster_3").exists()
        try:
            async with export_cluster(
                host, 3, client_compatible=True, room_id="custom"
            ):
                pass
        except ValueError as error:
            if "room_id cannot be overridden" not in str(error):
                raise
        else:
            message = "client export accepted a custom root directory"
            raise AssertionError(message)
        for number, level in ((True, 3), (3, True), (3, 0), (3, 10)):
            try:
                async with export_cluster(host, number, compression_level=level):
                    pass
            except ValueError:
                pass
            else:
                message = "invalid archive argument accepted"
                raise AssertionError(message)
    assert (root / "003/cluster_token.txt").read_bytes() == token


async def check_import(
    directory: Path, archive: ClusterArchive, output: Path, length: int
) -> None:
    restored = directory / "restored-memory"
    imported = await import_cluster(archive)
    config = imported.configuration
    assert not restored.exists()
    assert "cluster.ini" in imported.files
    key = config.dump(defaults=True, secrets=True)["settings"]["cluster_key"]
    assert key in imported.read_file("cluster.ini").decode()
    assert await imported.save(restored) is None
    assert config.model == "ClusterConfig"
    assert config.dump(defaults=True, secrets=True)["token"] == ""
    assert (restored / "cluster_token.txt").read_bytes() == b""
    assert (restored / "cluster.ini").is_file()
    assert archive.stream.tell() == length
    for source in (output, restored):
        parsed = await import_cluster(source)
        assert parsed.configuration.model == "ClusterConfig"
        assert (
            parsed.configuration.dump(defaults=True, secrets=True)["settings"][
                "cluster_key"
            ]
            != key
        )
    assert imported.read_file("cluster.ini") == (restored / "cluster.ini").read_bytes()
    for attempt in range(8):
        parent = directory / f"batch-{attempt}" / "new-parent"
        targets = [parent / f"room-{index}" for index in range(8)]
        assert await asyncio.gather(*(imported.save(path) for path in targets)) == [
            None
        ] * len(targets)
        assert all(
            (path / "cluster.ini").read_bytes() == imported.read_file("cluster.ini")
            for path in targets
        )
    try:
        imported.read_file("../cluster.ini")
    except KeyError:
        pass
    else:
        message = "read_file accepted a missing path"
        raise AssertionError(message)
    try:
        await imported.save(restored)
    except ValueError as error:
        if "already exists" not in str(error):
            raise
    else:
        message = "existing import destination was replaced"
        raise AssertionError(message)


async def check_client_import(directory: Path) -> None:
    from zipfile import ZipFile

    source = directory / "client.zip"
    index = (
        'KLEI     1 return {session_id="0123456789ABCDEF",version=5,'
        'server={name="Client fixture",server_language="zh",'
        "encode_user_path=true,online_mode=false},"
        'world={options={id="SURVIVAL_TOGETHER",name="World",desc="",location="forest",overrides={task_set="default"}}},'
        'enabled_mods={["workshop-123"]={enabled=true}}}'
    )
    with ZipFile(source, "w") as archive:
        archive.writestr("Cluster_9/Master/save/shardindex", index)
        archive.writestr(
            "Cluster_9/Master/save/session/0123456789ABCDEF/0000000001",
            b"world\x00\xff",
        )
        archive.writestr(
            "Cluster_9/Master/save/session/0123456789ABCDEF/A7K1NP32JUC8/0000000001",
            b"player\x00\xff",
        )
        archive.writestr("Cluster_9/Master/save/server_temp/cache", b"discard")
    destination = directory / "client-import"
    imported = await import_cluster(source)
    config = imported.configuration
    assert not destination.exists()
    await imported.save(destination)
    assert config.dump(defaults=True)["settings"]["cluster_name"] == "Client fixture"
    assert config.dump(defaults=True)["settings"]["cluster_language"] == "zh"
    shard = config.dump(defaults=True)["shards"]["Master"]
    assert shard["settings"]["name"] == "Master"
    assert shard["settings"]["encode_user_path"] is False
    assert (
        destination / "Master/save/session/0123456789ABCDEF/0000000001"
    ).read_bytes() == b"world\x00\xff"
    assert (
        destination / "Master/save/session/0123456789ABCDEF/KU_WRv6AVc8_/0000000001"
    ).read_bytes() == b"player\x00\xff"
    assert not (destination / "Master/save/server_temp").exists()
    assert "123" in (destination / "mods/dedicated_server_mods_setup.lua").read_text()
    (destination / "cluster.ini").write_text("[NETWORK]\ncluster_language = en\n")
    restored = await import_cluster(destination)
    assert (
        restored.configuration.dump(defaults=True)["settings"]["cluster_language"]
        == "en"
    )
    await check_apply(imported, destination)


async def check_apply(imported: ImportedCluster, destination: Path) -> None:
    (destination / "cluster_token.txt").write_text("keep-token")
    overrides = destination / "Master/modoverrides.lua"
    overrides.write_text("return {}\n")
    applied = await imported.apply(destination, level="data")
    assert applied.dump(defaults=True)["settings"]["cluster_language"] == "en"
    assert overrides.read_text() == "return {}\n"
    applied = await imported.apply(destination)
    assert applied.dump(defaults=True)["settings"]["cluster_language"] == "en"
    assert overrides.read_bytes() == imported.read_file("Master/modoverrides.lua")
    applied = await imported.apply(destination, level="all")
    assert applied.dump(defaults=True)["settings"]["cluster_language"] == "zh"
    assert (destination / "cluster_token.txt").read_text() == "keep-token"
    try:
        await imported.apply(destination, level="invalid")
    except ValueError as error:
        message = str(error)
    else:
        message = "invalid import level was accepted"
        raise AssertionError(message)
    assert "level must be" in message
    from zipfile import ZipFile

    partial_path = destination.parent / "partial.zip"
    with ZipFile(partial_path, "w") as archive:
        archive.writestr("Master/server.ini", "[SHARD]\nis_master = true\n")
        archive.writestr("Master/save/recipebook", b"recipe progress")
    partial = await import_cluster(partial_path)
    world = destination / "Master/save/session/0123456789ABCDEF/0000000001"
    before = world.read_bytes()
    try:
        await partial.apply(destination)
    except ValueError as error:
        if "missing shardindex" not in str(error):
            raise
    else:
        message = "incomplete import replaced an existing world"
        raise AssertionError(message)
    assert world.read_bytes() == before
    await check_apply_acl(imported, destination.parent)


async def check_apply_acl(imported: ImportedCluster, directory: Path) -> None:
    import struct

    # Linux POSIX ACL entries: owner, owning group, optional mask, others.
    acls = {
        name: struct.pack("<I", 2)
        + b"".join(struct.pack("<HHI", tag, mode, 0xFFFFFFFF) for tag, mode in entries)
        for name, entries in [
            ("system.posix_acl_access", [(1, 7), (4, 0), (16, 5), (32, 0)]),
            ("system.posix_acl_default", [(1, 7), (4, 0), (32, 0)]),
        ]
    }
    target = directory / "acl-room"
    await imported.save(target)
    cache = target / "mods/cache"
    cache.mkdir()
    token = target / "cluster_token.txt"
    token.write_bytes(b"keep-acl-fixture-token")
    token.chmod(0o640)
    inode = token.stat().st_ino
    for path in [target, cache]:
        for name, value in acls.items():
            os.setxattr(path, name, value)
    await imported.apply(target, level="data")
    for path in [target, cache]:
        for name, value in acls.items():
            assert os.getxattr(path, name) == value
        created = path / "new-file"
        created.write_bytes(b"private fixture")
        assert created.stat().st_mode & 0o777 == 0o600
    assert token.stat().st_ino == inode
    assert token.read_bytes() == b"keep-acl-fixture-token"

    parent = directory / "acl-parent"
    parent.mkdir()
    os.setxattr(parent, "system.posix_acl_default", acls["system.posix_acl_access"])
    target = parent / "room"
    await imported.save(target)
    for name in acls:
        os.removexattr(target, name)
    mode = target.stat().st_mode
    await imported.apply(target, level="data")
    assert target.stat().st_mode == mode
    assert acls.keys().isdisjoint(os.listxattr(target))


async def check_formats(directory: Path) -> None:
    import io
    import tarfile
    import zipfile

    session = "Surface One/save/session/0123456789ABCDEF"
    entries = {
        "cluster.ini": b"[NETWORK]\ncluster_name = Format fixture\n",
        "Surface One/server.ini": b"[SHARD]\nis_master = true\n",
        f"{session}/世界.meta": b"metadata\x00\xff",
        f"{session}/A7K1NP32JUC8/0000000001": b"encoded player\x00\xff",
        f"{session}/KU_ABCDEFGH/0000000001": b"raw player\x00\xff",
    }
    archives: list[bytes] = []
    for method in (
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
    ):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=method) as writer:
            for name, data in entries.items():
                writer.writestr(f"Room/{name}", data)
        archives.append(stream.getvalue())
    for mode in ("w", "w:gz", "w:bz2", "w:xz", "w:zst"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode=mode) as writer:
            for name, data in entries.items():
                info = tarfile.TarInfo(f"./Room/{name}")
                info.size = len(data)
                writer.addfile(info, io.BytesIO(data))
        archives.append(stream.getvalue())
    before = await asyncio.to_thread(lambda: sorted(directory.iterdir()))
    for data in archives:
        with ClusterArchive("unknown-format.bin", io.BytesIO(data)) as source:
            imported = await import_cluster(source)
        assert (
            imported.configuration.dump(defaults=True)["settings"]["cluster_name"]
            == "Format fixture"
        )
        session = "Surface One/save/session/0123456789ABCDEF"
        assert imported.read_file(f"{session}/世界.meta") == b"metadata\x00\xff"
        assert (
            imported.read_file(f"{session}/KU_WRv6AVc8_/0000000001")
            == b"encoded player\x00\xff"
        )
        assert (
            imported.read_file(f"{session}/KU_ABCDEFGH_/0000000001")
            == b"raw player\x00\xff"
        )
    assert await asyncio.to_thread(lambda: sorted(directory.iterdir())) == before

    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as writer:
            info = tarfile.TarInfo("Surface One/save/session/0123456789ABCDEF/link")
            info.type = kind
            info.linkname = "/etc/passwd"
            writer.addfile(info)
        with ClusterArchive("links.tar", io.BytesIO(stream.getvalue())) as source:
            try:
                await import_cluster(source)
            except ValueError as error:
                if "links" not in str(error) and "regular files" not in str(error):
                    raise
            else:
                message = "archive accepted a link or special file"
                raise AssertionError(message)


async def check_archive_integrity_and_names() -> None:
    import gzip
    import io
    import tarfile
    import zipfile

    session = "Master/save/session/0123456789ABCDEF"
    entries = {
        "cluster.ini": b"[NETWORK]\ncluster_name = Integrity fixture\n",
        "Master/server.ini": b"[SHARD]\nis_master = true\n",
        f"{session}/cafx.meta": b"snapshot\x00\xff",
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as writer:
        for name, data in entries.items():
            writer.writestr(name, data)
    zipped = stream.getvalue().replace(b"cafx.meta", b"caf\x82.meta")
    with ClusterArchive("cp437.zip", io.BytesIO(zipped)) as source:
        imported = await import_cluster(source)
    assert imported.read_file(f"{session}/café.meta") == b"snapshot\x00\xff"

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as writer:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            writer.addfile(info, io.BytesIO(data))
    packed = gzip.compress(stream.getvalue(), compresslevel=0, mtime=0)
    damaged = packed.replace(b"snapshot\x00\xff", b"corrupt!\x00\xff", 1)
    assert damaged != packed
    for data in (
        zipped.replace(b"snapshot\x00\xff", b"corrupt!\x00\xff", 1),
        damaged,
        packed[:-1],
        packed + damaged,
        gzip.compress(damaged),
        zipped.replace(b"caf\x82.meta", b"caf\x00.meta"),
    ):
        with ClusterArchive("damaged.bin", io.BytesIO(data)) as source:
            try:
                await import_cluster(source)
            except ValueError, OSError:
                pass
            else:
                message = "archive accepted a corrupt or incomplete stream"
                raise AssertionError(message)


async def check_compressed_tar_integrity() -> None:
    import bz2
    import hashlib
    import io
    import lzma
    import tarfile
    from compression import zstd

    name = "Master/save/session/0123456789ABCDEF/0000000001"
    world = hashlib.shake_256(b"archive integrity").digest(120_000)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as writer:
        for path, data in {
            "cluster.ini": b"[NETWORK]\ncluster_name = Integrity\n",
            "Master/server.ini": b"[SHARD]\nis_master = true\n",
            name: world,
        }.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            writer.addfile(info, io.BytesIO(data))
    tar = stream.getvalue()
    cases = (
        (bz2.compress(tar), bz2.decompress, -2),
        (lzma.compress(tar), lzma.decompress, -12),
        (
            zstd.compress(tar, options={zstd.CompressionParameter.checksum_flag: 1}),
            zstd.decompress,
            -1,
        ),
    )
    for packed, decode, offset in cases:
        with ClusterArchive("valid.bin", io.BytesIO(packed)) as source:
            imported = await import_cluster(source)
        assert imported.read_file(name) == world
        damaged = bytearray(packed)
        damaged[offset] ^= 1
        try:
            decode(damaged)
        except OSError, lzma.LZMAError, zstd.ZstdError:
            pass
        else:
            message = "test fixture did not damage the compression checksum"
            raise AssertionError(message)
        with ClusterArchive("damaged.bin", io.BytesIO(damaged)) as source:
            try:
                await import_cluster(source)
            except ValueError, OSError:
                pass
            else:
                message = "TAR ended before validating its compressed stream"
                raise AssertionError(message)


class Secret:
    def __init__(self, value: str) -> None:
        self.value = value

    def get_secret_value(self) -> str:
        return self.value


async def check_upload() -> None:  # ruff: ignore[too-many-statements, too-many-locals]
    payload = b"binary archive\0\xff" * 1024
    parts: list[bytes] = []
    requests: list[tuple[str, str, str, str]] = []
    receiving, release, aborted = (threading.Event() for _ in range(3))
    release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self, body: bytes = b"", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"fixture-etag"')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            requests.append((
                self.path,
                self.headers.get("Authorization", ""),
                self.headers.get("x-amz-security-token", ""),
                self.headers.get("Content-Type", ""),
            ))
            if "uploads" in self.path:
                self.respond(
                    b"<InitiateMultipartUploadResult><Bucket>fixture</Bucket>"
                    b"<Key>room.7z</Key><UploadId>fixture-upload</UploadId>"
                    b"</InitiateMultipartUploadResult>"
                )
            else:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.respond(
                    b"<CompleteMultipartUploadResult><Location>fixture</Location>"
                    b"<Bucket>fixture</Bucket><Key>room.7z</Key>"
                    b'<ETag>"fixture-etag"</ETag></CompleteMultipartUploadResult>'
                )

        def do_PUT(self) -> None:
            parts.append(self.rfile.read(int(self.headers["Content-Length"])))
            receiving.set()
            release.wait(20)
            self.respond()

        def do_DELETE(self) -> None:
            assert "uploadId=fixture-upload" in self.path
            aborted.set()
            self.respond(status=204)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        environment = {
            "AWS_ALLOW_HTTP": "true",
            "AWS_BUCKET_NAME": "environment-bucket",
            "AWS_ENDPOINT_URL_S3": endpoint,
            "AWS_ACCESS_KEY_ID": "environment-key",
            "AWS_SECRET_ACCESS_KEY": "environment-secret",
            "AWS_SESSION_TOKEN": "environment-token",
            "AWS_REGION": "environment-region",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        try:
            with patch.dict(os.environ, environment), BytesIO(payload) as source:
                with ClusterArchive("room.7z", source) as archive:
                    assert archive.stream.read(3) == payload[:3]
                    result = await archive.upload(
                        object_prefix="shared/", url_prefix="https://assets.invalid/"
                    )
                    assert result == ArchiveUploadResult(
                        "shared/room.7z", "https://assets.invalid/room.7z"
                    )
                    assert parts[-1] == payload
                    assert archive.stream.tell() == 3
                    path, authorization, token, content_type = requests[-2]
                    assert path.startswith("/environment-bucket/shared/room.7z?")
                    assert "Credential=environment-key/" in authorization
                    assert "/environment-region/s3/" in authorization
                    assert token == "environment-token"
                    assert content_type == "application/x-7z-compressed"

                    with patch.dict(
                        os.environ, {"AWS_ENDPOINT_URL_S3": "http://127.0.0.1:1"}
                    ):
                        result = await archive.upload(
                            bucket="explicit-bucket",
                            endpoint=endpoint,
                            region="explicit-region",
                            access_key_id=Secret("explicit-key"),
                            secret_access_key=Secret("explicit-secret"),
                            session_token=Secret("explicit-token"),
                        )
                    assert result == ArchiveUploadResult("room.7z", None)
                    path, authorization, token, _ = requests[-2]
                    assert path.startswith("/explicit-bucket/room.7z?")
                    assert "Credential=explicit-key/" in authorization
                    assert "/explicit-region/s3/" in authorization
                    assert token == "explicit-token"
                    assert parts[-1] == payload

                    receiving.clear()
                    release.clear()
                    task = asyncio.create_task(archive.upload())
                    assert await asyncio.to_thread(receiving.wait, 10)
                    task.cancel()
                    await asyncio.sleep(0.05)
                    assert not task.done(), "cancellation returned before part cleanup"
                    release.set()
                    try:
                        await task
                    except asyncio.CancelledError:
                        assert aborted.is_set()
                    else:
                        message = "archive upload cancellation was swallowed"
                        raise AssertionError(message)
                assert archive.stream.closed
                assert not source.closed
        finally:
            release.set()
            server.shutdown()
            worker.join(5)


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        asyncio.run(check_export(Path(temporary)))
        asyncio.run(check_client_import(Path(temporary)))
        asyncio.run(check_formats(Path(temporary)))
        asyncio.run(check_archive_integrity_and_names())
        asyncio.run(check_compressed_tar_integrity())
        asyncio.run(check_upload())


if __name__ == "__main__":
    main()
