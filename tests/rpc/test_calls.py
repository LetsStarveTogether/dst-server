# ruff: file-ignore[blocking-path-method-in-async-function]
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from weakref import ref

import pytest
from pydantic import ValidationError
from ulid import ULID

from dst_server import commands as c
from dst_server.errors import (
    ErrorCode,
    IndeterminateCommandError,
    IndeterminateError,
    RemoteError,
)
from dst_server.events.server import SavedEvent
from dst_server.models.cluster import (
    ModUpdateStatus,
)
from dst_server.rpc import servants as servant_module
from dst_server.rpc.client import ClusterClient, rpc_runtime
from dst_server.rpc.codec import unwrap_outcome
from dst_server.rpc.servants import (
    BootstrapServant,
)
from dst_server.rpc.transport import filesystem_rpc_server
from tests.cluster.helpers import controller as make_controller
from tests.helpers import wait_for_event
from tests.rpc.helpers import FakeController, connected

capnp: Any = pytest.importorskip("capnp")


async def test_typed_commands_cross_real_capabilities(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        assert await client.status() == controller.value
        shard = client.shard("Master")
        assert shard is client.shard("Master")
        assert await shard.status() == controller.master.value
        assert await shard.execute("return 1", timeout=4) == "return 1"
        assert await shard.save(timeout=5) == SavedEvent(
            path="session/SESSION/0000000031", snapshot=31
        )
        assert (
            await client.list_snapshots(limit=17, before=0) == controller.master.catalog
        )
        assert (
            await client.rollback_to_day(21, timeout=6)
            == controller.master.catalog.snapshots[0]
        )
        assert controller.master.requests[-2:] == [
            c.Execute(source="return 1", timeout=4),
            c.Save(timeout=5),
        ]
        assert controller.requests[-2:] == [
            c.Snapshots(limit=17, before=0),
            c.RollbackToDay(day=21, timeout=6),
        ]


async def test_mod_update_sdk_preserves_restart_across_rpc(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        await client.update_mods(restart=True)
        await client.update_mods()

    assert controller.requests == [
        c.UpdateMods(restart=True),
        c.UpdateMods(restart=False),
    ]


async def test_mod_maintenance_status_crosses_cluster_and_shard_rpc(
    tmp_path: Path,
) -> None:
    controller = FakeController()
    controller.master.value = controller.master.value.replace(
        game_attempt=ULID(), outdated_mods=("Insight", "测试 MOD")
    )
    controller.value = controller.value.replace(
        shards=(controller.master.value,),
        mod_update=ModUpdateStatus(
            enabled=True,
            pending=True,
            updating=False,
            retry_in_seconds=123.5,
            error="MOD update failed",
        ),
    )
    async with connected(tmp_path, controller) as client:
        status = await client.status()
        shard = await client.shard("Master").status()

    assert status == controller.value
    assert (
        status.mod_update.model_fields_set
        == controller.value.mod_update.model_fields_set
    )
    assert shard == status.shards[0] == controller.master.value
    assert shard.outdated_mods == ("Insight", "测试 MOD")
    assert shard.game_attempt == controller.master.value.game_attempt


async def test_discovery_and_raw_calls_use_public_registry(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        methods = {method.name: method for method in await client.describe()}
        assert methods == {
            method.name: method for method in c.describe_operations("cluster")
        }
        assert "activate" not in methods
        assert "rollback_to_snapshot" not in methods
        assert "execute" not in methods
        assert methods["status"].mutation is False
        assert methods["status"].arguments_schema["properties"] == {}
        assert methods["announce"].arguments_schema["required"] == ["message"]
        assert methods["announce"].arguments_schema["additionalProperties"] is False
        assert all(method.description for method in methods.values())
        assert await client.call("status") == controller.value.model_dump(
            mode="json", exclude_unset=True
        )
        assert await client.call("announce", {"message": "hello"}, timeout=1.5) is None
        assert controller.requests[-1] == c.Announce(message="hello", timeout=1.5)
        shard = client.shard("Master")
        shard_methods = {method.name: method for method in await shard.describe()}
        assert "evaluate" in shard_methods
        assert "activate" not in shard_methods
        assert await shard.call("execute", {"source": "return 1"}) == "return 1"
        with pytest.raises(ValueError, match="not available"):
            await client.call("activate")
        with pytest.raises(ValueError, match="reserved fields"):
            await client.call("status", {"timeout": 1})
        previous = len(controller.requests)
        with pytest.raises(RemoteError) as invalid:
            await client.call("announce", {"message": 1})
        assert invalid.value.error.code is ErrorCode.INVALID_ARGUMENT
        assert len(controller.requests) == previous


@pytest.mark.parametrize(
    "payload",
    [
        b'{"method":"start","method":"stop"}',
        b'{"method":"start","arguments":{"timeout":1}}',
        b'{"method":"start","timeout":0}',
        b'{"method":"start","timeout":NaN}',
        b'{"method":"start","timeout":true}',
        b'{"method":"wait_saved","arguments":{}}',
        b'{"method":"__getattribute__"}',
        b'{"method":"status","unknown":true}',
    ],
)
async def test_untrusted_requests_fail_before_dispatch(
    tmp_path: Path, payload: bytes
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        response = await client._capability.call(request=payload)
        with pytest.raises(RemoteError) as failure:
            unwrap_outcome(response.result)
        assert failure.value.error.code is ErrorCode.INVALID_ARGUMENT
        assert controller.requests == []


@pytest.mark.parametrize(
    "command",
    [
        c.Save().model_copy(update={"timeout": 0}),
        c.Snapshots().model_copy(update={"before": True}),
    ],
)
async def test_invalid_copied_command_fails_before_opening_capability(
    command: c.Request[Any],
) -> None:
    client = ClusterClient(None, None, None)
    with pytest.raises(ValidationError):
        await client.shard("Master").invoke(command)


async def test_unknown_shard_is_reported(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        with pytest.raises(RemoteError) as unknown:
            await client.shard("Unknown").status()
        assert unknown.value.error.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [(False, ErrorCode.TIMEOUT), (True, ErrorCode.INDETERMINATE)],
)
async def test_server_deadlines_distinguish_queries_and_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: bool, expected: ErrorCode
) -> None:
    monkeypatch.setattr(servant_module, "RPC_TIMEOUT_MARGIN", 0.01)
    controller = FakeController()

    async def slow(_: c.Request[Any]) -> None:
        await asyncio.sleep(1)

    controller.hook = slow
    async with connected(tmp_path, controller) as client:
        command = (
            c.Start(timeout=0.02) if mutation else c.ClusterStatusQuery(timeout=0.02)
        )
        with pytest.raises(RemoteError) as failure:
            await client.invoke(command)
        assert failure.value.error.code is expected


@pytest.mark.parametrize("command_type", [c.ClusterSave, c.Reset])
async def test_mutating_workflow_rejects_busy_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_type: type[c.ClusterSave | c.Reset],
) -> None:
    controller, master, caves, _, _ = await make_controller(tmp_path, monkeypatch)
    try:
        tmp_path.chmod(0o700)
        path = tmp_path / "cluster.sock"
        async with (
            asyncio.timeout(5),
            rpc_runtime(),
            filesystem_rpc_server(path, lambda: BootstrapServant(controller)),
            await ClusterClient.connect(path) as client,
            controller._serialized(),
        ):
            with pytest.raises(RemoteError) as failure:
                await client.invoke(command_type(timeout=0.1))
            assert failure.value.error.code is ErrorCode.INVALID_STATE
            assert (await client.status()).busy
            assert not any(
                isinstance(request, c.Save | c.Reset)
                for request in master.requests + caves.requests
            )
    finally:
        await controller.aclose()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("secret"), ErrorCode.INVALID_ARGUMENT),
        (RuntimeError("secret"), ErrorCode.INVALID_STATE),
        (KeyError("secret"), ErrorCode.NOT_FOUND),
        (OSError("secret"), ErrorCode.INTERNAL),
        (IndeterminateCommandError("secret"), ErrorCode.INDETERMINATE),
    ],
)
async def test_remote_errors_are_typed_and_do_not_expose_exception_messages(
    tmp_path: Path, error: Exception, expected: ErrorCode
) -> None:
    controller = FakeController()
    controller.hook = AsyncMock(side_effect=error)
    async with connected(tmp_path, controller) as client:
        with pytest.raises(RemoteError) as failure:
            await client.status()
        assert failure.value.error.code is expected
        assert "secret" not in str(failure.value)


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("mutation", [False, True])
async def test_invalid_result_preserves_uncertainty(
    tmp_path: Path, mutation: bool, raw: bool
) -> None:
    controller = FakeController()
    controller.hook = AsyncMock(return_value=object())
    async with connected(tmp_path, controller) as client:
        command = c.Start() if mutation else c.ClusterStatusQuery()
        pending = client.call(command.method) if raw else client.invoke(command)
        with pytest.raises(RemoteError) as failure:
            await pending
        assert failure.value.error.code is (
            ErrorCode.INDETERMINATE if mutation else ErrorCode.INTERNAL
        )


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("disconnect", [False, True])
async def test_accepted_mutation_survives_caller_loss(
    tmp_path: Path, disconnect: bool, raw: bool
) -> None:
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def mutate(_: c.Request[Any]) -> None:
        entered.set()
        await release.wait()
        completed.set()

    controller = FakeController()
    controller.hook = mutate
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.call("start") if raw else client.start())
        try:
            await wait_for_event(entered, pending)
            if disconnect:
                client.close()
            else:
                pending.cancel("caller cancelled")
            done, _ = await asyncio.wait((pending,), timeout=1)
            assert pending in done
            expected = IndeterminateError if disconnect else asyncio.CancelledError
            with pytest.raises(expected) as failure:
                pending.result()
            if not disconnect:
                assert pending.cancelled()
                assert failure.value.args == ("caller cancelled",)
                assert failure.value.__notes__ == [
                    (
                        "RPC mutation result could not be confirmed; "
                        "the operation may still be running."
                    )
                ]
            release.set()
            await wait_for_event(completed)
        finally:
            release.set()
            pending.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("raw", [False, True])
async def test_query_cancellation_reaches_handler(tmp_path: Path, raw: bool) -> None:
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def query(_: c.Request[Any]) -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            cancelled.set()

    controller = FakeController()
    controller.hook = query
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.call("status") if raw else client.status())
        try:
            await wait_for_event(entered, pending)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=1)
            await wait_for_event(cancelled)
        finally:
            release.set()
            pending.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(pending, return_exceptions=True)


async def test_shard_handles_are_cached_only_while_in_use(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        shard = client.shard("Master")
        assert shard is client.shard("Master")
        references = [ref(client.shard(f"Missing-{index}")) for index in range(100)]
        assert all(reference() is None for reference in references)
        assert len(client._shards) == 1


def test_client_close_releases_connection_and_shard_capabilities() -> None:
    class Resource:
        def close(self) -> None:
            pass

    resources = [Resource() for _ in range(4)]
    references = tuple(ref(resource) for resource in resources)
    client = ClusterClient(*resources[:3])
    shard = client.shard("Master")
    shard._capability = resources[3]
    del resources

    client.close()

    assert all(reference() is None for reference in references)


async def test_client_releases_encoded_request_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dst_server.rpc import client as client_module

    class Payload(bytearray):
        pass

    sent = asyncio.Event()
    response = asyncio.get_running_loop().create_future()
    references = []

    def encode(_: c.Request[Any]) -> Payload:
        payload = Payload(1024 * 1024)
        references.append(ref(payload))
        return payload

    def send(*, request: Payload) -> asyncio.Future[Any]:
        assert len(request) == 1024 * 1024
        sent.set()
        return response

    monkeypatch.setattr(client_module, "encode_request", encode)
    client = ClusterClient(None, None, SimpleNamespace(call=send))
    pending = asyncio.create_task(client.status())
    try:
        await wait_for_event(sent, pending)
        assert references
        assert references[0]() is None
    finally:
        pending.cancel()
        async with asyncio.timeout(5):
            await asyncio.gather(pending, return_exceptions=True)


async def test_call_releases_native_request_before_running_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    respond = servant_module.EndpointMethods._respond

    async def inspect_context(
        self: Any, context: Any, method: str, *args: Any, **kwargs: Any
    ) -> None:
        if method == "execute":
            with pytest.raises(capnp.KjException, match="releaseParams"):
                _ = context.params
            entered.set()
        await respond(self, context, method, *args, **kwargs)

    async def blocked(command: c.Request[Any]) -> str:
        await release.wait()
        assert isinstance(command, c.Execute)
        return command.source

    monkeypatch.setattr(servant_module.EndpointMethods, "_respond", inspect_context)
    controller = FakeController()
    controller.master.hook = blocked
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.shard("Master").execute("x" * 1024 * 1024))
        try:
            await wait_for_event(entered, pending)
            release.set()
            await pending
        finally:
            release.set()
            pending.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(pending, return_exceptions=True)


async def test_connection_timeout_covers_socket_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked(_: str) -> None:
        await asyncio.Event().wait()

    from dst_server.rpc import client as module

    monkeypatch.setattr(
        module,
        "capnp",
        SimpleNamespace(AsyncIoStream=SimpleNamespace(create_unix_connection=blocked)),
    )
    async with asyncio.timeout(5):
        with pytest.raises(TimeoutError):
            await ClusterClient.connect("unused", timeout=0.01)
