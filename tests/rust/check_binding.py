"""Run with the isolated wheel interpreter, rpc_fixture and optional native CLI."""

# Trusted executable paths are passed separately; checks run without pytest.
# Polling observes state owned by the separate native fixture process.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print, pytest-assert-in-except, async-busy-wait]

import asyncio
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
from itertools import product
from pathlib import Path

from dst_server import Client, DstError, describe


async def check_subscription(client: Client, socket: str) -> None:
    baseline = len(await asyncio.to_thread(os.listdir, "/proc/self/fd"))
    stream = await (await Client.connect(socket)).subscribe("events")
    observed = await client.call("status")
    assert await stream.next() == {
        "records": [observed],
        "dropped": 0,
        "closed": False,
    }
    waiting = asyncio.create_task(stream.next())
    await asyncio.sleep(0)
    await stream.close()
    try:
        assert (await waiting)["closed"]
    except DstError as error:
        # Rust may accept close before the new Python task submits its read.
        assert error.code == "transport"
    while len(await asyncio.to_thread(os.listdir, "/proc/self/fd")) > baseline:
        await asyncio.sleep(0.01)

    stream = await client.subscribe("events")
    await client.close()
    try:
        await stream.next()
    except DstError as error:
        assert error.code == "transport"
    else:
        message = "explicitly closed connection still served a subscription"
        raise AssertionError(message)
    await stream.close()


async def check(socket: str) -> None:
    async with asyncio.timeout(5):
        client = await Client.connect(socket)
        assert await client.describe() == {
            "room": describe("room"),
            "shard": describe("shard"),
        }
        value = await client.call("status")
        assert value["exact"] == 9_007_199_254_740_993
        assert value["unsigned"] == 2**64 - 1
        assert value["huge"] == 2**100
        assert value["nested"] == [None, False, 0, "", {"float": 0.25}]
        arguments = {
            "configuration": {"number": 2**100, "empty": [None, {}, []]},
            "replace_permissions": True,
        }
        echoed = await client.call("configure", arguments)
        assert echoed["request"]["request"]["arguments"] == arguments
        frozen = frozendict(
            arguments | {"configuration": frozendict(arguments["configuration"])}
        )
        echoed = await client.call("configure", frozen)
        assert echoed["request"]["request"]["arguments"] == arguments
        for require_empty in (None, False, True):
            echoed = await client.call("regenerate", {"require_empty": require_empty})
            assert (
                echoed["request"]["request"]["arguments"]["require_empty"]
                is require_empty
            )
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
        try:
            await client.call(
                "give", {"userid": "KU_test", "item": "goldnugget", "count": 65}
            )
        except DstError as error:
            assert error.code == "invalid"
        else:
            message = "invalid request reached transport"
            raise AssertionError(message)
        await check_subscription(client, socket)


def check_interrupted_registration(directory: str, program: list[str]) -> None:
    path = str(Path(directory) / "starting.sock")
    arguments = [*program, "subscribe", "events", "--socket", path]
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(5)
        with subprocess.Popen(arguments) as child, listener.accept()[0] as peer:
            peer.settimeout(5)
            assert peer.recv(1)
            child.send_signal(signal.SIGINT)
            assert child.wait(timeout=2) == 0


def check_interrupted_input(program: list[str]) -> None:
    commands = (
        ["logs", "journal", "--query", "-"],
        ["logs", "telemetry", "-"],
        [
            "archive",
            "upload",
            "0",
            "--configuration",
            "-",
            "--bucket",
            "fixture",
            "--region",
            "us-east-1",
            "--endpoint",
            "http://127.0.0.1:9",
            "--access-key-id",
            "fixture",
            "--secret-access-key",
            "fixture",
        ],
    )
    for arguments, stop in product(commands, (signal.SIGINT, signal.SIGTERM)):
        child = subprocess.Popen([*program, *arguments], stdin=subprocess.PIPE)
        try:
            assert child.stdin is not None
            child.stdin.write(b"{")
            child.stdin.flush()
            deadline = time.monotonic() + 5
            while int.from_bytes(
                fcntl.ioctl(child.stdin, termios.FIONREAD, b"\0" * 4), sys.byteorder
            ):
                assert child.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.01)
            child.send_signal(stop)
            assert child.wait(timeout=5) == -stop
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            if child.stdin is not None:
                child.stdin.close()


def main() -> None:
    console_script = Path(sys.executable).with_name("dst-server")
    methods = json.loads(
        subprocess.check_output([console_script, "--json", "describe"], timeout=5)
    )
    assert methods == describe()
    assert any(method["name"] == "save" for method in methods)
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
            check_interrupted_registration(directory, programs[-1])
            for program in programs:
                check_interrupted_input(program)
            for program, shutdown_signal in product(
                programs, (signal.SIGINT, signal.SIGTERM)
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
