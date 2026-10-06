"""Export an owned room without service control or temporary disk artifacts."""

# Child programs, paths and credentials are local disposable fixtures.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, hardcoded-password-func-arg]

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dst_server.settings import Room, RoomStore, build_template


def arguments(directory: Path) -> list[str]:
    program = (
        sys.argv[1:]
        if len(sys.argv) > 1
        else [sys.executable, "-I", "-m", "dst_server"]
    )
    return [
        *program,
        "--json",
        "archive",
        "--root",
        str(directory / "rooms"),
        "--quadlet-dir",
        str(directory / "units"),
        "--systemctl",
        str(directory / "systemctl"),
    ]


def command(
    directory: Path, output: Path, *options: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*arguments(directory), "export", "7", "--output", str(output), *options],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def check(directory: Path) -> None:
    systemctl = directory / "systemctl"
    systemctl.write_text(
        f"#!{sys.executable}\nraise SystemExit('export called systemctl')\n"
    )
    systemctl.chmod(0o755)
    store = RoomStore(directory / "rooms")
    store.save(
        Room({
            "number": 7,
            "cluster": build_template(
                "pure_survival", number=7, token="archive-cli-fixture-token"
            ).dump(secrets=True),
        })
    )
    output = directory / "room.7z"
    result = command(directory, output, "--room-id", "shared-room")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["filename"].startswith("DST-shared-room-")
    assert receipt["path"] == str(output)
    contents = output.read_bytes()
    assert contents.startswith(b"7z\xbc\xaf\x27\x1c")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    check_import(directory, output)

    existing = command(directory, output)
    assert existing.returncode == 1
    assert output.read_bytes() == contents

    locked = directory / "locked.7z"
    with (directory / "rooms/007/.dst-room.lock").open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exported = command(directory, locked)
        assert exported.returncode == 0, exported.stderr
        assert locked.read_bytes().startswith(b"7z\xbc\xaf\x27\x1c")
    assert not (directory / "rooms/007/.dst-archives").exists()

    client = command(directory, directory / "client.7z", "--client-compatible")
    assert client.returncode == 0, client.stderr
    assert json.loads(client.stdout)["filename"].startswith("DST-Cluster_7-")
    conflict = command(
        directory,
        directory / "conflict.7z",
        "--client-compatible",
        "--room-id",
        "custom",
    )
    assert conflict.returncode == 2, conflict.stderr
    assert not (directory / "conflict.7z").exists()

    invalid_level = command(
        directory, directory / "invalid-level.7z", "--compression-level", "10"
    )
    assert invalid_level.returncode == 2, invalid_level.stderr
    assert not (directory / "invalid-level.7z").exists()

    check_upload_termination(directory)


def check_import(directory: Path, source: Path) -> None:
    restored = directory / "restored"
    imported = subprocess.run(
        [*arguments(directory), "import", str(source), "--output", str(restored)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    result_config = json.loads(imported.stdout)
    assert result_config["path"] == str(restored)
    assert result_config["configuration"]["settings"]["cluster_name"]
    assert (restored / "cluster_token.txt").read_bytes() == b""
    assert (restored / "cluster.ini").is_file()
    assert not (restored / ".dst-server.json").exists()
    existing_import = subprocess.run(
        [*arguments(directory), "import", str(source), "--output", str(restored)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert existing_import.returncode == 1
    assert "already exists" in existing_import.stderr

    from zipfile import ZipFile

    client_input = directory / "client.zip"
    with ZipFile(client_input, "w") as archive:
        archive.writestr(
            "Cluster_2/Master/save/shardindex",
            'KLEI     1 return {session_id="0123456789ABCDEF",version=5,'
            'server={name="Client fixture",encode_user_path=true},'
            'world={options={id="SURVIVAL_TOGETHER",name="World",desc="",location="forest",overrides={task_set="default"}}},enabled_mods={}}',
        )
        archive.writestr(
            "Cluster_2/Master/save/session/0123456789ABCDEF/000000001", b"world\x00\xff"
        )
        archive.writestr(
            "Cluster_2/Master/save/session/0123456789ABCDEF/A7K1NP32JUC8/000000001",
            b"player\x00\xff",
        )
        archive.writestr("Cluster_2/Master/save/server_temp/cache", b"discard")
    client_output = directory / "imported-client"
    client_import = subprocess.run(
        [
            *arguments(directory),
            "import",
            str(client_input),
            "--from-client",
            "--output",
            str(client_output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert client_import.returncode == 0, client_import.stderr
    assert (
        json.loads(client_import.stdout)["configuration"]["settings"]["cluster_name"]
        == "Client fixture"
    )
    assert (
        client_output / "forest/save/session/0123456789ABCDEF/000000001"
    ).read_bytes() == b"world\x00\xff"
    shard = json.loads(client_import.stdout)["configuration"]["shards"]["forest"]
    assert shard["settings"]["name"] == "forest"
    assert shard["settings"]["encode_user_path"] is False
    assert (
        client_output / "forest/save/session/0123456789ABCDEF/KU_WRv6AVc8_/000000001"
    ).read_bytes() == b"player\x00\xff"
    assert not (client_output / "Master").exists()
    assert not (client_output / "forest/save/server_temp").exists()


def check_upload_termination(directory: Path) -> None:
    receiving, aborted, release = (threading.Event() for _ in range(3))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self, body: bytes = b"", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", "fixture-part")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.respond(
                b"<InitiateMultipartUploadResult><Bucket>fixture</Bucket>"
                b"<Key>room.7z</Key><UploadId>fixture-upload</UploadId>"
                b"</InitiateMultipartUploadResult>"
            )

        def do_PUT(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
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
        try:
            with subprocess.Popen(
                [
                    *arguments(directory),
                    "upload",
                    "7",
                    "--client-compatible",
                    "--bucket",
                    "fixture",
                    "--endpoint",
                    f"http://127.0.0.1:{server.server_port}",
                    "--access-key-id",
                    "fixture-key",
                    "--secret-access-key",
                    "fixture-secret",
                ],
                env={
                    **os.environ,
                    "AWS_ALLOW_HTTP": "true",
                    "NO_PROXY": "127.0.0.1",
                    "no_proxy": "127.0.0.1",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    assert receiving.wait(15), "upload did not reach the local fixture"
                    process.send_signal(signal.SIGTERM)
                    # Finish the owned part before aborting the upload.
                    time.sleep(0.1)
                    assert process.poll() is None
                    release.set()
                    _, stderr = process.communicate(timeout=10)
                    assert process.returncode == 1, stderr
                    assert aborted.is_set(), (
                        "CLI exited before aborting multipart upload"
                    )
                    assert "archive upload cancelled" in stderr
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
        finally:
            release.set()
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        check(Path(temporary))
