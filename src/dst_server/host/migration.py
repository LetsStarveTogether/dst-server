"""One-shot offline migration; normal operation only reads the current format."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import orjson

from dst_server.configuration.files import (
    configuration_file_exists,
    read_text,
    validate_directory,
)
from dst_server.deployment.application import (
    MASTER_COMMAND,
    QuadletApplication,
    container_stop_command,
)
from dst_server.deployment.models import ContainerUnit, PodUnit
from dst_server.deployment.quadlet import references_pod
from dst_server.rooms import CONTROL_FILE, Control, write_control

from .locking import room_lock

if TYPE_CHECKING:
    from pathlib import Path

    from .service import Host


def _application(directory: Path, number: int) -> QuadletApplication:
    pod = PodUnit.load(directory / f"dst-{number:03d}.pod")
    units = []
    for path in sorted(directory.glob("*.container")):
        if not references_pod(path, f"{pod.name}.pod"):
            continue
        unit = ContainerUnit.load(path)
        command = unit.exec
        if command[:1] == MASTER_COMMAND[:1] and command[1:2] in {
            ("master",),
            ("serve",),
        }:
            unit = unit.replace(exec=(command[0], "agent", *command[1:]))
        units.append(unit.replace(exec_stop=container_stop_command(unit)))
    masters = [unit for unit in units if unit.exec[:3] == MASTER_COMMAND]
    if len(masters) != 1:
        msg = f"expected one master for room {number:03d}"
        raise ValueError(msg)
    master = masters[0].replace(
        part_of=(f"{pod.name}-pod.service",),
        restart="on-failure",
        restart_sec=30,
        start_limit_interval_sec=600,
        start_limit_burst=3,
    )
    secondaries = tuple(
        unit.replace(
            after=(),
            part_of=(f"{master.name}.service",),
            restart="no",
            restart_sec=None,
            start_limit_interval_sec=None,
            start_limit_burst=None,
        )
        for unit in units
        if unit.name != master.name
    )
    return QuadletApplication(pod=pod, master=master, secondaries=secondaries)


def _obsolete_files(host: Host) -> tuple[Path, ...]:
    paths = [host.quadlet_dir / ".dst-operation.lock"]
    for number in host.rooms.numbers():
        directory = host.rooms.path(number)
        paths.append(directory / ".dst-mod-update.lock")
        paths.extend(
            path
            for path in directory.rglob(".last_login")
            if len(path.relative_to(directory).parts) == 5  # ruff: ignore[magic-value-comparison]
            and path.relative_to(directory).parts[1:3] == ("save", "session")
        )
    results = host.cluster_root / ".dst-maintenance"
    if results.exists() or results.is_symlink():
        validate_directory(results)
        paths.extend(
            path
            for path in results.iterdir()
            if re.fullmatch(r"dst-maintenance-[0-9a-f]{32}\.service\.json", path.name)
        )
    return tuple(sorted(path for path in paths if configuration_file_exists(path)))


async def migrate(host: Host, *, apply: bool = False) -> dict[str, Any]:  # ruff: ignore[complex-structure]
    """Preview every change; apply only while the entire installation is stopped."""
    validate_directory(host.cluster_root)
    validate_directory(host.quadlet_dir)
    controls = {}
    applications = {}
    for number in host.rooms.numbers():
        directory = host.rooms.path(number)
        validate_directory(directory)
        path = directory / CONTROL_FILE
        values = (
            orjson.loads(read_text(path)) if configuration_file_exists(path) else {}
        )
        if not isinstance(values, dict):
            msg = f"expected a control object: {path}"
            raise TypeError(msg)
        for key in ("revision", "override", "until"):
            values.pop(key, None)
        controls[number] = Control.model_validate_json(orjson.dumps(values))
        application = _application(host.quadlet_dir, number)
        application.validate_updates(host.quadlet_dir)
        applications[number] = application
    obsolete = _obsolete_files(host)
    result = {
        "status": "preview",
        "rooms": {
            number: {
                "control": str(host.rooms.path(number) / CONTROL_FILE),
                "deployment": [
                    str(host.quadlet_dir / path) for path in application.files()
                ],
            }
            for number, application in applications.items()
        },
        "remove": [str(path) for path in obsolete],
    }
    if not apply:
        return result
    tasks = await host.systemd.list_patterns(("dst-maintenance-*.service",))
    if any(
        state.active not in {"inactive", "failed"} or state.job_id
        for state in tasks.values()
    ):
        msg = "migration requires stopped legacy maintenance tasks"
        raise RuntimeError(msg)
    for number in controls:
        if await host._running_units(number):  # ruff: ignore[private-member-access]
            msg = f"migration requires stopped room services: {number:03d}"
            raise RuntimeError(msg)
    for number, control in controls.items():
        async with room_lock(host.rooms.path(number)):
            applications[number].save(host.quadlet_dir)
            write_control(host.rooms.path(number), control)
    for path in obsolete:
        path.unlink()
    results = host.cluster_root / ".dst-maintenance"
    if results.is_dir() and not any(results.iterdir()):
        results.rmdir()
    await host.systemd.reload()
    result["status"] = "migrated"
    return result
