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
        for number, level in ((True, 3), (3, True), (3, 0), (3, 23)):
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
    config = await import_cluster(archive, restored)
    assert config.model == "ClusterConfig"
    assert config.dump(defaults=True, secrets=True)["token"] == ""
    assert (restored / "cluster_token.txt").read_bytes() == b""
    assert (restored / "cluster.ini").is_file()
    assert archive.stream.tell() == length
    assert (
        await import_cluster(output, directory / "restored-file")
    ).model == "ClusterConfig"
    assert (
        await import_cluster(restored, directory / "restored-directory")
    ).model == "ClusterConfig"
    try:
        await import_cluster(archive, restored)
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
        'server={name="Client fixture",encode_user_path=true,online_mode=false},'
        'world={options={id="SURVIVAL_TOGETHER",name="World",desc="",location="forest",overrides={task_set="default"}}},'
        'enabled_mods={["workshop-123"]={enabled=true}}}'
    )
    with ZipFile(source, "w") as archive:
        archive.writestr("Cluster_9/Master/save/shardindex", index)
        archive.writestr(
            "Cluster_9/Master/save/session/0123456789ABCDEF/000000001", b"world\x00\xff"
        )
        archive.writestr(
            "Cluster_9/Master/save/session/0123456789ABCDEF/A7K1NP32JUC8/000000001",
            b"player\x00\xff",
        )
        archive.writestr("Cluster_9/Master/save/server_temp/cache", b"discard")
    destination = directory / "client-import"
    config = await import_cluster(source, destination, from_client=True)
    assert config.dump(defaults=True)["settings"]["cluster_name"] == "Client fixture"
    shard = config.dump(defaults=True)["shards"]["forest"]
    assert shard["settings"]["name"] == "forest"
    assert shard["settings"]["encode_user_path"] is False
    assert (
        destination / "forest/save/session/0123456789ABCDEF/000000001"
    ).read_bytes() == b"world\x00\xff"
    assert (
        destination / "forest/save/session/0123456789ABCDEF/KU_WRv6AVc8_/000000001"
    ).read_bytes() == b"player\x00\xff"
    assert not (destination / "Master").exists()
    assert not (destination / "forest/save/server_temp").exists()
    assert "123" in (destination / "mods/dedicated_server_mods_setup.lua").read_text()
    try:
        await import_cluster(source, directory / "invalid-option", from_client=1)  # ty: ignore[invalid-argument-type]
    except ValueError:
        pass
    else:
        message = "from_client accepted a non-boolean option"
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
        asyncio.run(check_upload())


if __name__ == "__main__":
    main()
