from dataclasses import dataclass, field
from pathlib import Path

from dst_server.telemetry import TelemetrySettings

LUA_DIRECTORY = Path(__file__).parents[1] / "lua"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    shard: str
    executable: Path = Path(
        "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
    )
    persistent_storage_root: Path = Path("/")
    conf_dir: str = "/"
    cluster: str = "cluster"
    telemetry_cluster: str | None = None
    ugc_directory: Path | None = Path("/cluster/mods/ugc")
    extra_args: tuple[str, ...] = ("-skip_update_server_mods",)
    lua_directory: Path = LUA_DIRECTORY
    telemetry: TelemetrySettings = field(default_factory=TelemetrySettings)
    monitor_parent_process: bool = True

    def command(self, *, monitor_parent_process: int | None = None) -> tuple[str, ...]:
        command = [
            str(self.executable),
            "-persistent_storage_root",
            str(self.persistent_storage_root),
            "-conf_dir",
            self.conf_dir,
            "-cluster",
            self.cluster,
            "-shard",
            self.shard,
        ]
        if self.ugc_directory is not None:
            command.extend(("-ugc_directory", str(self.ugc_directory)))
        if monitor_parent_process is not None:
            command.extend(("-monitor_parent_process", str(monitor_parent_process)))
        command.extend(self.extra_args)
        command.append("-cloudserver")
        return tuple(command)
