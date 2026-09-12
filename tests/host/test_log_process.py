import asyncio
import sys

import pytest

from dst_server.logs import _process


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_pipe_failure_cannot_interrupt_child_reaping(
    monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    monkeypatch.setattr(_process, "_CLOSE_TIMEOUT", 0.02)
    command = (
        sys.executable,
        "-c",
        (
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "print('ready',flush=True);time.sleep(60)"
        ),
    )
    failure = OSError("injected pipe failure")
    async with asyncio.timeout(3):
        with pytest.raises(OSError, match="injected pipe failure"):  # ruff: ignore[pytest-raises-with-multiple-statements]
            async with _process.log_process(command) as output:
                assert await anext(output) == b"ready\n"
                getattr(output, stream).set_exception(failure)
    assert output.process.returncode == -9
    assert output._stderr_task.done()
