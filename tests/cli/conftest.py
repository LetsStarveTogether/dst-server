from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import SecretStr

from dst_server.host import Host, systemd
from dst_server.host.systemd import Systemd, UnitStatus
from dst_server.presets.lst import TOKEN_ENVIRONMENT, fleet_room
from dst_server.rpc import ClusterClient, ShardClient


@pytest.fixture
def cli_systemd(monkeypatch: pytest.MonkeyPatch) -> Mock:
    manager = Mock(spec=Systemd)
    states: dict[str, str] = {}

    def list_units(names: Sequence[str]) -> dict[str, UnitStatus]:
        return {
            name: UnitStatus(
                name, "loaded", states.get(name, "inactive"), "dead", 0, "", "/"
            )
            for name in names
        }

    manager.list_units.side_effect = list_units
    manager.start.side_effect = lambda name: (
        states.__setitem__(name, "active") or "start-job"
    )
    manager.stop.side_effect = lambda name: (
        states.__setitem__(name, "inactive") or "stop-job"
    )
    monkeypatch.setattr(systemd, "Systemd", lambda: manager)
    return manager


@pytest.fixture
def cli_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_systemd: Mock
) -> Host:
    root, quadlets = tmp_path / "rooms", tmp_path / "quadlets"
    monkeypatch.setenv("DST_SERVER_CLUSTER_ROOT", str(root))
    monkeypatch.setenv("DST_SERVER_QUADLET_DIR", str(quadlets))
    monkeypatch.setenv(TOKEN_ENVIRONMENT, "test-cluster-token")
    return Host(root, quadlets, systemd=cli_systemd)


@pytest.fixture
def rpc_clients(
    cli_host: Host, monkeypatch: pytest.MonkeyPatch
) -> defaultdict[int, MagicMock]:
    def client() -> MagicMock:
        connection = MagicMock(spec=ClusterClient)
        connection.__aenter__.return_value = connection
        connection.invoke.return_value = None
        connection.status.return_value = SimpleNamespace(master="forest")
        connection.shard.return_value = MagicMock(spec=ShardClient)
        connection.shard.return_value.name = "cave"
        connection.shard.return_value.invoke.return_value = None
        return connection

    clients: defaultdict[int, MagicMock] = defaultdict(client)
    for number in (0, 1):
        definition = fleet_room(number, token=SecretStr("test-token"))
        cli_host.rooms.save(definition)

    def connect(path: Path) -> MagicMock:
        number = int(path.parent.name)
        assert path == cli_host.rooms.path(number) / ".dst-server.sock"
        if not path.parent.is_dir():
            raise FileNotFoundError(path)
        return clients[number]

    monkeypatch.setattr(ClusterClient, "connect", AsyncMock(side_effect=connect))
    return clients
