import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import logbook
import pytest
from ulid import ULID

from tests.system.helpers import CLEANUP_TIMEOUT, IMAGE, remove_container
from tests.system.quadlet import HAS_QUADLET_RUNTIME, QuadletSystem


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def capture_game_logs() -> Iterator[None]:
    with logbook.StderrHandler(level=logbook.INFO).applicationbound():
        yield


@pytest.fixture(scope="session", autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def image_matches_expected_build() -> None:
    expected_revision = os.environ.get("DST_SERVER_EXPECTED_REVISION")
    expected_version = os.environ.get("DST_SERVER_EXPECTED_VERSION")
    podman = shutil.which("podman")
    assert podman is not None
    inspected = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        (
            podman,
            "image",
            "inspect",
            "--format",
            (
                "{{.Config.User}}|"
                '{{ index .Labels "org.opencontainers.image.revision" }}|'
                '{{ index .Labels "org.opencontainers.image.version" }}'
            ),
            IMAGE,
        ),
        capture_output=True,
        text=True,
        timeout=CLEANUP_TIMEOUT,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr or inspected.stdout
    user, revision, version = inspected.stdout.strip().split("|", maxsplit=2)
    assert user == "steam"
    if expected_revision is not None:
        assert revision == expected_revision
    if expected_version is not None:
        assert version == expected_version


@pytest.fixture
async def container_name() -> AsyncIterator[str]:
    name = f"dst-sdk-test-{str(ULID()).lower()}"
    yield name
    await remove_container(name)


@pytest.fixture
async def quadlet_system(tmp_path: Path) -> AsyncIterator[QuadletSystem]:
    if not HAS_QUADLET_RUNTIME:
        pytest.skip("systemd Quadlet runtime is unavailable")
    system = QuadletSystem.create(tmp_path)
    try:
        await system.install()
        yield system
    finally:
        await system.cleanup()
