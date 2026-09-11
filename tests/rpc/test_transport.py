# ruff: file-ignore[blocking-path-method-in-async-function, invalid-argument-name]
import asyncio
import json
import socket
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ulid import ULID

from dst_server import commands as c
from dst_server.errors import (
    DisconnectedError,
    ErrorCode,
    ErrorInfo,
    IndeterminateError,
    RemoteError,
)
from dst_server.models.cluster import ClusterStatus
from dst_server.rpc import (
    ClusterClient,
    filesystem_rpc_server,
    load_schema,
    rpc_runtime,
)
from dst_server.rpc import transport as rpc_transport
from dst_server.rpc.codec import encode, encode_model, failure, success
from dst_server.rpc.transport import filesystem_socket
from tests.helpers import wait_for_event

capnp: Any = pytest.importorskip("capnp")
schema = load_schema()


@pytest.fixture
async def callback_errors() -> AsyncIterator[list[dict[str, Any]]]:
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    try:
        yield errors
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.parametrize("replacement", [False, True], ids=["owned", "replaced"])
def test_filesystem_socket_is_private_exclusive_and_inode_safe(
    tmp_path: Path,
    replacement: bool,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "cluster.sock"

    with filesystem_socket(path):
        metadata = path.stat(follow_symlinks=False)
        assert stat.S_ISSOCK(metadata.st_mode)
        assert metadata.st_mode & 0o777 == 0o600
        with (
            pytest.raises(FileExistsError, match="another RPC server"),
            filesystem_socket(tmp_path / "other.sock"),
        ):
            pass
        if replacement:
            path.unlink()
            path.write_text("replacement")

    if replacement:
        assert path.read_text() == "replacement"
    else:
        assert not path.exists()


def test_filesystem_socket_replaces_only_an_owned_stale_socket(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "cluster.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()

    with filesystem_socket(path):
        assert stat.S_ISSOCK(path.stat().st_mode)
    assert not path.exists()


@pytest.mark.parametrize("entry", ["file", "symlink"])
def test_filesystem_socket_preserves_unsafe_existing_entries(
    tmp_path: Path,
    entry: str,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "cluster.sock"
    target = tmp_path / "target"
    target.write_text("untouched")
    if entry == "file":
        path.write_text("untouched")
    else:
        path.symlink_to(target)

    with (
        pytest.raises(FileExistsError, match="refusing to replace"),
        filesystem_socket(path),
    ):
        pass
    assert path.read_text() == "untouched"


def test_filesystem_socket_rejects_world_writable_parent(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)
    with (
        pytest.raises(PermissionError, match="owner-controlled"),
        filesystem_socket(tmp_path / "cluster.sock"),
    ):
        pass


@pytest.mark.parametrize("name", ["bad\0socket", "x" * 108])
def test_filesystem_socket_rejects_invalid_unix_paths(
    tmp_path: Path,
    name: str,
) -> None:
    tmp_path.chmod(0o700)
    with (
        pytest.raises(ValueError, match="invalid Unix socket path"),
        filesystem_socket(tmp_path / name),
    ):
        pass


async def test_real_socket_success_error_and_root_cleanup(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    status = ClusterStatus(epoch=ULID(), phase="stopped", master="Master", shards=())
    closed = asyncio.Event()

    class Cluster(schema.Cluster.Server):
        async def call(self, request: bytes, _context: Any) -> None:
            assert request
            _context.results.result = success(encode_model(status))

        async def shard(self, shardName: str, _context: Any) -> None:
            _context.results.result = failure(
                ErrorInfo(ErrorCode.NOT_FOUND, ULID(), shardName)
            )

    class Bootstrap(schema.Bootstrap.Server):
        def __init__(self) -> None:
            self.cluster = Cluster()

        async def connect(self, _context: Any) -> None:
            _context.results.result = success(self.cluster)

        async def aclose(self) -> None:
            closed.set()

    path = tmp_path / "cluster.sock"
    async with (
        asyncio.timeout(5),
        rpc_runtime(),
        filesystem_rpc_server(path, Bootstrap) as server,
    ):
        async with await ClusterClient.connect(path) as client:
            assert await client.status() == status
            assert client.shard("Master") is client.shard("Master")
            with pytest.raises(RemoteError) as missing:
                await client.shard("Missing").status()
            assert missing.value.error.code is ErrorCode.NOT_FOUND

        async with asyncio.timeout(1):
            await closed.wait()
        assert not server.connections

    assert not path.exists()


@pytest.mark.parametrize("mutation", [False, True])
async def test_raw_call_uses_server_metadata_for_locally_unknown_methods(
    tmp_path: Path, mutation: bool
) -> None:
    tmp_path.chmod(0o700)
    entered, release = asyncio.Event(), asyncio.Event()
    method = c.MethodDescription(
        name="future_method",
        scope="cluster",
        description="A method known only to the server.",
        arguments_schema={"type": "object"},
        result_schema={"type": "string"},
        default_timeout=0.125,
        mutation=mutation,
    )

    class Cluster(schema.Cluster.Server):
        async def describe(self, _context: Any) -> None:
            _context.results.result = success(encode(c.METHOD_DESCRIPTIONS, (method,)))

        async def call(self, request: bytes, _context: Any) -> None:
            payload = json.loads(request)
            assert payload == {
                "method": "future_method",
                "arguments": {"value": "hello"},
                "timeout": 0.125,
            }
            entered.set()
            await release.wait()

    class Bootstrap(schema.Bootstrap.Server):
        async def connect(self, _context: Any) -> None:
            _context.results.result = success(Cluster())

    with pytest.raises(ValueError, match="not available"):
        c.operation("cluster", method.name)
    path = tmp_path / "cluster.sock"
    watchdog = asyncio.timeout(5)
    async with (
        watchdog,
        rpc_runtime(),
        filesystem_rpc_server(path, Bootstrap),
        await ClusterClient.connect(path) as client,
    ):
        assert await client.describe() == (method,)
        pending = asyncio.create_task(client.call(method.name, {"value": "hello"}))
        try:
            await wait_for_event(entered, pending)
            pending.cancel()
            done, _ = await asyncio.wait((pending,), timeout=1)
            assert pending in done
            with pytest.raises(asyncio.CancelledError) as cancelled:
                pending.result()
            assert pending.cancelled()
            assert getattr(cancelled.value, "__notes__", []) == (
                [
                    (
                        "RPC mutation result could not be confirmed; "
                        "the operation may still be running."
                    )
                ]
                if mutation
                else []
            )
        finally:
            release.set()
            pending.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(pending, return_exceptions=True)
    assert not watchdog.expired(), "RPC test exceeded its watchdog"


@pytest.mark.parametrize(
    ("method", "expected"),
    [("status", DisconnectedError), ("start", IndeterminateError)],
)
async def test_disconnect_classifies_queries_and_mutations(
    tmp_path: Path,
    method: str,
    expected: type[BaseException],
) -> None:
    tmp_path.chmod(0o700)
    entered = asyncio.Event()
    release = asyncio.Event()

    class Cluster(schema.Cluster.Server):
        async def call(self, request: bytes, _context: Any) -> None:
            assert request
            entered.set()
            await release.wait()
            _context.results.result = success()

    class Bootstrap(schema.Bootstrap.Server):
        async def connect(self, _context: Any) -> None:
            _context.results.result = success(Cluster())

    path = tmp_path / "cluster.sock"
    async with (
        asyncio.timeout(5),
        rpc_runtime(),
        filesystem_rpc_server(path, Bootstrap) as server,
    ):
        client = await ClusterClient.connect(path)
        pending = asyncio.create_task(getattr(client, method)())
        try:
            await wait_for_event(entered, pending)
            for connection, stream in tuple(server.connections):
                connection.close()
                stream.close()
            with pytest.raises(expected):
                await asyncio.wait_for(asyncio.shield(pending), timeout=1)
        finally:
            release.set()
            client.close()
            pending.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(pending, return_exceptions=True)


async def test_server_shutdown_bounds_capability_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback_errors: list[dict[str, Any]],
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rpc_transport, "_CLOSE_TIMEOUT", 0.01)
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    class Cluster(schema.Cluster.Server):
        pass

    class Bootstrap(schema.Bootstrap.Server):
        async def connect(self, _context: Any) -> None:
            _context.results.result = success(Cluster())

        async def aclose(self) -> None:
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

    path = tmp_path / "cluster.sock"
    async with asyncio.timeout(5), rpc_runtime():
        client = None
        try:
            async with filesystem_rpc_server(path, Bootstrap) as server:
                client = await ClusterClient.connect(path)
        finally:
            if client is not None:
                client.close()

    assert cleanup_started.is_set()
    assert cleanup_cancelled.is_set()
    assert not server.connections
    assert not server.tasks
    assert not path.exists()
    assert callback_errors == []


async def test_failed_connection_bootstrap_is_owned_and_reaped(
    tmp_path: Path,
    callback_errors: list[dict[str, Any]],
) -> None:
    tmp_path.chmod(0o700)
    accepted = asyncio.Event()

    def bootstrap() -> object:
        accepted.set()
        message = "bootstrap failed"
        raise RuntimeError(message)

    path = tmp_path / "cluster.sock"
    async with (
        asyncio.timeout(5),
        rpc_runtime(),
        filesystem_rpc_server(path, bootstrap) as server,
    ):
        stream = await capnp.AsyncIoStream.create_unix_connection(str(path))
        try:
            await wait_for_event(accepted)
            while server.tasks:  # ruff: ignore[async-busy-wait]
                await asyncio.sleep(0)
        finally:
            stream.close()
        assert not server.connections
        assert not server.tasks

    assert callback_errors == []
