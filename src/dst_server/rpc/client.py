# ruff: file-ignore[async-function-with-timeout, private-member-access]
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import import_module
from os import PathLike, fspath
from typing import Any, Literal, Self, cast
from weakref import WeakValueDictionary

from pydantic import BaseModel
from ulid import ULID

from dst_server.api import ClusterAPI, ShardAPI
from dst_server.cluster.subscriptions import BATCH_SIZE, MAX_BATCH_SIZE
from dst_server.commands import Request, Scope, encode_request, operation
from dst_server.errors import (
    DisconnectedError,
    ErrorCode,
    ErrorInfo,
    IndeterminateError,
    RemoteError,
)
from dst_server.models.cluster import GameEventRecord, LifecycleRecord, LogRecord
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    RPC_TIMEOUT_MARGIN,
    positive_timeout,
)

from .codec import ERROR, decode, decode_model, unwrap_outcome
from .schema import SCHEMA_FINGERPRINT, load_schema

type StreamKind = Literal["logs", "lifecycle", "events"]
capnp: Any = import_module("capnp")


@asynccontextmanager
async def rpc_runtime() -> AsyncIterator[None]:
    async with capnp.kj_loop():
        yield


async def _read_call(operation: Any, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> Any:
    try:
        async with asyncio.timeout(timeout):
            return unwrap_outcome((await operation).result)
    except capnp.KjException as error:
        raise DisconnectedError(str(error)) from error


class Subscription[T]:
    def __init__(self, capability: Any, decode_item: Callable[[bytes], T]) -> None:
        self._capability: Any = capability
        self._decode_item = decode_item
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def next(self, max_items: int = MAX_BATCH_SIZE) -> tuple[T, ...]:
        max_items = BATCH_SIZE.validate_python(max_items, strict=True)
        if self.closed:
            return ()
        try:
            batch = (await self._capability.next(maxItems=max_items)).batch
        except capnp.KjException as error:
            self.closed = True
            self._capability = None
            raise DisconnectedError(str(error)) from error
        match batch.which():
            case "items":
                try:
                    return tuple(self._decode_item(item) for item in batch.items)
                except Exception:
                    await self.close()
                    raise
            case "closed":
                self.closed = True
                self._capability = None
                return ()
            case "error":
                error = decode(ERROR, batch.error)
                if error.code is not ErrorCode.OVERFLOW:
                    await self.close()
                raise RemoteError(error)
            case selected:
                await self.close()
                msg = f"invalid RPC batch member: {selected}"
                raise ValueError(msg)

    async def close(self) -> None:
        if self.closed:
            return
        try:
            await _read_call(self._capability.close(), RPC_TIMEOUT_MARGIN)
        finally:
            self.closed = True
            self._capability = None


class RemoteEndpoint:
    scope: Scope

    async def _get_capability(self) -> Any:
        raise NotImplementedError

    async def invoke[T](self, command: Request[T]) -> T:
        spec = operation(self.scope, command)
        payload = encode_request(command)
        capability = await self._get_capability()
        try:
            async with asyncio.timeout(command.timeout + 2 * RPC_TIMEOUT_MARGIN):
                pending = capability.call(request=payload)
                del payload
                response = await pending
        except TimeoutError as error:
            if spec.mutation:
                raise IndeterminateError from error
            raise RemoteError(
                ErrorInfo(ErrorCode.TIMEOUT, ULID(), "operation timed out")
            ) from error
        except asyncio.CancelledError as error:
            if spec.mutation:
                raise IndeterminateError from error
            raise
        except capnp.KjException as error:
            if spec.mutation:
                raise IndeterminateError from error
            raise DisconnectedError(str(error)) from error
        try:
            return cast("T", decode(spec.response, unwrap_outcome(response.result)))
        except RemoteError:
            raise
        except Exception as error:
            if spec.mutation:
                raise IndeterminateError from error
            raise

    async def _subscribe[T: BaseModel](
        self, kind: StreamKind, model: type[T]
    ) -> Subscription[T]:
        capability = await self._get_capability()
        subscription = await _read_call(capability.subscribe(kind=kind))
        return Subscription(subscription, lambda item: decode_model(model, item))

    async def subscribe_logs(self) -> Subscription[LogRecord]:
        return await self._subscribe("logs", LogRecord)

    async def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        return await self._subscribe("lifecycle", LifecycleRecord)

    async def subscribe_events(self) -> Subscription[GameEventRecord]:
        return await self._subscribe("events", GameEventRecord)


class ClusterClient(RemoteEndpoint, ClusterAPI):
    scope: Scope = "cluster"

    def __init__(self, stream: Any, client: Any, capability: Any) -> None:
        self._stream: Any = stream
        self._client: Any = client
        self._capability: Any = capability
        self._shards: WeakValueDictionary[str, ShardClient] = WeakValueDictionary()
        self._closed = False

    @classmethod
    async def connect(
        cls, path: str | PathLike[str], *, timeout: float = DEFAULT_CONNECT_TIMEOUT
    ) -> Self:
        timeout = positive_timeout(timeout)
        stream = client = None
        try:
            async with asyncio.timeout(timeout):
                stream = await capnp.AsyncIoStream.create_unix_connection(fspath(path))
                client = capnp.TwoPartyClient(stream)
                response = (
                    await client
                    .bootstrap()
                    .cast_as(load_schema().Bootstrap)
                    .connect(schemaFingerprint=SCHEMA_FINGERPRINT)
                )
                return cls(stream, client, unwrap_outcome(response.result))
        except BaseException:
            if client is not None:
                client.close()
            if stream is not None:
                stream.close()
            raise

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()
            self._stream.close()
            self._client = self._stream = self._capability = None
            for shard in self._shards.values():
                shard._capability = None
            self._shards.clear()

    async def _get_capability(self) -> Any:
        if self._closed:
            msg = "ClusterClient is closed"
            raise DisconnectedError(msg)
        return self._capability

    def shard(self, shard_name: str) -> ShardClient:
        shard = self._shards.get(shard_name)
        if shard is None:
            shard = self._shards[shard_name] = ShardClient(self, shard_name)
        return shard


class ShardClient(RemoteEndpoint, ShardAPI):
    scope: Scope = "shard"

    def __init__(self, cluster: ClusterClient, shard_name: str) -> None:
        self._cluster = cluster
        self.name = shard_name
        self._capability: Any = None

    async def _get_capability(self) -> Any:
        cluster = await self._cluster._get_capability()
        if self._capability is None:
            self._capability = await _read_call(cluster.shard(shardName=self.name))
        return self._capability
