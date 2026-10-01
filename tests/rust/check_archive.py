"""Exercise native archive files, offline exports and awaited S3 cleanup."""

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

from dst_server.archive import ArchiveUploadResult, ClusterArchive, export_cluster
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
            contents = archive.stream.read()
            assert contents.startswith(b"7z\xbc\xaf\x27\x1c")
            output = directory / "saved.7z"
            await archive.save(output)
            assert output.read_bytes() == contents
            try:
                await archive.save(output)
            except FileExistsError:
                pass
            else:
                message = "existing archive was overwritten"
                raise AssertionError(message)
        assert archive.stream.closed
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
        asyncio.run(check_upload())


if __name__ == "__main__":
    main()
