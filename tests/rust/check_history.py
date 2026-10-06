"""Read a room's journal across a cursor, a coverage change and old chat records."""

# The Lua runtime and bootstrap scenario are repository test dependencies.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import asyncio
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

from dst_server.history import HistoryRecord, count_by, read_history, replay_by_run
from dst_server.host import Host
from dst_server.logs import JournalQuery, JournalRecord, JournalResult

START = datetime(2026, 11, 20, tzinfo=UTC)
END = START + timedelta(days=1)
UNIT = "dst-007.service"
CHAT = {
    "userid": "KU_a",
    "name": "Survivor",
    "prefab": "wilson",
    "message": "public text",
    "emote": False,
}


COVERAGE_NAME = "dst.telemetry.coverage"


def capture_body() -> dict:
    runtime = shutil.which("luajit")
    assert runtime
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            runtime,
            root / "tests/lua/bootstrap_spec.lua",
            root,
            root / "dst-scripts/scripts",
            "optional_failure",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    health = json.loads(result.stdout.splitlines()[0].removeprefix(b"DST_DRIVER|"))[
        "health"
    ]
    for field in ("generation", "control_ready", "events_emitted"):
        del health[field]
    return {
        **health,
        "requested_profile": health["profile"],
        "shard_loss": dict.fromkeys(
            (
                "telemetry_gaps",
                "invalid_telemetry",
                "generation_rejected",
                "sequence_rejected",
                "reply_dropped",
                "relay_dropped",
            ),
            0,
        ),
        "room_loss": {"stdout_dropped": 0},
    }


def uid(number: int) -> str:
    return f"{number:026d}"


def entry(
    number: int,
    event: str,
    data: object,
    *,
    record_uid: str | None = None,
    nonce: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    sequence: int | None = 2,
    tick: int = 3,
    generation: int = 1,
    unit: str = UNIT,
    instance: str | None = None,
) -> JournalRecord:
    attrs = {
        "log.record.uid": uid(number) if record_uid is None else record_uid,
        "dst.cluster.name": "cluster",
        "dst.shard.name": "forest",
        "dst.game.attempt.id": nonce,
        "dst.runtime.generation": generation,
    }
    if sequence is not None:
        attrs.update({
            "dst.event.sequence": sequence,
            "dst.tick": tick,
            "dst.monotonic_ms": tick * 100,
        })
    message = "DST_RECORD|" + json.dumps({
        "body": data,
        "event_name": event,
        "observed_timestamp_ns": 1_798_000_000_123_456_789,
        "resource": {"service.instance.id": instance} if instance else {},
        "attributes": attrs,
    })
    return JournalRecord({"_SYSTEMD_UNIT": unit}, str(number), START, UNIT, message)


def coverage_rows(body: dict) -> tuple[JournalRecord, list[JournalRecord]]:
    capture = partial(entry, 2, COVERAGE_NAME, sequence=None, instance="agent-1")
    return capture(body), [
        capture({k: v for k, v in body.items() if k != "revision"}),
        capture(body | {"play_queue_dropped": -1}),
        capture(body | {"shard_loss": body["shard_loss"] | {"relay_dropped": -1}}),
        capture(body | {"room_loss": [0]}),
        capture(body | {"unexpected": "extra field"}),
        capture(body, record_uid="invalid"),
        capture(body, nonce="invalid"),
        capture(body, generation=2**53),
        capture(body, instance=None),
    ]


async def check_history(body: dict) -> None:
    world_fields = [
        "season",
        "phase",
        "moonphase",
        "nightmarephase",
        "israining",
        "issnowing",
        "isacidraining",
        "islunarhailing",
        "lunar_rifts_enabled",
        "shadow_rifts_enabled",
    ]
    roster = entry(
        1,
        "dst.server.presence",
        {
            "clients": ["KU_a", "KU_lobby"],
            "players": [
                {"userid": "KU_a", "prefab": prefab, "is_ghost": False, "loaded": ready}
                for prefab, ready in [("wilson", True), ("willow", None)]
            ],
            "world": dict.fromkeys(world_fields),
            "reason": "startup",
        },
    )
    coverage, bad_coverage = coverage_rows(body)
    public = entry(3, "dst.player.public_chat", CHAT, tick=2, sequence=3)
    reloaded = entry(
        4,
        "dst.client.authenticated",
        {"userid": "KU_a"},
        generation=2,
        tick=1,
        sequence=1,
    )
    bad_events = [
        entry(
            5 + int(whisper),
            "dst.player.chat",
            {**CHAT, "message": "old chat", "whisper": whisper, "player": None},
        )
        for whisper in (True, False)
    ]
    bad_events.extend((
        # A legacy projection may already have discarded both whisper and player.
        entry(7, "dst.player.chat", CHAT),
        entry(10, "dst.client.authenticated", {}),
        entry(13, "dst.client.authenticated", ["KU_a"]),
        entry(14, "dst.player.shard_left", {"player": [1, "wilson", "KU_a"]}),
        entry(15, "dst.client.authenticated", {"userid": "KU_a"}, sequence=None),
    ))
    # The nested key can match journalctl's text filter on an operational record.
    unrelated = (
        entry(8, "dst.control.reply", {"dst.event.sequence": 1}, sequence=None),
        entry(
            9,
            "dst.client.authenticated",
            {"userid": "KU_x"},
            unit="other.service",
        ),
    )
    host = AsyncMock(spec=Host)
    host.user = False
    host.journal.side_effect = [
        JournalResult((*bad_coverage, coverage, roster), "first", True, "", False),
        JournalResult(
            (
                roster,
                coverage,
                *unrelated,
                *bad_events,
                replace(
                    entry(11, "dst.player.public_chat", CHAT), fields={"UNIT": UNIT}
                ),
                replace(
                    entry(12, "dst.player.public_chat", CHAT),
                    fields={"_SYSTEMD_USER_UNIT": UNIT},
                ),
                public,
                reloaded,
            ),
            "last",
            False,
            "warning",
            True,
        ),
    ]
    first, last = [
        page async for page in read_history(host, [7], since=START, until=END)
    ]
    before, after = (call.args[1] for call in host.journal.await_args_list)
    assert before.grep
    assert all(
        re.search(before.grep, row.message)
        for row in (coverage, roster, public, *bad_events)
    )
    assert before.since == START
    assert before.cursor is None
    assert after.since is None
    assert after.cursor == first.next_cursor == "first"
    assert after.until == before.until == END - timedelta(microseconds=1)
    assert first.rejected == len(bad_coverage)
    assert first.coverage[0].data == body
    assert first.coverage[0].shard == "forest"
    (fact,) = first.records
    assert fact.unit == UNIT
    assert fact.observed_timestamp_ns == 1_798_000_000_123_456_789
    assert set(fact.participants()) == {
        ("clients[]", "KU_lobby"),
        ("clients[]", "KU_a"),
        ("players[].userid", "KU_a"),
    }
    assert [record.uid for record in last.records] == [uid(1), uid(3), uid(4)]
    assert last.coverage == first.coverage
    assert last.records[1].event["data"]["message"] == "public text"
    runs = replay_by_run((*first.records, *last.records))
    assert [record.uid for record in runs[first.coverage[0].run]] == [
        uid(3),
        uid(1),
    ]
    assert runs[last.records[-1].run] == [last.records[-1]]
    assert last.rejected == len(bad_events)
    assert last.diagnostics_truncated
    assert last.next_cursor == "last"
    assert last.diagnostics == "warning"
    assert count_by(
        [fact, fact], event="dst.server.presence", field="data.players[].userid"
    ) == [("KU_a", 1)]
    assert count_by([fact], field="data.players[].loaded") == [(True, 1), (None, 1)]
    assert count_by([fact], field="generation") == [(1, 1)]
    await check_user_history(body, coverage, public, bad_coverage)
    await check_resumed_counts()


async def check_resumed_counts() -> None:
    uids = [1, 2, 1, 3, 2]
    rows = tuple(
        entry(
            position,
            "dst.client.authenticated",
            {"userid": "KU_a" if number == 1 else "KU_b"},
            record_uid=uid(number),
        )
        for position, number in enumerate(uids, start=1)
    )

    def journal(_rooms: list[int], query: JournalQuery) -> JournalResult:
        start = int(query.cursor or 0)
        assert query.limit is not None
        end = min(start + query.limit, len(rows))
        return JournalResult(rows[start:end], str(end), end < len(rows), "", False)

    host = AsyncMock(spec=Host)
    host.user = False
    host.journal.side_effect = journal
    read = partial(read_history, host, [7], since=START, until=END)
    for size in (1, 2, len(rows)):
        pages = [page async for page in read(page_size=size)]
        assert [r.uid for p in pages for r in p.records] == list(map(uid, uids))
        first = pages[0]
        assert first.next_cursor == str(size)
        count = partial(count_by, field="data.userid", seen=set())
        totals = Counter(dict(count(first.records)))
        for _ in range(2):
            async for page in read(page_size=size, cursor=first.next_cursor):
                totals.update(dict(count(page.records)))
        assert totals == {"KU_a": 1, "KU_b": 2}


async def check_user_history(
    body: dict,
    coverage: JournalRecord,
    public: JournalRecord,
    invalid: list[JournalRecord],
) -> None:
    host = AsyncMock(spec=Host)
    host.user = True
    user_fields = {"_SYSTEMD_UNIT": "user@1000.service", "_SYSTEMD_USER_UNIT": UNIT}
    host.journal.return_value = JournalResult(
        (
            *(replace(row, fields=user_fields) for row in invalid),
            entry(13, COVERAGE_NAME, body, sequence=None, instance="system"),
            replace(
                entry(14, COVERAGE_NAME, body, sequence=None, instance="spoof"),
                fields={"UNIT": UNIT},
            ),
            replace(coverage, fields=user_fields),
            public,
        ),
        "end",
        False,
        "",
        False,
    )
    [prior] = [
        page
        async for page in read_history(
            host, [7], since=START, until=END, coverage_only=True
        )
    ]
    pattern = host.journal.await_args.args[1].grep
    assert re.search(pattern, coverage.message)
    assert not re.search(pattern, public.message)
    assert [(row.uid, row.agent_instance_id) for row in prior.coverage] == [
        (uid(2), "agent-1")
    ]
    assert prior.rejected == len(invalid)
    assert prior.next_cursor == "end"
    assert prior.records == ()
    user_gameplay = replace(public, fields=user_fields)
    assert HistoryRecord.from_journal(user_gameplay) is None
    record = HistoryRecord.from_journal(user_gameplay, user=True)
    assert record is not None
    assert record.unit == UNIT
    assert record.uid == uid(3)


if __name__ == "__main__":
    asyncio.run(check_history(capture_body()))
