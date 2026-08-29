import asyncio
import fcntl
import inspect
import os
import socket
import stat
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from logbook import Logger

capnp: Any = import_module("capnp")
logger = Logger(__name__)

INTERNAL_RPC_ADDRESS = "dst-server-registry"
PUBLIC_RPC_SOCKET = Path("/cluster/.dst-server.sock")
_UNIX_PATH_BYTES = 107
_CLOSE_TIMEOUT = 5.0
_CANCEL_REAP_TIMEOUT = 0.1


def _consume_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _validate_unix_path(path: Path) -> bytes:
    encoded = os.fsencode(path)
    if not encoded or len(encoded) > _UNIX_PATH_BYTES or b"\0" in encoded:
        msg = f"invalid Unix socket path: {path}"
        raise ValueError(msg)
    return encoded


@contextmanager
def filesystem_socket(  # ruff: ignore[complex-structure]
    path: Path,
) -> Iterator[socket.socket]:
    path = path.absolute()
    _validate_unix_path(path)
    parent = path.parent
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    sock: socket.socket | None = None
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(directory)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            msg = f"RPC socket directory must be owner-controlled: {parent}"
            raise PermissionError(msg)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            msg = f"another RPC server owns {parent}"
            raise FileExistsError(msg) from error
        try:
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.geteuid():
                msg = f"refusing to replace non-owned Unix socket path: {path}"
                raise FileExistsError(msg)
            os.unlink(path.name, dir_fd=directory)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        path.chmod(0o600, follow_symlinks=False)
        sock.listen(socket.SOMAXCONN)
        sock.setblocking(False)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        identity = (current.st_dev, current.st_ino)
        yield sock
    finally:
        if sock is not None:
            sock.close()
        if identity is not None:
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == identity:
                    os.unlink(path.name, dir_fd=directory)
        os.close(directory)


@contextmanager
def abstract_socket(name: str = INTERNAL_RPC_ADDRESS) -> Iterator[socket.socket]:
    if not name or "\0" in name or len(name.encode()) > _UNIX_PATH_BYTES - 1:
        msg = f"invalid abstract Unix socket name: {name!r}"
        raise ValueError(msg)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(f"\0{name}")
        sock.listen(socket.SOMAXCONN)
        sock.setblocking(False)
        yield sock
    finally:
        sock.close()


@dataclass(slots=True)
class RpcUnixServer:
    server: asyncio.AbstractServer
    connections: set[tuple[Any, Any]]
    tasks: set[asyncio.Task[Any]]
    closing: asyncio.Event

    async def close(self) -> None:
        self.closing.set()
        self.server.close()
        for server, stream in tuple(self.connections):
            server.close()
            stream.close()
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await self.server.wait_closed()
                while self.tasks:
                    await asyncio.wait(tuple(self.tasks))
        except TimeoutError:
            pending = tuple(self.tasks)
        else:
            return

        for task in pending:
            task.cancel()
        if pending:
            try:
                async with asyncio.timeout(_CANCEL_REAP_TIMEOUT):
                    await asyncio.wait(pending)
            except TimeoutError:
                logger.warning("RPC connection cleanup exceeded the shutdown deadline")
            for task in pending:
                if task.done():
                    _consume_task(task)
                else:
                    task.add_done_callback(_consume_task)


async def create_rpc_server(  # ruff: ignore[complex-structure]
    sock: socket.socket,
    bootstrap: Callable[[], object],
) -> RpcUnixServer:
    connections: set[tuple[Any, Any]] = set()
    tasks: set[asyncio.Task[Any]] = set()
    closing = asyncio.Event()

    async def connected(stream: Any) -> None:  # ruff: ignore[complex-structure]
        task = asyncio.current_task()
        if task is not None:
            tasks.add(task)
        root: object | None = None
        connection: Any | None = None
        pair: tuple[Any, Any] | None = None
        disconnected = False

        def disconnect() -> None:
            nonlocal disconnected
            if disconnected:
                return
            disconnected = True
            if connection is not None:
                connection.close()
            stream.close()

        try:
            root = bootstrap()
            connection = capnp.TwoPartyServer(stream, bootstrap=root)
            pair = (connection, stream)
            connections.add(pair)
            if not closing.is_set():
                await connection.on_disconnect()
        finally:
            try:
                if pair is not None:
                    connections.discard(pair)
                disconnect()
                if root is not None and (close := getattr(root, "aclose", None)):
                    try:
                        closed = close()
                        if inspect.isawaitable(closed):
                            await closed
                    except Exception:
                        logger.exception("RPC connection root cleanup failed")
            finally:
                if task is not None:
                    tasks.discard(task)

    server = await capnp.AsyncIoStream.create_unix_server(connected, sock=sock)
    return RpcUnixServer(server, connections, tasks, closing)


@asynccontextmanager
async def filesystem_rpc_server(
    path: Path,
    bootstrap: Callable[[], object],
) -> AsyncIterator[RpcUnixServer]:
    with filesystem_socket(path) as sock:
        server = await create_rpc_server(sock, bootstrap)
        try:
            yield server
        finally:
            await server.close()


@asynccontextmanager
async def abstract_rpc_server(
    bootstrap: Callable[[], object],
    name: str = INTERNAL_RPC_ADDRESS,
) -> AsyncIterator[RpcUnixServer]:
    with abstract_socket(name) as sock:
        server = await create_rpc_server(sock, bootstrap)
        try:
            yield server
        finally:
            await server.close()
