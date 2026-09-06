import os
import shutil

import pytest


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
