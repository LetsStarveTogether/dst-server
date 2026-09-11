"""Run against an installed wheel with: python -I tests/distribution.py."""

import io
import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from contextlib import redirect_stdout
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

import dst_server
from dst_server.cli import main
from dst_server.cluster.service import configure_otel
from dst_server.configuration.models import ClusterConfig
from dst_server.deployment import QuadletApplication
from dst_server.rooms import RoomStore
from dst_server.runtime import ServerConfig


def check_package() -> None:
    installed = distribution("dst-server")
    entries = tuple(installed.files or ())
    assert Path("dst_server/__init__.py") in entries, "an installed wheel is required"
    assert (
        Path(dst_server.__file__).resolve().parent
        == Path(str(installed.locate_file("dst_server"))).resolve()
    )
    for entry in entries:
        if entry.parts[0] == "dst_server":
            assert Path(str(installed.locate_file(entry))).is_file(), (
                f"missing package file: {entry}"
            )
    for resource in (
        "rpc/schema/rpc.capnp",
        "lua/dst_server.lua",
        "lua/dst_server/console.lua",
        "host/systemd/dst-room-schedule.service",
        "host/systemd/dst-room-schedule.timer",
        "host/systemd/dst-room-recycle.service",
    ):
        assert files("dst_server").joinpath(resource).read_bytes(), resource
    with redirect_stdout(io.StringIO()):
        for command in (
            "room",
            "template",
            "deployment",
            "mod",
            "schedule",
            "maintenance",
            "agent",
            "player",
            "world",
            "console",
            "logs",
            "rpc",
            "announce",
            "annotations",
            "completion",
        ):
            assert main((command, "--help")) == 0, command


def check_cli(directory: Path) -> None:
    environment = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PYTHON")
        },
        "PYTHONNOUSERSITE": "1",
        "DST_SERVER_CLUSTER_TOKEN": "distribution-test-token",
        "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={directory / 'no-system-bus'}",
        "NO_COLOR": "1",
        "COLUMNS": "100",
    }
    entrypoint = Path(sys.executable).with_name("dst-server")

    def run(*arguments: str, module: bool = False) -> str:
        command = (
            (sys.executable, "-I", "-m", "dst_server") if module else (str(entrypoint),)
        )
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            (*command, *arguments),
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (arguments, result.stdout, result.stderr)
        return result.stdout

    assert run("--help") == run("--help", module=True)
    assert run() == run("--help")
    roots = (
        "--cluster-root",
        str(directory / "rooms"),
        "--quadlet-dir",
        str(directory / "quadlets"),
        "--json",
    )
    run(
        *roots,
        "room",
        "create",
        "299",
        "--template",
        "pure_survival",
        "--max-players",
        "11",
    )
    run(*roots, "room", "edit", "299", "--set", "/recycle=true", module=True)
    assert (
        json.loads(
            run(
                *roots,
                "room",
                "show",
                "299",
                "--field",
                "/cluster/settings/max_players",
            )
        )
        == 11
    )
    store = RoomStore(directory / "rooms")
    assert store.load(299).cluster.settings.max_players == 11
    assert store.load(299).recycle
    assert ClusterConfig.load(store.path(299)).settings.max_players == 11
    application = QuadletApplication.load(directory / "quadlets", name="dst-299")
    assert application.master.exec[1:3] == ("agent", "master")
    assert all(
        unit.log_driver == "journald"
        for unit in (application.master, *application.secondaries)
    )
    assert not (store.path(299) / ".dst-server.sock").exists()


if __name__ == "__main__":
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.invalid"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    assert configure_otel(ServerConfig(shard="smoke")) is None
    check_package()
    with TemporaryDirectory(prefix="dst-distribution-") as directory:
        check_cli(Path(directory))
