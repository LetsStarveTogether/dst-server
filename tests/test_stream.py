import asyncio
import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from itertools import permutations
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from ulid import ULID

from dst_server.telemetry import stream
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import PREFIX, EventStream

PLAYER = {"prefab": "wilson", "guid": 42, "userid": "KU_TEST", "position": None}
NATIVE_PREFIXES = ["", "[00:00:01]: ", "[125:59:59]: "]


def event_line(nonce: str, sequence: int, **changes: object) -> str:
    return PREFIX + json.dumps(
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
        ensure_ascii=False,
    )


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


@pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize(
    "kind", ["valid", "wrong_nonce", "bad_nonce", "schema", "surrogate"]
)
@pytest.mark.parametrize(
    "size", [None, 65536, 65537], ids=["short", "exact", "oversized"]
)
async def test_mixed_validation_preserves_later_events(
    prefix: str, as_bytes: bool, kind: str, size: int | None
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
        session_id="\ud800" if kind == "surrogate" else "👩🏽‍💻e\u0301\u2028",
    )
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
        framed = prefix + line
        assert await events.accept(
            framed.encode(errors="surrogatepass") if as_bytes else framed, timestamp
        ) == (timestamp in {1, 5, 7})

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


async def test_full_stream_waits_for_space_without_dropping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    recorded = Mock()
    monkeypatch.setattr(recorder, "record_event", recorded)
    events = EventStream(recorder)
    assert await events.accept(event_line(events.nonce, 1), 11)

    publishing = asyncio.create_task(events.accept(event_line(events.nonce, 2), 22))
    await asyncio.sleep(0)

    assert not publishing.done()
    assert events.queue.qsize() == 1
    assert events.dropped == 0
    recorded.assert_called_once_with("accepted", event_name="dst.world.state_changed")
    first = await events.read()
    assert first is not None
    assert (first.record.seq, first.observed_timestamp_ns) == (1, 11)
    async with asyncio.timeout(1):
        assert await publishing
    second = await events.read()
    assert second is not None
    assert (second.record.seq, second.observed_timestamp_ns) == (2, 22)
    assert recorded.call_args_list == [
        call("accepted", event_name="dst.world.state_changed"),
        call("accepted", event_name="dst.world.state_changed"),
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


async def test_close_rejects_blocked_publishers_without_evicting_accepted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    recorded = Mock()
    monkeypatch.setattr(recorder, "record_event", recorded)
    events = EventStream(recorder)
    assert await events.accept(event_line(events.nonce, 1), 1)
    publishing = asyncio.create_task(events.accept(event_line(events.nonce, 2), 2))
    await asyncio.sleep(0)
    assert not publishing.done()

    events.close()

    async with asyncio.timeout(1):
        assert await publishing
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
async def test_cancelled_backpressure_has_no_gameplay_metric_side_effects(
    monkeypatch: pytest.MonkeyPatch, name: str, data: dict[str, object]
) -> None:
    monkeypatch.setattr(stream, "QUEUE_SIZE", 1)
    recorder = Recorder("cluster", "shard")
    recorder.set_player_count(2)
    actions = Mock()
    recorded = Mock()
    monkeypatch.setattr(recorder, "record_action", actions)
    monkeypatch.setattr(recorder, "record_event", recorded)
    events = EventStream(recorder)
    assert await events.accept(event_line(events.nonce, 1), 1)
    line = event_line(events.nonce, 2, event=name, data=data)
    publishing = asyncio.create_task(events.accept(line, 2))
    await asyncio.sleep(0)

    assert recorder.player_count == 2
    actions.assert_not_called()
    publishing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await publishing
    assert events.queue.qsize() == 1
    assert recorder.player_count == 2
    assert events.dropped == 1
    actions.assert_not_called()
    assert recorded.call_args_list == [
        call("accepted", event_name="dst.world.state_changed"),
        call("dropped", event_name=name, reason="ingress_cancelled"),
    ]

    assert await events.read() is not None
    assert await events.accept(line, 2)
    if name == "dst.player.action":
        actions.assert_called_once_with("CHOP", True)
    else:
        assert recorder.player_count == (3 if name.endswith("entered") else 1)


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
    root = Path(__file__).parents[1]
    result = await asyncio.to_thread(
        subprocess.run,
        [
            luajit,
            str(root / "tests/lua/driver_spec.lua"),
            str(root),
            *arguments,
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert isinstance(result.stdout, bytes)
    *lines, status = result.stdout.split(b"\n")
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
        assert observed.record.event == "dst.world.state_changed"
    else:
        assert observed.record.event == "dst.telemetry.error"
        assert observed.record.data.message == "event_too_large"
    assert observed.record.seq == 1


@pytest.mark.parametrize("prefix", ["", "[125:59:59]: "])
@pytest.mark.parametrize("source", ["normal", "source"])
@pytest.mark.parametrize("output", ["print", "nolineprint"])
@pytest.mark.parametrize(
    "order", ["_".join(order) for order in permutations(("log", "error", "event"))]
)
async def test_native_debugprint_mixed_output_preserves_events(
    luajit: str, prefix: str, source: str, output: str, order: str
) -> None:
    lines = await lua_driver_output(luajit, "print_mixed", source, output, order)
    events = EventStream(Recorder("cluster", "shard"))
    events.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    logs = []
    for timestamp, line in enumerate(lines, start=1):
        if not await events.accept(prefix.encode() + line, timestamp):
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


async def test_rejected_events_warn_once_per_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder("cluster", "shard")
    recorded = Mock()
    event_logger = Mock()
    monkeypatch.setattr(recorder, "record_event", recorded)
    monkeypatch.setattr(stream, "logger", event_logger)
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
    assert event_logger.warning.call_count == 4
    assert all(
        "DST_OTEL|" not in str(args) for args in event_logger.warning.call_args_list
    )


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


async def test_sequences_are_preserved_without_inventing_a_loss_cause() -> None:
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
    assert observed == identities
    assert events.invalid == 0
    assert events.dropped == 0
