from __future__ import annotations

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
