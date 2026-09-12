import os
import shutil
from collections.abc import Iterator
from zipfile import ZipFile

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--scripts-zip", help="Run native Lua contracts against this game's scripts.zip"
    )


@pytest.fixture(scope="session", autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def native_script_source(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    source = request.config.getoption("--scripts-zip")
    with pytest.MonkeyPatch.context() as patch:
        if source is not None:
            directory = tmp_path_factory.mktemp("native-scripts")
            with ZipFile(source) as archive:
                archive.extractall(directory)
            scripts = directory / "scripts"
            if not (scripts / "json.lua").is_file():
                pytest.fail("scripts.zip must contain scripts/json.lua")
            patch.setenv("DST_SERVER_TEST_SCRIPTS", str(scripts))
        yield


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
