"""The packaged command-line entry point."""

import asyncio
import os
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from cyclopts.exceptions import CycloptsError
from rich.console import Console

from .common import BatchFailure, Options, options

app = App(
    name="dst-server",
    version=lambda: version("dst-server"),
    result_action="return_value",
)


def _commands() -> None:
    commands = {
        "room": ("rooms", "Create, configure and run rooms."),
        "template": ("rooms", "Inspect and apply gameplay templates."),
        "deployment": ("rooms", "Install the LST fleet and automation."),
        "mod": ("rooms", "Configure and update room mods."),
        "schedule": ("operations", "Manage daily room opening hours."),
        "maintenance": ("operations", "Run maintenance and inspect tasks."),
        "agent": ("operations", "Run game agents inside containers."),
        "player": ("game", "Inspect players and manage permissions."),
        "world": ("game", "Inspect and operate game worlds."),
        "console": ("game", "Evaluate Lua through a game agent."),
        "logs": ("game", "Read historical and live journal logs."),
        "rpc": ("game", "Discover and call game RPC methods."),
    }
    for name, (module, help_text) in commands.items():
        app.command(f"dst_server.cli.{module}:{name}_app", name=name, help=help_text)
    for name, module, help_text in (
        ("announce", "game", "Broadcast a message to selected rooms."),
        ("annotations", "operations", "Generate Lua language-server annotations."),
        ("completion", "operations", "Print a shell completion script."),
    ):
        app.command(f"dst_server.cli.{module}:{name}", name=name, help=help_text)


@app.meta.default
async def _launch(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    cluster_root: Annotated[Path, Parameter(env_var="DST_SERVER_CLUSTER_ROOT")] = Path(
        "/srv/dst"
    ),
    quadlet_dir: Annotated[Path, Parameter(env_var="DST_SERVER_QUADLET_DIR")] = Path(
        "/etc/containers/systemd"
    ),
    json: bool = False,
) -> int:
    token = options.set(Options(cluster_root.absolute(), quadlet_dir.absolute(), json))
    try:
        result = await app.run_async(
            tokens or ("--help",), exit_on_error=False, print_error=False
        )
        return result if isinstance(result, int) else 0
    finally:
        options.reset(token)


def main(argv: Sequence[str] | None = None) -> int:
    if "room" not in app:
        _commands()
    try:
        return (
            asyncio.run(
                app.meta.run_async(argv, exit_on_error=False, print_error=False)
            )
            or 0
        )
    except BatchFailure:
        return 1
    except CycloptsError as error:
        Console(stderr=True, highlight=False).print(str(error), markup=False)
        return 2
    except KeyboardInterrupt:
        return 130
    except (Exception, asyncio.CancelledError) as error:
        if os.environ.get("DST_SERVER_DEBUG"):
            raise
        Console(stderr=True, highlight=False).print(
            str(error) or type(error).__name__, markup=False
        )
        return 1


__all__ = ["app", "main"]
