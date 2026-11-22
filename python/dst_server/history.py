"""Read a bounded window of managed room history from the local journal."""

from collections import Counter
from collections.abc import AsyncIterator, Iterable, Iterator, MutableSet, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NamedTuple, Self

from ._native import (
    decode_room_message,
    event_capture,
    event_userid_fields,
    normalize_event_filter,
)
from .events import GameEvent
from .host import Host
from .logs import JournalRecord, _utc

__all__ = [
    "CoverageChange",
    "EventCapture",
    "EventFilter",
    "HistoryCheckpoint",
    "HistoryPage",
    "HistoryReader",
    "HistoryRecord",
    "HistoryRejection",
    "HistorySelection",
    "ShardRun",
    "count_by",
    "participants",
    "read_history",
    "replay_by_run",
]

type Scalar = str | int | float | bool | None


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
    for role in event_userid_fields(event["event"]):
        for value in _values(event["data"], role.split(".")):
            if isinstance(value, str):
                yield role, value


@dataclass(frozen=True, slots=True)
class EventFilter:
    """Match exact event names, shard names and a participant in any recorded role."""

    events: tuple[str, ...] = ()
    shards: tuple[str, ...] = ()
    userid: str | None = None

    def __post_init__(self) -> None:
        values = normalize_event_filter(asdict(self))
        object.__setattr__(self, "events", tuple(values["events"]))
        object.__setattr__(self, "shards", tuple(values["shards"]))


@dataclass(frozen=True, slots=True)
class HistoryCheckpoint:
    """Persist with the UID-based results of a page and return to the same reader."""

    scope: str
    cursor: str | None


@dataclass(frozen=True, slots=True)
class HistorySelection:
    rooms: tuple[int, ...]
    since: datetime
    until: datetime
    namespace: str | None = None
    coverage_only: bool = False
    where: EventFilter = field(default_factory=EventFilter)

    def to_native(self) -> dict[str, Any]:
        value = asdict(self)
        value["since"] = _utc(self.since).isoformat()
        value["until"] = _utc(self.until).isoformat()
        return value


@dataclass(frozen=True, slots=True)
class _Observation:
    uid: str
    unit: str
    cluster: str
    shard: str
    observed_timestamp_ns: int
    resource: dict[str, Any] = field(default_factory=dict, kw_only=True)
    attributes: dict[str, Any] = field(default_factory=dict, kw_only=True)
    severity_text: str | None = field(default=None, kw_only=True)
    trace: dict[str, Any] | None = field(default=None, kw_only=True)
    receipt_time: datetime | None = field(default=None, kw_only=True)
    cursor: str | None = field(default=None, kw_only=True)


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
        unit = entry.emitting_unit(user=user)
        if unit is None:
            return None
        uid = entry.fields.get("_UID")
        raw = decode_room_message(
            entry.fields.get("MESSAGE"),
            unit,
            user,
            uid if isinstance(uid, str) else None,
        )
        if raw is None:
            return None
        observation = _observation(raw, unit, entry.timestamp, entry.cursor)
        return observation if isinstance(observation, HistoryRecord) else None


@dataclass(frozen=True, slots=True)
class EventCapture:
    """What one coverage sample says about the selected event's producer hooks."""

    profile: str
    profile_includes_event: bool
    telemetry_status: str
    capabilities: dict[str, str]


@dataclass(frozen=True, slots=True)
class CoverageChange(_Observation):
    """Capture settings and observer health reported by a shard's Agent."""

    nonce: str
    generation: int
    data: dict[str, Any]
    agent_instance_id: str

    def capture_for(self, name: str) -> EventCapture:
        """Read the installed hooks at this sample's time in this shard run."""
        minimum, hooks = event_capture(name)
        profile = self.data["profile"]
        return EventCapture(
            profile,
            profile == "play" or (profile == "timeline" and minimum == "timeline"),
            self.data["telemetry_status"],
            {hook: self.data["capabilities"].get(hook, "unknown") for hook in hooks},
        )

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
class HistoryRejection:
    unit: str
    cursor: str
    receipt_time: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class HistoryReader:
    """Host and credentials that selected the journal files for this page."""

    host_id: str
    host_id_kind: Literal["machine_id", "boot_id"]
    effective_uid: int
    effective_gid: int
    groups: tuple[int, ...]
    executable: str


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """Observations retain native order, including interleaved coverage changes."""

    observations: tuple[HistoryRecord | CoverageChange, ...]
    rejections: tuple[HistoryRejection, ...]
    visible_units: tuple[str, ...]
    reader: HistoryReader
    selection: HistorySelection
    user: bool
    candidates_read: int
    checkpoint: HistoryCheckpoint
    has_more: bool
    diagnostics: str
    diagnostics_truncated: bool

    @classmethod
    def from_native(cls, raw: dict[str, Any]) -> Self:
        raw["observations"] = tuple(
            _observation(row, row.pop("unit"), _receipt(row), row.pop("cursor"))
            for row in raw["observations"]
        )
        raw["rejections"] = tuple(
            HistoryRejection(receipt_time=_receipt(row), **row)
            for row in raw["rejections"]
        )
        raw["visible_units"] = tuple(raw["visible_units"])
        reader = raw["reader"]
        raw["reader"] = HistoryReader(**(reader | {"groups": tuple(reader["groups"])}))
        raw["checkpoint"] = HistoryCheckpoint(**raw["checkpoint"])
        raw.pop("next_cursor")
        selection = raw["selection"]
        selection["since"] = datetime.fromisoformat(selection["since"])
        selection["until"] = datetime.fromisoformat(selection["until"])
        selection["rooms"] = tuple(selection["rooms"])
        selection["where"] = EventFilter(**selection["where"])
        raw["user"] = selection.pop("user")
        raw["selection"] = HistorySelection(**selection)
        return cls(**raw)

    @property
    def next_cursor(self) -> str | None:
        return self.checkpoint.cursor

    @property
    def records(self) -> tuple[HistoryRecord, ...]:
        return tuple(row for row in self.observations if isinstance(row, HistoryRecord))

    @property
    def coverage(self) -> tuple[CoverageChange, ...]:
        return tuple(
            row for row in self.observations if isinstance(row, CoverageChange)
        )

    @property
    def rejected(self) -> int:
        return len(self.rejections)


def _observation(
    raw: dict[str, Any],
    unit: str,
    receipt: datetime | None = None,
    cursor: str | None = None,
) -> HistoryRecord | CoverageChange:
    kind = raw.pop("kind")
    cls = CoverageChange if kind == "coverage" else HistoryRecord
    return cls(unit=unit, receipt_time=receipt, cursor=cursor, **raw)


def _receipt(raw: dict[str, Any]) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=raw.pop("receipt_timestamp_us")
    )


async def read_history(
    host: Host,
    numbers: Sequence[int],
    *,
    since: datetime,
    until: datetime,
    cursor: str | None = None,
    checkpoint: HistoryCheckpoint | None = None,
    page_size: int = 500,
    coverage_only: bool = False,
    where: EventFilter | None = None,
    namespace: str | None = None,
) -> AsyncIterator[HistoryPage]:
    """Scan a half-open receipt window in native journal order.

    Supply a saved checkpoint to validate the source and selection before resuming.
    For a durable aggregate, commit the checkpoint and distinct UIDs together.
    Yields:
        Pages with ordered observations, rejections and resumable progress.
    """
    selection = HistorySelection(
        tuple(numbers), since, until, namespace, coverage_only, where or EventFilter()
    )
    while True:
        page = await host.history_page(
            selection, page_size=page_size, cursor=cursor, checkpoint=checkpoint
        )
        yield page
        if not page.has_more:
            return
        cursor, checkpoint = None, page.checkpoint


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
