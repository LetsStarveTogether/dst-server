from pathlib import Path
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from pydantic import SecretStr

from dst_server.deployment.application import QuadletApplication
from dst_server.host.migration import migrate
from dst_server.host.service import Host
from dst_server.host.systemd import UnitStatus
from dst_server.presets.lst import fleet_room
from dst_server.rooms import CONTROL_FILE, read_control


@pytest.fixture
def legacy(tmp_path: Path) -> Host:
    systemd = Mock(
        list_units=AsyncMock(return_value={}),
        list_patterns=AsyncMock(return_value={}),
        reload=AsyncMock(),
    )
    host = Host(tmp_path / "rooms", tmp_path / "quadlets", systemd=systemd)
    host.rooms.save(fleet_room(0, token=SecretStr("private-token")))
    directory = host.rooms.path(0)
    control = orjson.loads((directory / CONTROL_FILE).read_bytes())
    control.update(revision=12, override=False, until=None, paused=True)
    (directory / CONTROL_FILE).write_bytes(orjson.dumps(control))
    application = QuadletApplication.load(host.quadlet_dir, name="dst-000")
    for unit in (application.master, *application.secondaries):
        unit.replace(
            exec=(unit.exec[0], *unit.exec[2:]),
            exec_stop=(),
            after=()
            if unit is application.master
            else (f"{application.master.name}.container",),
            part_of=(),
            restart="on-failure",
            restart_sec=None,
            start_limit_interval_sec=None,
            start_limit_burst=None,
        ).save(host.quadlet_dir)
    world = directory / "forest/save/session/ABC/.last_login"
    world.parent.mkdir(parents=True)
    world.write_text("2026-09-18T00:00:00+00:00\n")
    (world.parent / "world").write_bytes(b"unchanged native save")
    (directory / "forest/worldgenoverride.lua").write_text("return require('world')\n")
    (directory / ".dst-mod-update.lock").touch()
    (host.quadlet_dir / ".dst-operation.lock").touch()
    tasks = host.cluster_root / ".dst-maintenance"
    tasks.mkdir()
    (tasks / ("dst-maintenance-" + "a" * 32 + ".service.json")).write_text("{}")
    return host


def contents(host: Host) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for root in (host.cluster_root, host.quadlet_dir)
        for path in root.rglob("*")
        if path.is_file()
    }


async def test_preview_is_read_only_and_apply_preserves_native_data(
    legacy: Host,
) -> None:
    before = contents(legacy)
    preview = await migrate(legacy)
    assert preview["status"] == "preview"
    assert len(preview["remove"]) == 4
    assert contents(legacy) == before
    legacy.systemd.reload.assert_not_awaited()
    result = await migrate(legacy, apply=True)
    assert result["status"] == "migrated"
    control = read_control(legacy.rooms.path(0))
    assert control.paused
    assert control.activity is None
    application = QuadletApplication.load(legacy.quadlet_dir, name="dst-000")
    assert application.master.restart_sec == 30
    assert application.master.start_limit_burst == 3
    assert application.master.exec[:3] == (
        "/app/.venv/bin/dst-server",
        "agent",
        "master",
    )
    assert application.secondaries[0].restart == "no"
    assert application.secondaries[0].after == ()
    for unit in (application.master, *application.secondaries):
        assert unit.exec_stop == (
            "-/usr/bin/podman",
            "kill",
            "--signal",
            "TERM",
            unit.container_name,
        )
    for path, content in before.items():
        if str(path) in preview["remove"]:
            assert not path.exists()
        elif path.name != CONTROL_FILE and path.parent != legacy.quadlet_dir:
            assert path.read_bytes() == content
    assert not (legacy.cluster_root / ".dst-maintenance").exists()
    legacy.systemd.reload.assert_awaited_once()
    assert "private-token" not in str(result)
    assert (await migrate(legacy))["remove"] == []


@pytest.mark.parametrize("busy", ["room", "task", "queued"])
async def test_apply_requires_stopped_rooms_and_legacy_tasks(
    legacy: Host, busy: str
) -> None:
    before = contents(legacy)
    unit = "dst-000-forest.service" if busy == "room" else "dst-maintenance-old.service"
    state = UnitStatus(
        unit,
        "loaded",
        "inactive" if busy == "queued" else "active",
        "running",
        int(busy == "queued"),
        "start" if busy == "queued" else "",
        "/",
    )
    operation = (
        legacy.systemd.list_units if busy == "room" else legacy.systemd.list_patterns
    )
    operation.return_value = {unit: state}
    with pytest.raises(RuntimeError, match="requires stopped"):
        await migrate(legacy, apply=True)
    assert contents(legacy) == before
    legacy.systemd.reload.assert_not_awaited()


async def test_migration_rejects_unknown_control_fields_without_rewriting(
    legacy: Host,
) -> None:
    path = legacy.rooms.path(0) / CONTROL_FILE
    values = orjson.loads(path.read_bytes())
    values["unknown"] = "must not silently drop"
    path.write_bytes(orjson.dumps(values))
    before = contents(legacy)
    with pytest.raises(ValueError, match="Extra inputs"):
        await migrate(legacy, apply=True)
    assert contents(legacy) == before


async def test_migration_rejects_symlink_artifacts(
    legacy: Host, tmp_path: Path
) -> None:
    artifact = legacy.rooms.path(0) / ".dst-mod-update.lock"
    artifact.unlink()
    target = tmp_path / "outside"
    target.write_text("keep")
    artifact.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        await migrate(legacy, apply=True)
    assert target.read_text() == "keep"


async def test_migration_keeps_schedules_and_existing_activity_on_rerun(
    legacy: Host,
) -> None:
    path = legacy.rooms.path(0) / CONTROL_FILE
    values = orjson.loads(path.read_bytes())
    values["schedule"] = [{"start": "18:00:00", "end": "00:00:00"}]
    values["activity"] = {
        "sessions": {"forest": "ABC"},
        "last_active_at": "2026-09-18T00:00:00Z",
    }
    path.write_bytes(orjson.dumps(values))
    await migrate(legacy, apply=True)
    control = read_control(legacy.rooms.path(0))
    assert control.schedule[0].start.hour == 18
    assert control.activity is not None
    assert control.activity.sessions == {"forest": "ABC"}
    before = path.read_bytes()
    await migrate(legacy, apply=True)
    assert path.read_bytes() == before
