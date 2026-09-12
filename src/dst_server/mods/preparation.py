import os
from pathlib import Path

from logbook import Logger

from dst_server.configuration.overrides import has_setup_code

from . import native
from .files import activate, prepare_shared
from .native import EXECUTABLE

logger = Logger(__name__)


def _log_update(line: str) -> None:
    logger.info("[MOD_UPDATE]: {line}", line=line)


async def prepare(
    install_path: Path, cluster_path: Path, *, update: bool = True
) -> None:
    """Prepare room Mods, updating only while all game processes are stopped."""
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()  # ruff: ignore[blocking-path-method-in-async-function]
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    mod_ids = prepare_shared(cluster_path)
    logger.info("Found {mods} Workshop mod(s).", mods=len(mod_ids))
    setup = cluster_path / "mods" / "dedicated_server_mods_setup.lua"
    if not update or not (mod_ids or has_setup_code(setup)):
        return
    proxy = os.environ.get("DST_SERVER_MOD_PROXY") or None
    activate(install_path, cluster_path)
    await native.update(
        executable,
        cluster_path / "mods" / "ugc",
        proxy=proxy,
        log_handler=_log_update,
    )
