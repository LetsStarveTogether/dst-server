import asyncio
from pathlib import Path

import pytest

from dst_server.cluster.controller import ClusterController
from tests.cluster.helpers import controller


async def test_failed_setup_reclaims_registered_agent_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = asyncio.all_tasks()

    async def fail(_self: ClusterController) -> None:  # ruff: ignore[unused-async]
        message = "setup failed after registration"
        raise RuntimeError(message)

    monkeypatch.setattr(ClusterController, "wait_idle", fail)
    with pytest.raises(RuntimeError, match="setup failed after registration"):
        await controller(tmp_path, monkeypatch)
    assert asyncio.all_tasks() <= existing
