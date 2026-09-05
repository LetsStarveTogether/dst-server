import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from logbook import Logger

from dst_server.runtime import ServerConfig
from dst_server.steamcmd import SteamCMD
from dst_server.telemetry import TelemetrySettings

from . import console, layout, mods

if TYPE_CHECKING:
    from dst_server.telemetry.otel import Pipeline

DEFAULT_INSTALL_PATH = Path("/install")
DEFAULT_CLUSTER_PATH = Path("/cluster")
EXECUTABLE = Path("bin64/dontstarve_dedicated_server_nullrenderer_x64")
OTEL_ENDPOINTS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)
CLUSTER_NAME_ENV = "DST_SERVER_CLUSTER_NAME"
MIN_EXTERNAL_PORT = 1024
MAX_EXTERNAL_PORT = 65535
logger = Logger(__name__)


def _validate_external_port(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_EXTERNAL_PORT <= value <= MAX_EXTERNAL_PORT
    ):
        msg = "external_port must be between 1024 and 65535"
        raise ValueError(msg)
    return value


def log_handler(prefix: str) -> Callable[[str], None]:
    def write(line: str) -> None:
        logger.info("{prefix}{line}", prefix=prefix, line=line)

    return write


async def prepare_shared(
    install_path: Path = DEFAULT_INSTALL_PATH,
    cluster_path: Path = DEFAULT_CLUSTER_PATH,
    *,
    update_mods: bool = True,
) -> tuple[layout.Shard, ...]:
    backend = os.environ.get("DST_SERVER_MOD_UPDATER", "native")
    if backend not in {"native", "steamcmd"}:
        msg = "DST_SERVER_MOD_UPDATER must be 'native' or 'steamcmd'"
        raise ValueError(msg)
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()  # ruff: ignore[blocking-path-method-in-async-function]
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    shards = layout.discover(cluster_path)
    layout.prepare(cluster_path)
    mod_ids = mods.prepare_shared(cluster_path)
    logger.info(
        "Found {shards} shard(s) and {mods} Workshop mod(s).",
        shards=len(shards),
        mods=len(mod_ids),
    )
    setup = cluster_path / "mods" / "dedicated_server_mods_setup.lua"
    if update_mods and (mod_ids or mods.has_setup_code(setup)):
        if backend == "steamcmd":
            from dst_server.workshop import WorkshopUpdater

            items, collections = mods.setup_downloads(setup, strict=True)
            if not items and not collections:
                return shards
            steamcmd_executable = os.environ.get("DST_SERVER_STEAMCMD")
            if not steamcmd_executable:
                if directory := os.environ.get("STEAMCMDDIR"):
                    steamcmd_executable = str(Path(directory) / "steamcmd.sh")
                else:
                    steamcmd_executable = "steamcmd"
            resolved = shutil.which(steamcmd_executable)
            if resolved is None:
                msg = (
                    f"SteamCMD executable not found: {steamcmd_executable}; "
                    "set DST_SERVER_STEAMCMD "
                    "or STEAMCMDDIR"
                )
                raise FileNotFoundError(msg)
            updater = WorkshopUpdater(
                SteamCMD(resolved, log_handler=log_handler("[MOD_UPDATE]: ")),
                cluster_path / "mods",
            )
            await updater.update(items, collections=collections)
            mods.activate(install_path, cluster_path)
        else:
            mods.activate(install_path, cluster_path)
            await mods.update(
                executable,
                cluster_path / "mods" / "ugc",
                log_handler=log_handler("[MOD_UPDATE]: "),
            )
    return shards


def activate_shard(
    install_path: Path,
    cluster_path: Path,
    shard: layout.Shard,
) -> None:
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()
    mods.activate(install_path, cluster_path)
    console.ensure(shard.console)


def create_server_config(
    install_path: Path,
    cluster_path: Path,
    shard: layout.Shard,
    *,
    external_port: int | None = None,
    telemetry: TelemetrySettings | None = None,
) -> ServerConfig:
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if external_port is not None:
        _validate_external_port(external_port)
    telemetry = telemetry if telemetry is not None else TelemetrySettings()
    return ServerConfig(
        shard=shard.name,
        executable=executable,
        persistent_storage_root=cluster_path.parent,
        conf_dir=".",
        cluster=cluster_path.name,
        telemetry_cluster=(
            os.environ.get(CLUSTER_NAME_ENV)
            or f"dst-{cluster_path.name.removeprefix('dst-')}"
        ),
        ugc_directory=cluster_path / "mods" / "ugc",
        extra_args=(
            "-skip_update_server_mods",
            "-external_port",
            str(external_port),
        )
        if external_port is not None
        else ("-skip_update_server_mods",),
        telemetry=telemetry,
    )


def otel_requested() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").casefold() != "true" and any(
        os.environ.get(name) for name in OTEL_ENDPOINTS
    )


def configure_otel(
    config: ServerConfig,
    *,
    instance_id: str | None = None,
) -> Pipeline | None:
    if not otel_requested():
        return None

    from dst_server.telemetry.otel import configure

    name = config.telemetry_cluster or config.cluster
    attributes = {"dst.cluster.name": name}
    if instance_id is not None:
        attributes["service.instance.id"] = instance_id
    return configure(
        resource_attributes=attributes,
        outbox_path=(
            config.persistent_storage_root
            / config.conf_dir
            / config.cluster
            / config.shard
            / ".telemetry.sqlite3"
        ),
    )
