import asyncio
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, call

import orjson
import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from ulid import ULID

from dst_server.models.driver import DriverHealth, DriverReady, DriverStarting
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import stream
from dst_server.telemetry.otel import Pipeline
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import PREFIX, EventStream
from tests.helpers import run_lua_process

PLAYER = {"prefab": "wilson", "guid": 42, "userid": "KU_TEST", "position": None}
NATIVE_PREFIXES = ["", "[00:00:01]: ", "[125:59:59]: "]


def event_line(nonce: str, sequence: int, **changes: object) -> str:
    return (
        PREFIX
        + orjson.dumps(
            {
                "v": 2,
                "nonce": nonce,
                "generation": 1,
                "session_id": "SESSION",
                "seq": sequence,
                "event": "dst.world.state_changed",
                "tick": 10,
                "monotonic_ms": 20,
                "cycle": 2,
                "data": {"name": "cycles", "value": 2},
            }
            | changes,
        ).decode()
    )


@pytest.mark.parametrize("season", ["autumn", "wet", "monsoon", "custom_mod_season"])
async def test_mod_season_event_is_preserved(season: str) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    await events.accept(
        event_line(events.nonce, 1, data={"name": "season", "value": season}),
        123,
    )
    events.close()

    observed = await events.read()
    assert events.invalid == 0
    assert observed is not None
    assert observed.record.data.model_dump() == {"name": "season", "value": season}
    assert observed.observed_timestamp_ns == 123


async def test_mod_condition_survives_full_consumer_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    events = EventStream(Recorder("cluster", "shard"))
    await events.accept(event_line(events.nonce, 1), 1)
    await events.accept(
        event_line(events.nonce, 2, event="dst.mod.outdated", data={"name": "Insight"}),
        2,
    )
    await events.accept(event_line(events.nonce, 3), 3)
    events.close()
    assert events.outdated_mods == {"Insight"}
    assert events.dropped == 2


async def test_only_valid_current_attempt_mod_reports_change_condition() -> None:
    events = EventStream(Recorder("cluster", "shard"))
    for nonce, data in (
        (str(ULID()), {"name": "Old process"}),
        (events.nonce, {"name": ""}),
        (events.nonce, {"name": "x" * 4097}),
        (events.nonce, {"name": 1}),
        (events.nonce, {"name": "Spoofed version", "version": "1"}),
    ):
        await events.accept(
            event_line(nonce, 1, event="dst.mod.outdated", data=data), 1
        )
    assert events.outdated_mods == set()
    for generation in (1, 2, 1):
        await events.accept(
            event_line(
                events.nonce,
                1,
                generation=generation,
                event="dst.mod.outdated",
                data={"name": "Insight"},
            ),
            1,
        )
    assert events.outdated_mods == {"Insight"}
    assert events.invalid == 5
    events.close()


@given(st.lists(st.binary(max_size=128), min_size=1, max_size=40))
@example([b"\xff", b"{", b"{}"])
async def test_damaged_event_frames_cannot_poison_later_valid_events(
    damaged_payloads: list[bytes],
) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    try:
        for index, payload in enumerate(damaged_payloads):
            assert await events.accept(PREFIX.encode() + payload, index)
        assert events.invalid == len(damaged_payloads)
        assert await events.accept(event_line(events.nonce, 42), 123)
    finally:
        events.close()
    observed = await events.read()
    assert observed is not None
    assert (observed.record.seq, observed.observed_timestamp_ns) == (42, 123)
    assert await events.read() is None


@given(st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=64))
async def test_unicode_event_data_survives_json_and_stream_validation(
    session_id: str,
) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    await events.accept(
        event_line(events.nonce, 1, session_id=session_id or "SESSION"), 1
    )
    events.close()
    observed = await events.read()
    assert observed is not None
    assert observed.record.session_id == (session_id or "SESSION")


@pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize(
    "session_id",
    [
        pytest.param("OLD_SESSION", id="ascii"),
        pytest.param("👨‍👩‍👧‍👦👍🏽✈️✈︎e\u0301🇨🇳1️⃣", id="graphemes"),
        pytest.param(
            "\u200b\u202eRTL\u202c\u2066LTR\u2069\ue000", id="invisible-bidi-pua"
        ),
        pytest.param("\u0085\u2028\u2029", id="unicode-line-separators"),
        pytest.param("玩家👩🏽‍💻\n\tDST_OTEL|", id="escaped-controls-marker"),
    ],
)
async def test_event_prefix_and_observation_are_preserved(
    prefix: str, as_bytes: bool, session_id: str
) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    line = prefix + event_line(events.nonce, 5, generation=7, session_id=session_id)
    timestamp = 1_788_761_234_123_456_789

    assert await events.accept(line.encode() if as_bytes else line, timestamp)

    observed = await events.read()
    assert observed is not None
    assert observed.observed_timestamp_ns == timestamp
    assert observed.record.nonce == events.nonce
    assert observed.record.generation == 7
    assert observed.record.session_id == session_id
    assert observed.record.seq == 5


@pytest.mark.parametrize(
    "prefix",
    [
        "ordinary log ",
        "[00:00:01]: player says ",
        "[00:00:01]: [Chat] ",
        "[0:00:01]: ",
        "[00:60:01]: ",
        "[00:00:60]: ",
        "[00:00:01]:",
        " ",
        "\n",
        "FAKE_",
    ],
)
async def test_embedded_markers_remain_ordinary_logs(prefix: str) -> None:
    events = EventStream(Recorder("cluster", "shard"))

    assert not await events.accept(prefix + event_line(events.nonce, 1), 1)
    assert events.queue.empty()
    assert events.invalid == 0
    assert events.dropped == 0


@pytest.mark.parametrize("line", ["ordinary log", b"ordinary \xff log", "\ud800"])
async def test_non_event_lines_do_not_enter_validation(line: str | bytes) -> None:
    events = EventStream(Recorder("cluster", "shard"))

    assert not await events.accept(line, 1)
    assert events.invalid == 0


@pytest.mark.parametrize(
    ("kind", "size"),
    [
        ("valid", None),
        ("wrong_nonce", None),
        ("bad_nonce", None),
        ("schema", None),
        ("surrogate", None),
        ("valid", 65536),
        ("valid", 65537),
    ],
)
async def test_mixed_validation_preserves_later_events(
    kind: str, size: int | None
) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    nonce = (
        str(ULID())
        if kind == "wrong_nonce"
        else "not-a-ulid"
        if kind == "bad_nonce"
        else events.nonce
    )
    candidate = event_line(
        nonce,
        2,
        v=1 if kind == "schema" else 2,
        session_id="👩🏽‍💻e\u0301\u2028",
    )
    if kind == "surrogate":
        candidate = candidate.replace("👩🏽‍💻e\u0301\u2028", "\ud800")
    if size is not None:
        candidate += " " * (size - len(candidate.encode(errors="surrogatepass")))
    lines = [
        event_line(events.nonce, 1),
        "ordinary log\t" + candidate,
        "[Say] (KU_TEST) 玩家👩🏽‍💻: " + candidate,
        "#LUA ERROR: " + candidate,
        candidate,
        "\t[C]: in function 'error': " + candidate,
        event_line(events.nonce, 3),
    ]
    for timestamp, line in enumerate(lines, start=1):
        assert await events.accept("[125:59:59]: " + line, timestamp) == (
            timestamp in {1, 5, 7}
        )

    events.close()
    observed = []
    while event := await events.read():
        observed.append((event.record.seq, event.observed_timestamp_ns))
    accepted = kind == "valid" and size != 65537
    assert observed == ([(1, 1), (2, 5), (3, 7)] if accepted else [(1, 1), (3, 7)])
    assert events.invalid == (0 if accepted else 1)
    assert events.dropped == 0


@pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(b"\xc0\xaf", id="overlong"),
        pytest.param(b"\x80", id="lone-continuation"),
        pytest.param(b"\xf0\x9f\x98", id="truncated"),
        pytest.param(b"\xed\xa0\x80", id="surrogate"),
        pytest.param(b"\xf4\x90\x80\x80", id="out-of-range"),
        pytest.param(b"\xff\x00", id="binary"),
    ],
)
async def test_invalid_utf8_mixed_with_logs_does_not_poison_events(
    prefix: str, invalid: bytes
) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    candidate = event_line(events.nonce, 1).encode().replace(b"SESSION", invalid)
    for label in (b"mod message: ", b"[Say] (KU_TEST) name: ", b"#LUA ERROR: "):
        assert not await events.accept(prefix.encode() + label + candidate, 1)
    assert events.invalid == 0
    assert await events.accept(prefix.encode() + candidate, 2)
    assert await events.accept(prefix + event_line(events.nonce, 2), 3)

    observed = await events.read()
    assert observed is not None
    assert (observed.record.seq, observed.observed_timestamp_ns) == (2, 3)
    assert events.queue.empty()
    assert events.invalid == 1
    assert events.dropped == 0


async def test_full_stream_drops_oldest_without_waiting_for_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    recorded = Mock()
    observer = Mock()
    journal = Mock()
    monkeypatch.setattr(recorder, "record_event", recorded)
    monkeypatch.setattr(recorder, "observe_game", journal)
    events = EventStream(recorder, observe_event=observer)
    async with asyncio.timeout(1):
        assert await events.accept(event_line(events.nonce, 1), 11)
        assert await events.accept(event_line(events.nonce, 2), 22)
    assert observer.call_count == 2
    assert journal.call_count == 2
    assert [call.args[0].record.seq for call in journal.call_args_list] == [1, 2]
    assert events.queue.qsize() == 1
    assert events.dropped == 1
    second = await events.read()
    assert second is not None
    assert (second.record.seq, second.observed_timestamp_ns) == (2, 22)
    assert recorded.call_args_list == [
        call("accepted", event_name="dst.world.state_changed"),
        call("accepted", event_name="dst.world.state_changed"),
        call("dropped", event_name="dst.world.state_changed", reason="queue_full"),
    ]


async def test_close_drains_every_accepted_event_without_a_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 2)
    recorder = Recorder("cluster", "shard")
    recorder.set_process_up(True)
    recorder.set_player_count(2)
    events = EventStream(recorder)
    assert await events.accept(event_line(events.nonce, 1), 1)
    assert await events.accept(event_line(events.nonce, 2), 2)

    events.close()
    events.close()

    assert events.queue.qsize() == 2
    observed = []
    while event := await events.read():
        observed.append(event.record.seq)
    assert observed == [1, 2]
    assert events.dropped == 0
    assert recorder.process_up is False
    assert recorder.player_count == 0
    assert await events.read() is None
    with pytest.raises(asyncio.QueueShutDown):
        await events.queue.get()


async def test_close_unblocks_all_waiting_readers() -> None:
    events = EventStream(Recorder("cluster", "shard"))
    readers = [asyncio.create_task(events.read()) for _ in range(3)]
    await asyncio.sleep(0)

    events.close()

    async with asyncio.timeout(1):
        assert await asyncio.gather(*readers) == [None, None, None]


async def test_close_rejects_late_publishers_without_evicting_queued_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    recorded = Mock()
    monkeypatch.setattr(recorder, "record_event", recorded)
    events = EventStream(recorder)
    assert await events.accept(event_line(events.nonce, 1), 1)
    events.close()
    assert await events.accept(event_line(events.nonce, 2), 2)
    observed = await events.read()
    assert observed is not None
    assert observed.record.seq == 1
    assert await events.read() is None
    assert events.dropped == 1
    assert recorded.call_args_list == [
        call("accepted", event_name="dst.world.state_changed"),
        call("dropped", event_name="dst.world.state_changed", reason="stream_closed"),
    ]


async def test_closed_stream_accounts_for_late_events() -> None:
    events = EventStream(Recorder("cluster", "shard"))
    events.close()

    assert await events.accept(event_line(events.nonce, 1), 1)
    assert not await events.accept("ordinary log", 1)
    assert events.invalid == 0
    assert events.dropped == 1
    assert await events.read() is None
    assert await events.read() is None


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("dst.player.shard_entered", {"player": PLAYER}),
        ("dst.player.shard_left", {"player": PLAYER}),
        (
            "dst.player.action",
            {
                "action_id": "CHOP",
                "action_sequence": 1,
                "success": True,
                "reason": None,
                "error": None,
                "actor": PLAYER,
                "target": None,
                "initial_target_owner": None,
                "inventory_object": None,
                "position": None,
                "recipe": None,
                "forced": False,
            },
        ),
    ],
)
async def test_full_queue_updates_gameplay_metrics_once_before_consumption(
    monkeypatch: pytest.MonkeyPatch, name: str, data: dict[str, object]
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    actions = Mock()
    monkeypatch.setattr(recorder, "record_action", actions)
    events = EventStream(recorder)
    assert await events.accept(event_line(events.nonce, 1), 1)
    line = event_line(events.nonce, 2, event=name, data=data)
    assert await events.accept(line, 2)
    assert await events.accept(line, 3)
    assert events.queue.qsize() == 1
    assert events.dropped == events.duplicates == 1
    if name == "dst.player.action":
        actions.assert_called_once_with("CHOP", True)
    else:
        actions.assert_not_called()
        assert recorder.player_count == int(name.endswith("entered"))


async def test_line_limit_counts_utf8_event_bytes_only() -> None:
    events = EventStream(Recorder("cluster", "shard"))
    line = event_line(events.nonce, 1, session_id="测试")
    exact_limit = line + " " * (stream.MAX_LINE_BYTES - len(line.encode()))

    assert await events.accept("[00:00:01]: " + exact_limit, 1)
    assert await events.accept(exact_limit + " ", 2)

    observed = await events.read()
    assert observed is not None
    assert observed.record.session_id == "测试"
    assert events.queue.empty()
    assert events.invalid == 1


async def lua_driver_output(luajit: str, *arguments: str) -> list[bytes]:
    root = Path(__file__).parents[2]
    output = await asyncio.to_thread(
        run_lua_process,
        luajit,
        root / "tests/lua/driver_spec.lua",
        root,
        *arguments,
    )
    *lines, status = output.split(b"\n")
    assert status == b""
    assert lines.pop() == b"ok"
    return lines


@pytest.mark.parametrize("size", [65535, 65536, 65537])
@pytest.mark.parametrize("source", ["normal", "source"])
async def test_native_debugprint_preserves_telemetry_lines(
    luajit: str, size: int, source: str
) -> None:
    (line,) = await lua_driver_output(luajit, "print_boundary", str(size), source)
    events = EventStream(Recorder("cluster", "shard"))
    events.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    assert await events.accept(line, 1)
    assert events.invalid == 0
    assert events.queue.qsize() == 1
    observed = await events.read()
    assert observed is not None
    if size <= stream.MAX_LINE_BYTES:
        assert len(line) == size
        assert observed.record.event == "dst.shard.connection_changed"
    else:
        assert observed.record.event == "dst.telemetry.error"
        assert observed.record.data.message == "event_too_large"
    assert observed.record.seq == 2


@pytest.mark.parametrize("source", ["normal", "source"])
@pytest.mark.parametrize("output", ["print", "nolineprint"])
@pytest.mark.parametrize(
    "order", ["_".join(order) for order in permutations(("log", "error", "event"))]
)
async def test_native_debugprint_mixed_output_preserves_events(
    luajit: str, source: str, output: str, order: str
) -> None:
    lines = await lua_driver_output(luajit, "print_mixed", source, output, order)
    for prefix in (b"", b"[125:59:59]: "):
        events = EventStream(Recorder("cluster", "shard"))
        events.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        logs = []
        for timestamp, line in enumerate(lines, start=1):
            if not await events.accept(prefix + line, timestamp):
                logs.append(line)

        events.close()
        observed = []
        while event := await events.read():
            assert event.record.event == "dst.world.state_changed"
            observed.append((event.record.seq, event.record.data.value))
        assert observed == [(1, 2), (2, 3), (3, 4)]
        assert events.invalid == 0
        assert events.dropped == 0
        assert any(b"#DST_OTEL|" in line for line in logs)
        assert any(b"#LUA ERROR stack traceback:" in line for line in logs)
        assert any(b"\xff" in line for line in logs)
        ordinary = next(line for line in logs if b"ordinary\t" in line)
        assert ordinary.endswith(b"\t") == (output == "print")
        assert ordinary.startswith(b"@") == (source == "source" and output == "print")
        assert any("玩家👩🏽‍💻\u200b\u202e\ue000".encode() in line for line in logs)


async def test_rejected_events_report_bounded_schema_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder("cluster", "shard")
    recorded = Mock()
    event_logger = Mock()
    monkeypatch.setattr(recorder, "record_event", recorded)
    monkeypatch.setattr(recorder, "observe_log", event_logger)
    events = EventStream(recorder)
    cases = [
        (PREFIX + "{", "schema"),
        (PREFIX + "x" * stream.MAX_LINE_BYTES, "oversized"),
        (PREFIX.encode() + b"\xff", "encoding"),
        (PREFIX + "\ud800", "encoding"),
        (event_line("测" * 26, 1), "schema"),
        (event_line(str(ULID()), 1), "nonce"),
    ]

    for line, _ in cases:
        assert await events.accept(line, 1)
        assert await events.accept(line, 1)

    assert recorded.call_args_list == [
        call("invalid", reason=reason) for _, reason in cases for _ in range(2)
    ]
    assert events.invalid == len(cases) * 2
    assert events.queue.empty()
    assert event_logger.call_count == 10
    assert all("DST_OTEL|" not in str(args) for args in event_logger.call_args_list)


@pytest.mark.parametrize(
    "changes",
    [
        {"v": 1},
        {"generation": -1},
        {"generation": "1"},
        {"generation": True},
        {"session_id": ""},
        {"session_id": 1},
        {"seq": 0},
        {"seq": True},
        {"tick": "10"},
        {"unexpected": True},
        {"data": {"name": "cycles", "value": "2"}},
    ],
)
async def test_invalid_envelopes_do_not_poison_subsequent_events(
    changes: dict[str, object],
) -> None:
    events = EventStream(Recorder("cluster", "shard"))

    assert await events.accept(event_line(events.nonce, 1, **changes), 1)
    assert await events.accept(event_line(events.nonce, 2), 2)

    observed = await events.read()
    assert observed is not None
    assert observed.record.seq == 2
    assert events.queue.empty()
    assert events.invalid == 1


async def test_ordered_stream_rejects_duplicates_and_stale_generations() -> None:
    events = EventStream(Recorder("cluster", "shard"))
    identities = [(1, 1), (1, 4), (1, 4), (1, 2), (2, 1), (1, 5)]

    for timestamp, (generation, seq) in enumerate(identities, start=1):
        assert await events.accept(
            event_line(events.nonce, seq, generation=generation, session_id=None),
            timestamp,
        )
    events.close()

    observed = []
    while event := await events.read():
        assert event.record.session_id is None
        observed.append((event.record.generation, event.record.seq))
    assert observed == [(1, 1), (1, 4), (2, 1)]
    assert events.duplicates == 1
    assert events.stale == 2
    assert events.gaps == 2
    assert events.invalid == 0
    assert events.dropped == 0


async def test_presence_corrects_player_entities_and_independent_connections() -> None:
    recorder = Recorder("cluster", "shard")
    observer = Mock()
    events = EventStream(recorder, observe_event=observer)
    for sequence, guid in enumerate((42, 43), 1):
        await events.accept(
            event_line(
                events.nonce,
                sequence,
                event="dst.player.shard_entered",
                data={"player": PLAYER | {"guid": guid}},
            ),
            sequence,
        )
    assert recorder.player_count == 1
    await events.accept(
        event_line(
            events.nonce, 3, event="dst.player.shard_left", data={"player": PLAYER}
        ),
        3,
    )
    assert recorder.player_count == 1
    await events.accept(
        event_line(
            events.nonce,
            4,
            event="dst.client.authenticated",
            data={"userid": "KU_LOBBY"},
        ),
        4,
    )
    assert recorder.client_count == recorder.player_count == 1
    await events.accept(
        event_line(
            events.nonce,
            6,
            event="dst.server.presence",
            data={
                "reason": "startup",
                "clients": ["KU_LOBBY", "KU_LOBBY"],
                "players": [],
                "max_players": 9,
                "health": {
                    "protocol": 2,
                    "generation": 1,
                    "telemetry_status": "active",
                    "last_error": None,
                    "events_emitted": 5,
                    "errors": 0,
                },
            },
        ),
        60,
    )
    assert recorder.player_count == 0
    assert recorder.client_count == 1
    assert events.last_presence_timestamp_ns == events.last_event_timestamp_ns == 60
    assert events.gaps == 1
    assert observer.call_args.args[0].event == "dst.server.presence"
    await events.accept(
        event_line(
            events.nonce,
            7,
            event="dst.client.disconnected",
            data={"userid": "KU_LOBBY"},
        ),
        61,
    )
    assert recorder.client_count == 0
    events.start_generation(2)
    assert events.sequence == 0
    assert events.gaps == 1
    assert events.last_presence_timestamp_ns is None
    await events.accept(
        event_line(
            events.nonce, 8, event="dst.player.shard_entered", data={"player": PLAYER}
        ),
        62,
    )
    assert events.stale == 1
    assert recorder.player_count == 0


@pytest.mark.parametrize("profile", ["off", "critical"])
async def test_activity_survives_queue_eviction_and_includes_lobby_disconnect(
    monkeypatch: pytest.MonkeyPatch, profile: stream.TelemetryProfile
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    events = EventStream(recorder, profile=profile)
    for seq, event in enumerate(("authenticated", "disconnected"), 1):
        await events.accept(
            event_line(
                events.nonce,
                seq,
                event="dst.client." + event,
                data={"userid": "KU_LOBBY"},
            ),
            seq * 1_000_000_000,
        )
    assert events.last_active_at == datetime.fromtimestamp(2, UTC)
    assert recorder.client_count == 0
    await events.accept(
        event_line(events.nonce, 3, event="dst.mod.outdated", data={"name": "Insight"}),
        3_000_000_000,
    )
    assert events.last_active_at == datetime.fromtimestamp(2, UTC)
    if profile == "off":
        assert events.queue.empty()
    else:
        assert events.dropped == 2
    events.start_generation(2)
    assert events.last_active_at is None


async def test_server_refreshes_presence_before_consumption_and_resets_on_new_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    server = Server(ServerConfig(shard="forest"))
    events = server.game_events
    health = DriverHealth(
        protocol=2,
        generation=1,
        telemetry_status="active",
        last_error=None,
        events_emitted=0,
        errors=0,
    )
    await server._observe_driver(DriverReady(nonce=events.nonce, health=health))
    await events.accept(event_line(events.nonce, 1), 1)
    presence_health = health.replace(
        telemetry_status="failed",
        errors=1,
        last_error={"stage": "install", "message": "installation_failed", "count": 1},
        events_emitted=1,
    )
    try:
        await events.accept(
            event_line(
                events.nonce,
                2,
                event="dst.server.presence",
                data={
                    "reason": "startup",
                    "clients": ["KU_A", "KU_LOBBY"],
                    "players": [
                        {"userid": "KU_A", "guid": 1},
                        {"userid": "KU_A", "guid": 2},
                    ],
                    "max_players": 9,
                    "health": presence_health.model_dump(mode="json"),
                },
            ),
            60,
        )
        assert events.dropped == 1
        assert server.recorder.player_count == 1
        assert server.recorder.client_count == 2
        assert server.driver_health == presence_health.replace(events_emitted=2)
        assert server.driver.is_ready(1)
        # Duplicate native ready must not erase an already observed snapshot.
        await server._observe_driver(DriverReady(nonce=events.nonce, health=health))
        assert events.sequence == 2
        assert server.recorder.player_count == 1
        await server._observe_driver(DriverStarting(nonce=events.nonce, generation=2))
        assert events.sequence == 0
        assert server.recorder.player_count == server.recorder.client_count == 0
        assert events.last_presence_timestamp_ns is None
        assert not server.driver.is_ready(2)
        await events.accept(event_line(events.nonce, 3), 61)
        assert events.stale == 1
        assert events.sequence == 0
    finally:
        await server.finish()


@pytest.mark.parametrize("fragment", ['"seq":1,"seq":2', '"seq":1,"s\\u0065q":2'])
async def test_duplicate_json_keys_are_rejected(fragment: str) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    line = event_line(events.nonce, 1).replace('"seq":1', fragment)
    await events.accept(line, 1)
    assert events.invalid == 1
    assert events.queue.empty()


async def test_export_precedes_eviction_and_diagnostics_survive_saturation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    sink = SimpleNamespace(
        logs_enabled=True,
        resource=SimpleNamespace(attributes={"service.name": "test"}),
        emit_operational=Mock(),
    )
    recorder = Recorder("cluster", "shard", pipeline=cast("Pipeline", sink))
    metrics = recorder.record_event = Mock()
    events = EventStream(recorder)
    for sequence in (1, 3, 5, 7):
        await events.accept(event_line(events.nonce, sequence), sequence)
    # Replayed observations must not be logged or exported twice.
    await events.accept(event_line(events.nonce, 7), 8)
    for sequence in range(3):
        await events.accept(
            event_line(events.nonce, sequence + 8, data={"secret": "private-text"}),
            sequence + 9,
        )
    events.close()
    events.close()

    records = [call.kwargs for call in sink.emit_operational.call_args_list]
    games = [
        record for record in records if record["event_name"].startswith("dst.world.")
    ]
    assert [record["attributes"]["dst.event.sequence"] for record in games] == [
        1,
        3,
        5,
        7,
    ]
    assert [record["observed_timestamp_ns"] for record in games] == [1, 3, 5, 7]
    assert all(
        record["attributes"]["dst.cluster.name"] == "cluster" for record in records
    )
    assert events.dropped == events.gaps == events.invalid == 3
    assert events.queue.qsize() == 1
    for kind in ("sequence_gap", "notification_dropped", "rejected"):
        reports = [
            record
            for record in records
            if record["event_name"] == f"dst.telemetry.{kind}"
        ]
        assert [record["body"]["count"] for record in reports] == [1, 2, 3]
        assert sum(record["body"]["since_previous"] for record in reports) == 3
    assert "private-text" not in orjson.dumps(records).decode()
    assert metrics.call_args_list.count(call("invalid", reason="schema")) == 3
    assert metrics.call_args_list.count(call("gap", reason="sequence", count=1)) == 3


async def test_diagnostic_totals_are_attempt_scoped_across_world_generations() -> None:
    recorder = Recorder("cluster", "shard")
    sink = recorder.observe_log = Mock()
    events = EventStream(recorder)
    await events.accept(event_line(events.nonce, 3, generation=1), 100)
    await events.accept(event_line(events.nonce, 2, generation=2), 200)
    records = [
        call.kwargs
        for call in sink.call_args_list
        if call.kwargs["event_name"] == "dst.telemetry.sequence_gap"
    ]
    assert records[-1]["body"]["count"] == 3
    assert records[-1]["body"]["last_generation"] == 2
    assert records[-1]["body"]["last_after"] == 0
    assert records[-1]["body"]["last_next"] == 2
    assert "dst.runtime.generation" not in records[-1]["attributes"]


@pytest.mark.parametrize(
    "changes",
    [
        {"seq": 2**53},
        {"generation": 2**53},
        {"data": {"name": "cycles", "value": 2**53}},
        {"event": "dst.player.loaded", "data": {"player": PLAYER | {"guid": 2**53}}},
    ],
)
async def test_integer_boundary_rejection_does_not_interrupt_valid_observations(
    changes: dict[str, object],
) -> None:
    events = EventStream(Recorder("cluster", "shard"))
    await events.accept(event_line(events.nonce, 1, **changes), 1)
    await events.accept(event_line(events.nonce, 2), 2)
    events.close()
    assert events.invalid == 1
    observed = await events.read()
    assert observed is not None
    assert observed.record.seq == 2
    assert await events.read() is None
