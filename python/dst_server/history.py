"""Read a bounded window of managed room history from the local journal."""

import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Iterator, MutableSet, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from ._native import DstError, event_userid_fields, validate_coverage
from .events import GameEvent, event_names, validate_event
from .host import Host
from .logs import JournalQuery, JournalRecord, _utc

__all__ = [
    "CoverageChange",
    "HistoryPage",
    "HistoryRecord",
    "ShardRun",
    "count_by",
    "participants",
    "read_history",
    "replay_by_run",
]

type Scalar = str | int | float | bool | None
_PREFIX = "DST_RECORD|"
_COVERAGE = "dst.telemetry.coverage"
_HISTORY_EVENTS = (*event_names(), _COVERAGE)
_NAME_FIELD = r'"event_name"\s*:\s*'
_COVERAGE_FIELD = rf'{_NAME_FIELD}"{re.escape(_COVERAGE)}"'
_HISTORY_FIELDS = (
    r'(?:"dst\.event\.sequence"\s*:|'
    rf'{_NAME_FIELD}"(?:{"|".join(map(re.escape, _HISTORY_EVENTS))})")'
)
_UNIT = re.compile(r"dst-[0-2][0-9]{2}\.service")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ACCOUNT_PATHS = {field: field.split(".") for field in event_userid_fields()}


def _values(value: object, path: list[str]) -> Iterator[Scalar]:
    if not path:
        if value is None or isinstance(value, (str, int, float)):
            yield value
        return
    key = path[0].removesuffix("[]")
    if not isinstance(value, dict) or key not in value:
        return
    child = value[key]
    if key == path[0]:
        yield from _values(child, path[1:])
    elif isinstance(child, list):
        for item in child:
            yield from _values(item, path[1:])


def participants(event: GameEvent) -> Iterator[tuple[str, str]]:
    """Yield (body field, userid) for each observed role, including roster entries."""
    for field, path in _ACCOUNT_PATHS.items():
        for value in _values(event["data"], path):
            if isinstance(value, str):
                yield field, value


@dataclass(frozen=True, slots=True)
class _Observation:
    uid: str
    unit: str
    cluster: str
    shard: str
    observed_timestamp_ns: int


class ShardRun(NamedTuple):
    """Journal source within which the game's occurrence clocks can be compared."""

    unit: str
    cluster: str
    shard: str
    nonce: str
    generation: int


@dataclass(frozen=True, slots=True)
class HistoryRecord(_Observation):
    event: GameEvent

    @property
    def run(self) -> ShardRun:
        return ShardRun(
            self.unit,
            self.cluster,
            self.shard,
            self.event["nonce"],
            self.event["generation"],
        )

    def participants(self) -> Iterator[tuple[str, str]]:
        return participants(self.event)

    @staticmethod
    def from_journal(
        entry: JournalRecord, *, user: bool = False
    ) -> HistoryRecord | None:
        """Read one gameplay fact. Malformed observations raise ValueError."""
        observation = _decode(entry, user=user)
        return observation if isinstance(observation, HistoryRecord) else None


@dataclass(frozen=True, slots=True)
class CoverageChange(_Observation):
    """Capture settings and observer health reported by a shard's Agent."""

    nonce: str
    generation: int
    data: dict[str, Any]
    agent_instance_id: str

    @property
    def run(self) -> ShardRun:
        return ShardRun(
            self.unit, self.cluster, self.shard, self.nonce, self.generation
        )


def replay_by_run(
    records: Iterable[HistoryRecord],
) -> dict[ShardRun, list[HistoryRecord]]:
    """Group a bounded selection of records by run and sort each run by game time.

    Pass records from every page in the selection. The first observation of each
    UID is replayed. A tie on both game clocks retains send order.
    The order of occurrence within that tie is unknown.
    Clocks from different runs are independent.

    Returns:
        Records for each run in occurrence order, with runs in first-seen order.
    """
    by_uid: dict[str, HistoryRecord] = {}
    for record in records:
        by_uid.setdefault(record.uid, record)
    runs: dict[ShardRun, list[HistoryRecord]] = {}
    for record in by_uid.values():
        runs.setdefault(record.run, []).append(record)
    for rows in runs.values():
        rows.sort(
            key=lambda row: (
                row.event["tick"],
                row.event["monotonic_ms"],
                row.event["seq"],
            )
        )
    return runs


@dataclass(frozen=True, slots=True)
class HistoryPage:
    records: tuple[HistoryRecord, ...]
    coverage: tuple[CoverageChange, ...]
    next_cursor: str | None
    rejected: int
    diagnostics: str
    diagnostics_truncated: bool


def _observation(
    unit: str, log: dict[str, Any]
) -> HistoryRecord | CoverageChange | None:
    attrs = log["attributes"]
    name = log["event_name"]
    if "dst.event.sequence" not in attrs and name not in _HISTORY_EVENTS:
        return None
    data = log["body"]
    uid = attrs["log.record.uid"]
    cluster = attrs["dst.cluster.name"]
    shard = attrs["dst.shard.name"]
    nonce = attrs["dst.game.attempt.id"]
    generation = attrs["dst.runtime.generation"]
    observed = log["observed_timestamp_ns"]
    if (
        not _ULID.fullmatch(uid)
        or not _ULID.fullmatch(nonce)
        or not all(isinstance(v, str) and v for v in (cluster, shard))
    ):
        message = "invalid log observation identity"
        raise ValueError(message)
    if type(observed) is not int or not 0 <= observed < 2**64:
        message = "invalid log observation timestamp"
        raise ValueError(message)
    if type(generation) is not int or not 0 <= generation < 2**53:
        message = "invalid runtime generation"
        raise ValueError(message)
    if name == _COVERAGE:
        instance = log["resource"]["service.instance.id"]
        if not isinstance(instance, str) or not instance.strip():
            message = "invalid coverage agent instance"
            raise ValueError(message)
        validate_coverage(data)
        return CoverageChange(
            uid,
            unit,
            cluster,
            shard,
            observed,
            nonce,
            generation,
            data,
            agent_instance_id=instance,
        )
    event = validate_event({
        "event": name,
        "nonce": nonce,
        "generation": generation,
        "session_id": attrs.get("dst.session.id"),
        "seq": attrs["dst.event.sequence"],
        "tick": attrs["dst.tick"],
        "monotonic_ms": attrs["dst.monotonic_ms"],
        "cycle": attrs.get("dst.world.cycle"),
        "data": data,
    })
    return HistoryRecord(uid, unit, cluster, shard, observed, event)


def _decode(
    entry: JournalRecord,
    units: set[str] | None = None,
    *,
    user: bool,
    coverage_only: bool = False,
) -> HistoryRecord | CoverageChange | None:
    # UNIT can name a service on another process's message.
    unit = entry.fields.get("_SYSTEMD_USER_UNIT" if user else "_SYSTEMD_UNIT")
    if (
        not isinstance(unit, str)
        or not _UNIT.fullmatch(unit)
        or (units is not None and unit not in units)
        or not entry.message.startswith(_PREFIX)
    ):
        return None
    try:
        log = json.loads(entry.message[len(_PREFIX) :])
        if coverage_only and log["event_name"] != _COVERAGE:
            return None
        return _observation(unit, log)
    except (
        AttributeError,
        KeyError,
        RecursionError,
        TypeError,
        ValueError,
        DstError,
    ) as error:
        message = f"invalid structured room log at journal cursor {entry.cursor}"
        raise ValueError(message) from error


async def read_history(
    host: Host,
    numbers: Sequence[int],
    *,
    since: datetime,
    until: datetime,
    cursor: str | None = None,
    page_size: int = 500,
    coverage_only: bool = False,
) -> AsyncIterator[HistoryPage]:
    """Scan [since, until) in pages ordered by journal receipt time.

    The Host selects the system or user journal and its trusted service field.
    Resume a cursor with the same Host, room selection and coverage_only setting.
    An expired cursor or one outside the journal filter raises JournalCursorError.
    Use coverage_only on a preceding window to read just the coverage changes
    and carry the last known observation for each shard run into the next window.

    Yields:
        Each page contains every valid gameplay or coverage observation it reads.
        A UID may repeat within a page, across pages or after resuming.
        Rejected counts malformed journal entries. The cursor advances over them.
        A resumable job can commit results keyed by UID and this cursor together.
        A window with unknown coverage cannot establish a gameplay count of zero.

    Raises:
        ValueError: The window, room selection, or page size is invalid.
    """
    since, until = _utc(since), _utc(until)
    if until <= since or page_size < 1:
        message = "history requires a positive window and page size"
        raise ValueError(message)
    units = {f"dst-{number:03}.service" for number in numbers}
    fields = _COVERAGE_FIELD if coverage_only else _HISTORY_FIELDS
    pattern = rf"^DST_RECORD\|.*{fields}"
    user = host.user
    while True:
        result = await host.journal(
            numbers,
            JournalQuery(
                limit=page_size,
                direction="forward",
                cursor=cursor,
                # journalctl accepts either a start time or a cursor.
                since=since if cursor is None else None,
                until=until - timedelta(microseconds=1),
                grep=pattern,
            ),
        )
        records: list[HistoryRecord] = []
        coverage: list[CoverageChange] = []
        rejected = 0
        for entry in result.records:
            if not since <= entry.timestamp < until:
                continue
            try:
                observation = _decode(
                    entry, units, user=user, coverage_only=coverage_only
                )
            except ValueError:
                rejected += 1
                continue
            if observation is None:
                continue
            if isinstance(observation, HistoryRecord):
                records.append(observation)
            else:
                coverage.append(observation)
        cursor = result.next_cursor or cursor
        yield HistoryPage(
            tuple(records),
            tuple(coverage),
            cursor,
            rejected,
            result.diagnostics,
            result.diagnostics_truncated,
        )
        if not result.has_more:
            return


def count_by(
    records: Iterable[HistoryRecord],
    *,
    field: str,
    event: str | None = None,
    seen: MutableSet[str] | None = None,
) -> list[tuple[Scalar, int]]:
    """Count distinct UIDs by a GameEvent field, e.g. cycle or data.players[].userid.

    Pass the same seen set across pages and resumed reads of one aggregation.
    Each call counts newly seen UIDs for the selected event.

    Returns:
        (value, count) pairs in first observation order. Missing fields have no group.
        An explicit JSON null forms its own group. False and zero stay separate.

    Raises:
        ValueError: The GameEvent field path is malformed.
    """
    path = field.split(".")
    if not all(key.removesuffix("[]").isidentifier() for key in path):
        message = "use a dotted GameEvent field with [] for array elements"
        raise ValueError(message)
    if seen is None:
        seen = set()
    counts: Counter[tuple[type, Scalar]] = Counter()
    for record in records:
        if (event is not None and record.event["event"] != event) or record.uid in seen:
            continue
        seen.add(record.uid)
        groups = ((type(value), value) for value in _values(record.event, path))
        counts.update(dict.fromkeys(groups, 1))
    return [(value, count) for (_, value), count in counts.items()]
