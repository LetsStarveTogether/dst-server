"""Read a room's journal across a cursor, a coverage change and old chat records."""

# The Lua runtime and bootstrap scenario are repository test dependencies.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from dst_server import RoomStream
from dst_server.history import (
    EventFilter,
    HistoryRecord,
    count_by,
    read_history,
    replay_by_run,
)
from dst_server.host import Host
from dst_server.logs import JournalRecord

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
        "resource": {
            "service.name": "dst-server",
            "service.namespace": "test",
            "service.instance.id": instance,
            "dst.cluster.name": "cluster",
        }
        if instance
        else {},
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


JOURNAL = """
import json, pathlib, re, sys
base = pathlib.Path(sys.argv[0])
args = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)
with base.with_suffix('.args').open('a') as file:
    print(json.dumps(args), file=file)
rows = json.loads(base.with_suffix('.json').read_text())
rows = [r for r in rows if re.search(args['--grep'], r['MESSAGE'])]
if '--cursor' in args:
    matching = (i for i,r in enumerate(rows) if r['__CURSOR'] == args['--cursor'])
    start = next(matching, len(rows))
    rows = rows[start:]
for row in rows:
    print(json.dumps(row))
if rows and rows[0]['__CURSOR'] != '1':
    print('warning', file=sys.stderr)
"""


def journal_host(directory: Path, rows: tuple | list, *, user: bool = False) -> Host:
    reader = directory / "journal"
    reader.write_text(f"#!{sys.executable}\n{JOURNAL}")
    reader.chmod(0o755)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    reader.with_suffix(".json").write_text(
        json.dumps([
            row.fields
            | {
                "__CURSOR": str(i + 1),
                "MESSAGE": row.message,
                "__REALTIME_TIMESTAMP": str(
                    (row.timestamp - epoch) // timedelta(microseconds=1)
                ),
            }
            for i, row in enumerate(rows)
        ])
    )
    return Host(directory, directory, journalctl=reader, user=user)


def history_rows(body: dict) -> tuple[tuple, tuple, int]:
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
    initial = (*bad_coverage, coverage, roster)
    tail = (
        roster,
        coverage,
        *unrelated,
        *bad_events,
        replace(entry(11, "dst.player.public_chat", CHAT), fields={"UNIT": UNIT}),
        replace(
            entry(12, "dst.player.public_chat", CHAT),
            fields={"_SYSTEMD_USER_UNIT": UNIT},
        ),
        public,
        reloaded,
    )
    return initial, tail, len(bad_events)


def check_arguments(directory: Path, candidates: tuple, cursor: str | None) -> None:
    calls = [
        json.loads(line)
        for line in (directory / "journal.args").read_text().splitlines()
    ]
    before, after = calls[:2]
    assert all(re.search(before["--grep"], row.message) for row in candidates)
    assert before["--since"] == START.strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    assert "--cursor" not in before
    assert "--since" not in after
    assert after["--cursor"] == cursor
    assert (
        after["--until"]
        == before["--until"]
        == (END - timedelta(microseconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    )


async def check_history(body: dict, directory: Path) -> None:
    initial, tail, invalid_events = history_rows(body)
    coverage, bad_coverage = coverage_rows(body)
    public = entry(3, "dst.player.public_chat", CHAT, tick=2, sequence=3)
    host = journal_host(directory, [*initial, *tail])
    pages = [
        page
        async for page in read_history(
            host, [7], since=START, until=END, page_size=len(initial)
        )
    ]
    first, *remaining = pages
    check_arguments(directory, initial, first.next_cursor)
    assert first.reader.effective_uid == os.geteuid()
    assert first.reader.effective_gid == os.getegid()
    assert first.reader.groups == tuple(sorted(set(os.getgroups())))
    assert first.reader.host_id
    assert first.reader.host_id_kind in {"machine_id", "boot_id"}
    assert first.reader.executable == str(directory / "journal")
    assert first.rejected == len(bad_coverage)
    assert first.coverage[0].data == body
    assert first.coverage[0].shard == "forest"
    assert RoomStream.from_journal(first.coverage[0], tenant="test") == RoomStream(
        7, "dst-server", "test", "cluster", "agent-1", "test"
    )
    (fact,) = first.records
    assert fact.unit == UNIT
    assert fact.observed_timestamp_ns == 1_798_000_000_123_456_789
    assert set(fact.participants()) == {
        ("clients[]", "KU_lobby"),
        ("clients[]", "KU_a"),
        ("players[].userid", "KU_a"),
    }
    tail_records = tuple(r for page in remaining for r in page.records)
    assert [record.uid for record in tail_records] == [uid(1), uid(3), uid(4)]
    assert [c.data for page in remaining for c in page.coverage] == [body]
    assert first.observations == (*first.coverage, *first.records)
    assert first.coverage[0].receipt_time == first.records[0].receipt_time == START
    capture = first.coverage[0].capture_for("dst.player.ate")
    assert capture.capabilities == {"players": body["capabilities"]["players"]}
    assert tail_records[1].event["data"]["message"] == "public text"
    runs = replay_by_run((*first.records, *tail_records))
    assert [record.uid for record in runs[first.coverage[0].run]] == [uid(3), uid(1)]
    assert runs[tail_records[-1].run] == [tail_records[-1]]
    assert sum(page.rejected for page in remaining) == invalid_events
    assert any(
        r.reason == "unsupported_schema" for page in remaining for r in page.rejections
    )
    assert remaining[-1].next_cursor == str(len(initial) + len(tail))
    assert remaining[-1].diagnostics == "warning"
    selected = [
        p
        async for p in read_history(
            host,
            [7],
            since=START,
            until=END,
            page_size=1,
            where=EventFilter(events=("dst.player.public_chat",), userid="KU_a"),
        )
    ]
    assert [r.uid for p in selected for r in p.records] == [uid(3)]
    assert [r.uid for p in selected for r in p.coverage] == [uid(2), uid(2)]
    assert sum(p.rejected for p in selected) == invalid_events + len(bad_coverage)
    assert any(not p.observations and p.has_more for p in selected)
    assert not remaining[-1].has_more
    assert count_by(
        [fact, fact], event="dst.server.presence", field="data.players[].userid"
    ) == [("KU_a", 1)]
    assert count_by([fact], field="data.players[].loaded") == [(True, 1), (None, 1)]
    assert count_by([fact], field="generation") == [(1, 1)]
    await check_user_history(directory, body, coverage, public, bad_coverage)
    await check_resumed_counts(directory)


async def check_resumed_counts(directory: Path) -> None:
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

    empty = journal_host(directory, ())
    [prior] = [p async for p in read_history(empty, [7], since=START, until=END)]
    assert prior.visible_units == ()
    assert prior.next_cursor is None
    assert not prior.has_more
    host = journal_host(directory, rows)
    read = partial(read_history, host, [7], since=START, until=END)
    for size in (1, 2, len(rows)):
        pages = [page async for page in read(page_size=size)]
        assert [r.uid for p in pages for r in p.records] == list(map(uid, uids))
        first = pages[0]
        assert first.next_cursor == str(size)
        count = partial(count_by, field="data.userid", seen=set())
        totals = Counter(dict(count(first.records)))
        for _ in range(2):
            async for page in read(page_size=size, checkpoint=first.checkpoint):
                totals.update(dict(count(page.records)))
        assert totals == {"KU_a": 1, "KU_b": 2}
        failure = None
        try:
            await anext(
                read_history(
                    host, [7, 200], since=START, until=END, checkpoint=first.checkpoint
                )
            )
        except ValueError as error:
            failure = str(error)
        assert failure
        assert "different history selection" in failure


async def check_user_history(
    directory: Path,
    body: dict,
    coverage: JournalRecord,
    public: JournalRecord,
    invalid: list[JournalRecord],
) -> None:
    user_fields = {
        "_SYSTEMD_UNIT": "user@1000.service",
        "_SYSTEMD_USER_UNIT": UNIT,
        "_UID": str(os.geteuid()),
    }
    rows = (
        *(replace(row, fields=user_fields) for row in invalid),
        entry(13, COVERAGE_NAME, body, sequence=None, instance="system"),
        replace(
            entry(14, COVERAGE_NAME, body, sequence=None, instance="spoof"),
            fields={"UNIT": UNIT},
        ),
        replace(coverage, fields=user_fields),
        public,
    )
    host = journal_host(directory, rows, user=True)
    [prior] = [
        page
        async for page in read_history(
            host, [7], since=START, until=END, coverage_only=True
        )
    ]
    pattern = json.loads((directory / "journal.args").read_text().splitlines()[-1])[
        "--grep"
    ]
    assert re.search(pattern, coverage.message)
    assert not re.search(pattern, public.message)
    assert [(row.uid, row.agent_instance_id) for row in prior.coverage] == [
        (uid(2), "agent-1")
    ]
    assert prior.rejected == len(invalid)
    assert prior.next_cursor == str(len(rows) - 1)
    assert prior.records == ()
    user_gameplay = replace(public, fields=user_fields | {"MESSAGE": public.message})
    assert HistoryRecord.from_journal(user_gameplay) is None
    record = HistoryRecord.from_journal(user_gameplay, user=True)
    assert record is not None
    assert record.unit == UNIT
    assert record.uid == uid(3)
    for plain in (None, [255, 254], ["server started", "server stopped"]):
        raw = replace(user_gameplay, fields=user_fields | {"MESSAGE": plain})
        assert HistoryRecord.from_journal(raw, user=True) is None
    for field in (
        list(public.message.encode().replace(b"KU_a", b"KU_\xff")),
        [public.message, public.message],
    ):
        bad = replace(user_gameplay, fields=user_fields | {"MESSAGE": field})
        failure = None
        try:
            HistoryRecord.from_journal(bad, user=True)
        except ValueError as error:
            failure = str(error)
        assert failure
        assert "InvalidEnvelope" in failure


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp:
        asyncio.run(check_history(capture_body(), Path(temp)))
