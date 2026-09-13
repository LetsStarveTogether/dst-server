import os

import pytest

from dst_server.telemetry import otel


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(otel, "_globals_installed", True)
