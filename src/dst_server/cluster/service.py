import os
from pathlib import Path
from typing import TYPE_CHECKING

from logbook import Logger
from pydantic import TypeAdapter

from dst_server import mods
from dst_server.configuration import files as layout
from dst_server.configuration.models import Port
from dst_server.mods import EXECUTABLE
from dst_server.runtime import ServerConfig
from dst_server.telemetry import TelemetrySettings

if TYPE_CHECKING:
    from dst_server.telemetry.otel import Pipeline

DEFAULT_INSTALL_PATH = Path("/install")
DEFAULT_CLUSTER_PATH = Path("/cluster")
OTEL_ENDPOINTS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)
CLUSTER_NAME_ENV = "DST_SERVER_CLUSTER_NAME"
_EXTERNAL_PORT = TypeAdapter(Port)
logger = Logger(__name__)


def _validate_external_port(value: int) -> int:
    return _EXTERNAL_PORT.validate_python(value, strict=True)


async def prepare_shared(
    install_path: Path = DEFAULT_INSTALL_PATH,
    cluster_path: Path = DEFAULT_CLUSTER_PATH,
    *,
    update_mods: bool = True,
) -> tuple[layout.Shard, ...]:
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()  # ruff: ignore[blocking-path-method-in-async-function]
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    shards = layout.discover(cluster_path)
    layout.prepare(cluster_path)
    logger.info("Found {shards} shard(s).", shards=len(shards))
    await mods.prepare(install_path, cluster_path, update=update_mods)
    return shards


def activate_shard(
    install_path: Path,
    cluster_path: Path,
) -> None:
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()
    mods.activate(install_path, cluster_path)


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
    return configure(resource_attributes=attributes)
