from pathlib import Path
from typing import Annotated

from pydantic import Field

from dst_server.configuration.models import ShardName
from dst_server.models.base import RevalidatedFrozenModel
from dst_server.telemetry import TelemetrySettings

LUA_DIRECTORY = Path(__file__).parents[1] / "lua"


type Argument = Annotated[str, Field(pattern=r"^[^\x00]*$")]


class ServerConfig(RevalidatedFrozenModel):
    shard: ShardName
    executable: Path = Path(
        "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
    )
    persistent_storage_root: Path = Path("/")
    conf_dir: Argument = "/"
    cluster: Argument = "cluster"
    telemetry_cluster: str | None = None
    ugc_directory: Path | None = Path("/cluster/mods/ugc")
    extra_args: tuple[Argument, ...] = ("-skip_update_server_mods",)
    lua_directory: Path = LUA_DIRECTORY
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    monitor_parent_process: bool = True

    def command(self, *, monitor_parent_process: int | None = None) -> tuple[str, ...]:
        type(self).model_validate(self)
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
