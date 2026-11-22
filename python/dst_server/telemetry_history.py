"""Structured Netdata searches over the Agent instances attributed to a room."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self

from .history import CoverageChange, EventFilter, HistoryRecord
from .logs import NetdataLogRecord


@dataclass(frozen=True, slots=True)
class RoomStream:
    """One historical Agent instance attributed to a room."""

    room: int
    service_name: str
    service_namespace: str
    cluster: str
    instance_id: str
    tenant: str | None = None

    @classmethod
    def from_journal(
        cls, record: HistoryRecord | CoverageChange, *, tenant: str | None = None
    ) -> Self:
        """Bind a stream using a journal observation with the producer resource."""
        resource = record.resource
        instance = resource.get("service.instance.id")
        service = resource.get("service.name")
        namespace = resource.get("service.namespace", "")
        if record.cursor is None or resource.get("dst.cluster.name") != record.cluster:
            message = (
                "room attribution requires a journal record with its cluster resource"
            )
            raise ValueError(message)
        if (
            not isinstance(instance, str)
            or not instance.strip()
            or not isinstance(service, str)
            or not service
            or not isinstance(namespace, str)
        ):
            message = "journal entry does not establish a room stream"
            raise ValueError(message)
        return cls(
            int(record.unit[4:7]), service, namespace, record.cluster, instance, tenant
        )


@dataclass(frozen=True, slots=True)
class TelemetryHit(NetdataLogRecord):
    uid: str
    event_name: str
    shard: str


@dataclass(frozen=True, slots=True)
class TelemetryPage:
    hits: tuple[TelemetryHit, ...]
    matched_candidates: int | None
    returned_candidates: int
    distinct_uids: int
    unverified: int
    truncated: bool | None
    error: str | None
    file_status: Literal[
        "unknown", "diagnostics", "no_files", "files_found", "observed"
    ]
    diagnostics: str
    diagnostics_truncated: bool


@dataclass(frozen=True, slots=True)
class TelemetryHistory:
    source: RoomStream
    where: EventFilter
    since: datetime
    until: datetime
    native_since: datetime
    native_until: datetime
    fields: tuple[str, ...]
    limit: int
    gameplay: TelemetryPage
    coverage: TelemetryPage

    @classmethod
    def from_native(cls, raw: dict[str, Any]) -> Self:
        def page(rows: dict[str, Any]) -> TelemetryPage:
            hits = rows.pop("hits")
            for hit in hits:
                hit["fields"] = tuple(tuple(pair) for pair in hit["fields"])
            return TelemetryPage(
                hits=tuple(TelemetryHit(**hit) for hit in hits), **rows
            )

        selected = raw["selection"]
        return cls(
            RoomStream(**selected["source"]),
            EventFilter(**selected["where"]),
            datetime.fromisoformat(selected["since"]),
            datetime.fromisoformat(selected["until"]),
            datetime.fromtimestamp(raw["since"], UTC),
            datetime.fromtimestamp(raw["until"], UTC),
            tuple(selected["fields"]),
            selected["limit"],
            page(raw["gameplay"]),
            page(raw["coverage"]),
        )
