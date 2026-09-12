import os
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--scripts-zip", help="Run native Lua contracts against this game's scripts.zip"
    )


@pytest.fixture(scope="session")
def pinned_scripts() -> Path:
    return Path(__file__).parents[1] / "dst-scripts/scripts"


@pytest.fixture(scope="session")
def native_scripts(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    pinned_scripts: Path,
) -> Path:
    source = request.config.getoption("--scripts-zip")
    if source is None:
        return pinned_scripts
    directory = tmp_path_factory.mktemp("native-scripts")
    with ZipFile(source) as archive:
        archive.extractall(directory)
    scripts = directory / "scripts"
    if not (scripts / "json.lua").is_file():
        pytest.fail("scripts.zip must contain scripts/json.lua")
    return scripts


@pytest.fixture(scope="session")
def luajit() -> str:
    executable = shutil.which("luajit")
    if executable is None and os.environ.get("CI"):
        pytest.fail("LuaJIT is required in CI")
    if executable is None:
        pytest.skip("LuaJIT is not installed")
    return executable


@pytest.fixture(scope="session", params=["lua5.1", "luajit"])
def lua_runtime(request: pytest.FixtureRequest) -> str:
    runtime = str(request.param)
    executable = shutil.which(runtime)
    if executable is None and os.environ.get("CI"):
        pytest.fail(f"{runtime} is required in CI")
    if executable is None:
        pytest.skip(f"{runtime} is not installed")
    return executable
