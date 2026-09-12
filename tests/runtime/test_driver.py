import asyncio
from typing import cast
from unittest.mock import Mock

import orjson
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from dst_server import commands as c
from dst_server.events import GAME_EVENT_ADAPTER, GameEvent
from dst_server.events import server as server_events
from dst_server.models.driver import DriverHealth
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import Console
from dst_server.runtime.driver import Driver
from tests.runtime.helpers import (
    StubServer,
    StubWriter,
    feed_response,
    next_request,
    structured_result,
)


def diagnostic(count: int) -> dict[str, str | int]:
    return {"stage": "callback", "message": "callback_failed", "count": count}


def health(
    events_emitted: int, *, generation: int = 0, errors: int = 0
) -> DriverHealth:
    return DriverHealth.model_validate({
        "protocol": 2,
        "generation": generation,
        "telemetry_status": "degraded" if errors else "active",
        "last_error": diagnostic(errors) if errors else None,
        "events_emitted": events_emitted,
        "errors": errors,
    })


def event(
    generation: int = 0, seq: int = 1, *, error_count: int | None = None
) -> GameEvent:
    return GAME_EVENT_ADAPTER.validate_python({
        "v": 2,
        "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": generation,
        "session_id": "TEST",
        "seq": seq,
        "event": (
            "dst.telemetry.error"
            if error_count is not None
            else "dst.world.state_changed"
        ),
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": (
            diagnostic(error_count)
            if error_count is not None
            else {"name": "cycles", "value": 2}
        ),
    })


async def test_waiters_follow_current_generation_without_install_commands() -> None:
    server = await StubServer([
        structured_result({"snapshot": "session/TEST/0000000001"})
    ]).initialize()
    server.driver.starting(1)
    request = asyncio.create_task(server.game.request_save())
    cancelled = asyncio.create_task(server.driver.wait_ready())
    try:
        await asyncio.sleep(0)
        assert not request.done()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        server.driver.starting(2)
        server.driver.ready(health(9, generation=1))
        assert not server.driver.is_ready(2)
        server.driver.ready(health(1, generation=2))
        await asyncio.wait_for(request, 1)
        assert server.driver_health == health(1, generation=2)
        assert server.commands == [("save", {})]
    finally:
        request.cancel()
        cancelled.cancel()
        await asyncio.gather(request, cancelled, return_exceptions=True)
        await server.finish()


async def test_failure_wakes_waiters_and_next_world_recovers() -> None:
    driver = Driver()
    driver.starting(1)
    waiting = asyncio.create_task(driver.wait_ready())
    await asyncio.sleep(0)
    driver.failed("installation_failed")
    with pytest.raises(RuntimeError, match="installation_failed"):
        await asyncio.wait_for(waiting, 1)
    assert not driver.is_ready(1)
    driver.starting(2)
    assert driver.error is None
    driver.ready(health(0, generation=2))
    assert await driver.wait_ready() == 2


async def test_observations_do_not_replace_native_ready_handshake() -> None:
    driver = Driver()
    driver.observe_event(event(seq=7, error_count=2))
    driver.observe_health(0, health(99, errors=9))
    waiting = asyncio.create_task(driver.wait_ready())
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        assert not driver.is_ready(0)
        with pytest.raises(RuntimeError, match="not been installed"):
            _ = driver.health
        driver.ready(health(0))
        assert await asyncio.wait_for(waiting, 1) == 0
        assert driver.health == health(7, errors=2)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_generation_change_discards_old_health_until_native_ready() -> None:
    driver = Driver()
    driver.ready(health(0, generation=1))
    driver.observe_event(event(1, seq=8, error_count=2))
    driver.starting(2)
    driver.observe_health(1, health(20, generation=1, errors=5))
    driver.observe_event(event(1, seq=21, error_count=6))
    driver.observe_health(2, health(99, generation=2, errors=9))
    driver.observe_event(event(2, seq=1, error_count=1))
    assert not driver.is_ready(2)
    driver.ready(health(0, generation=2))
    assert await driver.wait_ready() == 2
    assert driver.health == health(1, generation=2, errors=1)


@pytest.mark.parametrize("generation", [0, 1])
def test_duplicate_or_stale_starting_keeps_current_driver(generation: int) -> None:
    driver = Driver()
    driver.ready(health(3, generation=1))
    driver.starting(generation)
    driver.ready(health(0, generation=generation))
    assert driver.generation == 1
    assert driver.is_ready(1)
    assert driver.health == health(3, generation=1)


async def test_close_wakes_waiters_and_rejects_late_native_ready() -> None:
    driver = Driver()
    waiting = asyncio.create_task(driver.wait_ready())
    await asyncio.sleep(0)
    driver.close()
    driver.close()
    driver.starting(1)
    driver.ready(health(1, generation=1))
    driver.failed("installation_failed")
    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiting, 1)
    assert not driver.is_ready(1)
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health


async def test_timed_out_console_recovers_after_native_world_change() -> None:
    server = Server(ServerConfig(shard="test"))
    writer = StubWriter()
    reader = asyncio.StreamReader()
    server.child = cast("asyncio.subprocess.Process", Mock(returncode=None))
    server.console = Console(
        cast("asyncio.StreamWriter", writer),
        reader,
        server.game_events.nonce,
        server.recorder,
    )
    server.lifecycle.handle(server_events.ReadyEvent(detail=""))
    server.driver.ready(health(0))
    try:
        with pytest.raises(TimeoutError):
            await server.execute("uncertain_write()", completion_timeout=0.01)
        assert not server.console.closed
        timed_out = await next_request(writer)
        before = list(writer.commands)
        for record in (
            {"generation": 1},
            {"health": health(1, generation=1).model_dump(mode="json")},
        ):
            line = b"DST_DRIVER|" + orjson.dumps({
                "nonce": server.game_events.nonce,
                **record,
            })
            assert await server.game_events.accept(line, 1)
        assert await server.driver.wait_ready() == 1
        assert server.driver_health == health(1, generation=1)
        assert writer.commands == before
        assert not server.console.closed
        saving = asyncio.create_task(server.game.request_save())
        try:
            request = await next_request(writer)
            assert request["generation"] == 1
            assert request["id"] != timed_out["id"]
            feed_response(reader, request, {"snapshot": "session/TEST/0000000001"})
            await asyncio.wait_for(saving, 1)
            assert len(writer.commands) == len(before) + 1
        finally:
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
    finally:
        await server.finish()


async def test_get_health_updates_committed_health() -> None:
    server = await StubServer([
        structured_result(health(9).model_dump(mode="json")),
    ]).initialize()
    observed = await server.game.invoke(c.Health())
    assert observed.events_emitted == 9
    assert server.driver_health.events_emitted == 9


def test_accepted_events_update_immutable_live_health() -> None:
    driver = Driver()
    driver.ready(health(0))
    installed = driver.health
    driver.observe_event(event(seq=7))
    observed = driver.health
    driver.observe_event(event(seq=8, error_count=2))
    degraded = driver.health
    driver.observe_event(event(seq=9))

    assert installed.events_emitted == 0
    assert observed.events_emitted == 7
    assert observed.telemetry_status == "active"
    assert degraded.events_emitted == 8
    assert degraded.errors == 2
    assert degraded.telemetry_status == "degraded"
    assert degraded.last_error is not None
    assert degraded.last_error.model_dump() == diagnostic(2)
    assert driver.health.events_emitted == 9
    assert driver.health.errors == 2
    assert driver.health.last_error == degraded.last_error
    assert driver.health.telemetry_status == "degraded"


def test_event_and_health_snapshots_never_regress_within_a_generation() -> None:
    driver = Driver()
    driver.ready(health(0))
    driver.observe_event(event(seq=10, error_count=3))
    latest = driver.health

    driver.observe_event(event(seq=2, error_count=1))
    driver.observe_health(0, health(5, errors=2))
    driver.observe_health(0, health(7))

    assert driver.health == latest
    driver.observe_health(0, health(15, errors=4))
    assert driver.health == health(15, errors=4)
    driver.observe_event(event(seq=16, error_count=5))
    assert driver.health == health(16, errors=5)


@pytest.mark.parametrize("generation", [-1, 1])
def test_event_and_health_from_other_generations_are_ignored(
    generation: int,
) -> None:
    driver = Driver()
    driver.ready(health(1, generation=1))
    current = driver.health
    foreign_generation = driver.generation + generation

    driver.observe_event(event(foreign_generation, seq=99, error_count=4))
    driver.observe_health(
        foreign_generation, health(99, generation=foreign_generation, errors=4)
    )

    assert driver.health == current
    assert driver.installed_generation == 1


@pytest.mark.parametrize("status", ["disabled", "degraded", "failed"])
async def test_telemetry_status_does_not_disable_working_core_driver(
    status: str,
) -> None:
    snapshot = health(0, errors=0 if status == "disabled" else 1).replace(
        telemetry_status=status
    )
    driver = Driver()
    driver.ready(snapshot)

    assert driver.is_ready(0)
    assert await driver.wait_ready() == 0
    assert driver.health == snapshot


@pytest.mark.parametrize("status", ["disabled", "failed"])
@pytest.mark.parametrize("diagnostic_first", [False, True])
def test_diagnostics_preserve_inactive_telemetry_status(
    status: str,
    diagnostic_first: bool,
) -> None:
    snapshot = health(0, errors=1 if status == "failed" else 0).replace(
        telemetry_status=status
    )
    driver = Driver()
    if diagnostic_first:
        driver.observe_event(event(seq=1, error_count=1))
    driver.ready(snapshot)
    if not diagnostic_first:
        driver.observe_event(event(seq=1, error_count=1))
    assert driver.health.telemetry_status == status

    driver.observe_event(event(seq=2, error_count=2))
    driver.observe_health(0, snapshot)
    assert driver.is_ready(0)
    assert driver.health == snapshot.replace(
        events_emitted=2,
        errors=2,
        last_error=diagnostic(2),
    )


async def test_close_invalidates_readiness_and_rejects_late_observations() -> None:
    driver = Driver()
    driver.ready(health(1))
    driver.close()
    driver.observe_event(event(seq=2, error_count=1))
    driver.observe_health(0, health(3, errors=2))

    assert not driver.is_ready(0)
    with pytest.raises(RuntimeError, match="closed"):
        await driver.wait_ready()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health


def test_new_install_drops_pending_old_diagnostics() -> None:
    driver = Driver()
    driver.observe_event(event(0, seq=9, error_count=3))

    driver.ready(health(0, generation=1))

    assert driver.health == health(0, generation=1)
    assert driver.is_ready(1)


@pytest.mark.parametrize(
    ("observed_generation", "response_generation"), [(0, 1), (1, 0)]
)
def test_observe_health_rejects_mismatched_response_generation(
    observed_generation: int, response_generation: int
) -> None:
    driver = Driver()
    driver.ready(health(1))
    initial = driver.health

    driver.observe_health(
        observed_generation, health(99, generation=response_generation, errors=4)
    )

    assert driver.health == initial


@given(
    st.lists(
        st.tuples(
            st.sampled_from((
                "starting",
                "ready",
                "event",
                "health",
                "failed",
                "close",
            )),
            st.integers(0, 4),
            st.integers(1, 20),
            st.integers(0, 5),
        ),
        max_size=40,
    )
)
@example([
    ("ready", 0, 10, 2),
    ("event", 0, 1, 1),
    ("starting", 2, 1, 0),
    ("ready", 1, 20, 5),
    ("ready", 2, 1, 0),
    ("close", 2, 1, 0),
    ("ready", 3, 20, 5),
])
def test_driver_action_sequences_preserve_generation_and_health(  # ruff: ignore[complex-structure]
    actions: list[tuple[str, int, int, int]],
) -> None:
    driver = Driver()

    def snapshot() -> tuple[int, bool, str | None, DriverHealth | None]:
        return (
            driver.generation,
            driver.closed,
            driver.error,
            driver.health if driver.is_ready(driver.generation) else None,
        )

    for action, generation, sequence, errors in actions:
        before = snapshot()
        match action:
            case "starting":
                driver.starting(generation)
            case "ready":
                driver.ready(health(sequence, generation=generation, errors=errors))
            case "event":
                driver.observe_event(
                    event(generation, sequence, error_count=errors or None)
                )
            case "health":
                driver.observe_health(
                    generation, health(sequence, generation=generation, errors=errors)
                )
            case "failed":
                driver.failed("installation_failed")
            case "close":
                driver.close()
        after = snapshot()
        assert after[0] >= before[0]
        if (
            before[1]
            or (action in {"starting", "ready"} and generation < before[0])
            or (action in {"event", "health"} and generation != before[0])
        ):
            assert after == before
        if after[0] == before[0] and before[3] is not None and after[3] is not None:
            assert after[3].events_emitted >= before[3].events_emitted
            assert after[3].errors >= before[3].errors
        if driver.closed or action == "failed":
            assert not driver.is_ready(driver.generation)
