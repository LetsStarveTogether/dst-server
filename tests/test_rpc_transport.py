# ruff: file-ignore[blocking-path-method-in-async-function, invalid-argument-name]
import asyncio
import socket
import stat
from pathlib import Path
from typing import Any

import pytest
from ulid import ULID

from dst_server.rpc import (
    SCHEMA_FINGERPRINT,
    ClusterClient,
    DisconnectedError,
    IndeterminateError,
    RemoteError,
    filesystem_rpc_server,
    load_schema,
    rpc_runtime,
)
from dst_server.rpc import transport as rpc_transport
from dst_server.rpc.codec import encode_model
from dst_server.rpc.errors import ErrorCode, ErrorInfo, failure, success, unwrap_outcome
from dst_server.rpc.models import ClusterStatus
from dst_server.rpc.transport import filesystem_socket

capnp: Any = pytest.importorskip("capnp")
schema = load_schema()


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


async def test_real_socket_handshake_success_error_and_root_cleanup(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    status = ClusterStatus(epoch=ULID(), phase="stopped", master="Master", shards=())
    closed: list[asyncio.Event] = []

    class Cluster(schema.Cluster.Server):
        async def status(self, _context: Any) -> None:
            _context.results.result = success(encode_model(status))

        async def shard(self, shardName: str, _context: Any) -> None:
            _context.results.result = failure(
                ErrorInfo(ErrorCode.NOT_FOUND, ULID(), shardName)
            )

    class Bootstrap(schema.Bootstrap.Server):
        def __init__(self) -> None:
            self.cluster = Cluster()
            self.closed = asyncio.Event()
            closed.append(self.closed)

        async def connect(self, schemaFingerprint: str, _context: Any) -> None:
            _context.results.result = (
                success(self.cluster)
                if schemaFingerprint == SCHEMA_FINGERPRINT
                else failure(
                    ErrorInfo(
                        ErrorCode.INCOMPATIBLE_SCHEMA,
                        ULID(),
                        "schema mismatch",
                    )
                )
            )

        async def aclose(self) -> None:
            self.closed.set()

    path = tmp_path / "cluster.sock"
    async with rpc_runtime(), filesystem_rpc_server(path, Bootstrap) as server:
        stream = await capnp.AsyncIoStream.create_unix_connection(str(path))
        wire = capnp.TwoPartyClient(stream)
        bootstrap = wire.bootstrap().cast_as(schema.Bootstrap)
        incompatible = await bootstrap.connect(schemaFingerprint="0" * 64)
        with pytest.raises(RemoteError) as mismatch:
            unwrap_outcome(incompatible.result)
        assert mismatch.value.error.code is ErrorCode.INCOMPATIBLE_SCHEMA
        wire.close()
        stream.close()
        async with asyncio.timeout(1):
            await closed[0].wait()

        async with await ClusterClient.connect(path) as client:
            assert await client.status() == status
            assert client.shard("Master") is client.shard("Master")
            with pytest.raises(RemoteError) as missing:
                await client.shard("Missing").status()
            assert missing.value.error.code is ErrorCode.NOT_FOUND

        async with asyncio.timeout(1):
            await closed[1].wait()
        assert not server.connections

    assert not path.exists()


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
        async def wait(self, _context: Any) -> None:
            entered.set()
            await release.wait()
            _context.results.result = success()

        status = wait
        start = wait

    class Bootstrap(schema.Bootstrap.Server):
        async def connect(self, schemaFingerprint: str, _context: Any) -> None:
            assert schemaFingerprint == SCHEMA_FINGERPRINT
            _context.results.result = success(Cluster())

    path = tmp_path / "cluster.sock"
    async with rpc_runtime(), filesystem_rpc_server(path, Bootstrap) as server:
        client = await ClusterClient.connect(path)
        pending = asyncio.create_task(getattr(client, method)())
        await entered.wait()
        for connection, stream in tuple(server.connections):
            connection.close()
            stream.close()
        try:
            with pytest.raises(expected):
                await pending
        finally:
            release.set()
            client.close()


async def test_server_shutdown_bounds_capability_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rpc_transport, "_CLOSE_TIMEOUT", 0.01)
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    class Cluster(schema.Cluster.Server):
        pass

    class Bootstrap(schema.Bootstrap.Server):
        async def connect(self, schemaFingerprint: str, _context: Any) -> None:
            assert schemaFingerprint == SCHEMA_FINGERPRINT
            _context.results.result = success(Cluster())

        async def aclose(self) -> None:
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

    path = tmp_path / "cluster.sock"
    async with rpc_runtime():
        async with filesystem_rpc_server(path, Bootstrap) as server:
            client = await ClusterClient.connect(path)
        client.close()

    assert cleanup_started.is_set()
    assert cleanup_cancelled.is_set()
    assert not server.connections
    assert not server.tasks
    assert not path.exists()
