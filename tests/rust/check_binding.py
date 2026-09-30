"""Run with the isolated wheel interpreter, rpc_fixture and optional native CLI."""

# Trusted executable paths are passed separately; checks run without pytest.
# Polling observes state owned by the separate native fixture process.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print, pytest-assert-in-except, async-busy-wait]

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dst_server import Client, DstError, describe


async def check(socket: str) -> None:
    async with asyncio.timeout(5):
        client = await Client.connect(socket)
        value = await client.call("status")
        assert value["exact"] == 9_007_199_254_740_993
        assert value["nested"] == [None, False, 0, "", {"float": 0.25}]
        operation = asyncio.create_task(client.call("save"))
        while (state := await client.call("status"))["accepted"] == value["accepted"]:
            await asyncio.sleep(0.01)
        assert state["accepted"] == value["accepted"] + 1
        assert state["completed"] == value["completed"]
        assert not operation.done()
        operation.cancel()
        result = await asyncio.gather(operation, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        await client.close()
        client = await Client.connect(socket)
        assert (await client.call("status"))["completed"] == value["completed"]
        await client.call("start")  # Release the fixture's accepted save.
        while (state := await client.call("status"))["completed"] == value["completed"]:
            await asyncio.sleep(0.01)
        assert state["completed"] == value["completed"] + 1
        assert state["accepted"] == value["accepted"] + 1
        stream = await client.subscribe("events")
        waiting = asyncio.create_task(stream.next())
        await asyncio.sleep(0)
        await stream.close()
        assert (await waiting)["closed"]
        try:
            await client.call(
                "give", {"userid": "KU_test", "item": "goldnugget", "count": 65}
            )
        except DstError as error:
            assert error.code == "invalid"
        else:
            message = "invalid request reached transport"
            raise AssertionError(message)
        await client.close()


def main() -> None:
    assert any(method["name"] == "save" for method in describe())
    with tempfile.TemporaryDirectory(prefix="dst-python-") as directory:
        socket = str(Path(directory) / "agent.sock")
        process = subprocess.Popen([sys.argv[1], socket], start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not Path(socket).exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.01)
            asyncio.run(check(socket))
            asyncio.run(
                check(socket)
            )  # A new Python event loop shares native RPC resources.
            descriptors = Path(f"/proc/{process.pid}/fd")
            baseline = len(list(descriptors.iterdir()))
            code = (
                "import asyncio; from dst_server import Client; "
                "asyncio.run(Client.connect(" + repr(socket) + "))"
            )
            subprocess.run([sys.executable, "-c", code], check=True, timeout=5)
            deadline = time.monotonic() + 5
            while len(list(descriptors.iterdir())) != baseline:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            programs = [[sys.executable, "-I", "-m", "dst_server"]]
            if len(sys.argv) > 2:
                programs.append(sys.argv[2:])
            for program, shutdown_signal in (
                (program, stop)
                for program in programs
                for stop in (signal.SIGINT, signal.SIGTERM)
            ):
                with subprocess.Popen(
                    [
                        *program,
                        "subscribe",
                        "events",
                        "--socket",
                        socket,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                ) as subscriber:
                    deadline = time.monotonic() + 5
                    while len(list(descriptors.iterdir())) <= baseline:
                        assert subscriber.poll() is None
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    subscriber.send_signal(shutdown_signal)
                    _, diagnostics = subscriber.communicate(timeout=5)
                    assert subscriber.returncode == 0, diagnostics
                deadline = time.monotonic() + 5
                while len(list(descriptors.iterdir())) != baseline:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=5)
            assert process.returncode == 0
    print(
        "Python native transport, cancellation, loop replacement and exit checks passed"
    )


if __name__ == "__main__":
    main()
