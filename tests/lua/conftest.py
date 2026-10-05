"""Run each Lua scenario in a fresh native interpreter process."""

# Repository fixtures and executable names are passed as separate arguments.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]
import json
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--binary",
        type=Path,
        default=ROOT / "target/debug/dst-server",
        help="Native SDK executable used to validate game customization choices",
    )
    parser.addoption(
        "--scripts",
        type=Path,
        default=ROOT / "dst-scripts/scripts",
        help="Native DST scripts directory, including scripts extracted from an image",
    )


def invoke(
    runtime: str, fixture: Path, *arguments: object, source: bytes = b""
) -> bytes:
    result = subprocess.run(
        [runtime, str(fixture), *(str(argument) for argument in arguments)],
        input=source,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    for line in result.stdout.splitlines():
        if line.startswith(b"DST_OTEL|"):
            record = json.loads(line.removeprefix(b"DST_OTEL|"))
            assert record["v"] == 3
            assert isinstance(record["data"], dict)
            assert isinstance(record["seq"], int)
    return result.stdout


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "driver_scenario" in metafunc.fixturenames:
        names = (
            invoke(
                "lua5.1",
                ROOT / "tests/lua/driver_spec.lua",
                ROOT,
                "--list",
                metafunc.config.getoption("scripts").resolve(),
            )
            .decode()
            .splitlines()
        )
        assert names, "driver_spec.lua must expose its scenarios"
        metafunc.parametrize("driver_scenario", names)


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def scripts(pytestconfig: pytest.Config) -> Path:
    return pytestconfig.getoption("scripts").resolve()


@pytest.fixture(scope="session")
def binary(pytestconfig: pytest.Config) -> Path:
    path = pytestconfig.getoption("binary").resolve()
    assert path.is_file(), (
        "Build the SDK with cargo build -p dst-server --bin dst-server"
    )
    return path


@pytest.fixture(scope="session", params=["lua5.1", "luajit"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(scope="session")
def lua(runtime: str) -> Callable[..., bytes]:
    return partial(invoke, runtime)
