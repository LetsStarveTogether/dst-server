"""Run installed archive commands against a stopped disposable room."""

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
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[0]).with_suffix('.state')\n"
        "state = path.read_text() if path.exists() else 'inactive'\n"
        "if state == 'transition':\n"
        "    path.write_text('active')\n"
        "    state = 'inactive'\n"
        "print(f'Id=dst-007.service\\nLoadState=loaded\\nActiveState={state}\\n'\n"
        "      'SubState=dead\\nJob=0\\nResult=success')\n",
        encoding="utf-8",
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

    existing = command(directory, output)
    assert existing.returncode == 1
    assert output.read_bytes() == contents

    locked = directory / "locked.7z"
    with (directory / "rooms/007/.dst-room.lock").open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert command(directory, locked).returncode == 1
        assert not locked.exists()

    systemctl.with_suffix(".state").write_text("transition", encoding="utf-8")
    raced = directory / "raced.7z"
    race = command(directory, raced)
    assert race.returncode == 1
    assert "inactive room service" in race.stderr
    assert not raced.exists()

    online = directory / "online.7z"
    assert command(directory, online).returncode == 1
    assert not online.exists()

    systemctl.with_suffix(".state").write_text("inactive", encoding="utf-8")
    check_upload_termination(directory)


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
