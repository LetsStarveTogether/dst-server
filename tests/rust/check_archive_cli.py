"""Export an owned room without service control or temporary disk artifacts."""

# Child programs, paths and credentials are local disposable fixtures.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, hardcoded-password-func-arg]

import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zipfile import ZipFile

from dst_server.settings import Room, RoomStore, build_template


def arguments(directory: Path) -> list[str]:
    program = sys.argv[1:] or [sys.executable, "-I", "-m", "dst_server"]
    return [*program, "--json", "archive", "--root", str(directory / "rooms")]


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
    before = sorted(directory.iterdir())
    parsed = subprocess.run(
        [*arguments(directory), "import", str(source)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stderr
    result = json.loads(parsed.stdout)
    assert result["path"] is None
    assert "cluster.ini" in result["files"]
    assert sorted(directory.iterdir()) == before
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

    client_input = directory / "client.zip"
    with ZipFile(client_input, "w") as archive:
        archive.writestr(
            "Cluster_2/Master/save/shardindex",
            'KLEI     1 return {session_id="0123456789ABCDEF",version=5,'
            'server={name="Client fixture",encode_user_path=true},'
            'world={options={id="SURVIVAL_TOGETHER",name="World",desc="",location="forest",overrides={task_set="default"}}},enabled_mods={}}',
        )
        archive.writestr(
            "Cluster_2/Master/save/session/0123456789ABCDEF/0000000001",
            b"world\x00\xff",
        )
        archive.writestr(
            "Cluster_2/Master/save/session/0123456789ABCDEF/A7K1NP32JUC8/0000000001",
            b"player\x00\xff",
        )
        archive.writestr("Cluster_2/Master/save/server_temp/cache", b"discard")
    client_output = directory / "imported-client"
    client_import = subprocess.run(
        [
            *arguments(directory),
            "import",
            str(client_input),
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
        client_output / "Master/save/session/0123456789ABCDEF/0000000001"
    ).read_bytes() == b"world\x00\xff"
    shard = json.loads(client_import.stdout)["configuration"]["shards"]["Master"]
    assert shard["settings"]["name"] == "Master"
    assert shard["settings"]["encode_user_path"] is False
    assert (
        client_output / "Master/save/session/0123456789ABCDEF/KU_WRv6AVc8_/0000000001"
    ).read_bytes() == b"player\x00\xff"
    assert not (client_output / "Master/save/server_temp").exists()

    check_apply(directory, client_input, client_output)

    import io
    import tarfile

    tar_input = directory / "format-detected.data"
    with tarfile.open(tar_input, "w:gz") as writer, ZipFile(client_input) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            writer.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo(
            "./Cluster_2/Master/save/session/0123456789ABCDEF/世界.meta"
        )
        info.size = 3
        writer.addfile(info, io.BytesIO(b"abc"))
    parsed_tar = subprocess.run(
        [*arguments(directory), "import", str(tar_input)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert parsed_tar.returncode == 0, parsed_tar.stderr
    assert (
        "Master/save/session/0123456789ABCDEF/世界.meta"
        in json.loads(parsed_tar.stdout)["files"]
    )


def check_apply(directory: Path, client_input: Path, client_output: Path) -> None:
    cluster_ini = client_output / "cluster.ini"
    cluster_ini.write_text("[NETWORK]\ncluster_name = Existing\n")
    overrides = client_output / "Master/modoverrides.lua"
    overrides.write_text("return {local_mod={enabled=true}}\n")
    apply = [
        *arguments(directory),
        "import",
        str(client_input),
        "--into",
        str(client_output),
    ]
    for options, level in [
        (["--level", "data"], "data"),
        ([], "runtime"),
        (["--level", "all"], "all"),
    ]:
        result = subprocess.run(
            [*apply, *options], capture_output=True, text=True, check=False, timeout=30
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["level"] == level
        assert receipt["path"] == str(client_output)
        assert receipt["configuration"]["settings"]["cluster_name"] == (
            "Client fixture" if level == "all" else "Existing"
        )
        assert ("local_mod" in overrides.read_text()) == (level == "data")
    for options in [["--output", str(directory / "conflict")], ["--level", "invalid"]]:
        result = subprocess.run(
            [*apply, *options], capture_output=True, text=True, check=False, timeout=30
        )
        assert result.returncode == 2, result.stderr
    with (client_output / ".dst-room.lock").open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            apply, capture_output=True, text=True, check=False, timeout=30
        )
        assert result.returncode == 1
        assert "busy" in result.stderr

    partial = directory / "partial.zip"
    with ZipFile(partial, "w") as archive:
        archive.writestr("Master/server.ini", "[SHARD]\nis_master = true\n")
        archive.writestr("Master/save/recipebook", b"recipe progress")
    world = client_output / "Master/save/session/0123456789ABCDEF/0000000001"
    before = world.read_bytes()
    result = subprocess.run(
        [*arguments(directory), "import", str(partial), "--into", str(client_output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1
    assert "no game saves" in result.stderr
    assert world.read_bytes() == before

    for cwd in [client_output, client_output / "Master"]:
        current = apply.copy()
        current[0] = str(Path(current[0]).absolute())
        if cwd == client_output:
            current[-1] = "."
        inode = cwd.stat().st_ino
        result = subprocess.run(
            current, cwd=cwd, capture_output=True, text=True, check=False, timeout=30
        )
        assert result.returncode == 1, result.stderr
        assert "current working directory" in result.stderr
        assert cwd.stat().st_ino == inode
        assert world.read_bytes() == before

    invalid = directory / os.fsdecode(b"destination-\xff")
    for option in ["--output", "--into"]:
        if option == "--into":
            shutil.copytree(client_output, invalid)
            (invalid / world.relative_to(client_output)).write_bytes(
                b"keep previous world"
            )
        result = subprocess.run(
            [*arguments(directory), "import", str(client_input), option, str(invalid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 1, result.stderr
        assert "valid UTF-8" in result.stderr
        if option == "--output":
            assert not invalid.exists()
        else:
            assert (
                invalid / world.relative_to(client_output)
            ).read_bytes() == b"keep previous world"


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
