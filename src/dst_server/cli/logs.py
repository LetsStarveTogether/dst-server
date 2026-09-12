"""Query retained host logs without connecting to a game process."""

from datetime import datetime
from typing import Annotated, Literal

from cyclopts import App, Parameter

from dst_server.logs import JournalQuery, JournalRecord, JournalResult, NetdataLogQuery

from .common import context, emit, make_host, select_rooms
from .output import diagnostic

type Direction = Literal["forward", "backward"]
type AllRooms = Annotated[bool, Parameter(name="--all")]

logs_app = App(name="logs", help="Query retained journal and OpenTelemetry logs.")


def journal_request(
    *,
    lines: int,
    follow: bool,
    direction: Direction | None,
    cursor: str | None,
    since: str | None,
    until: str | None,
    namespace: str | None,
) -> JournalQuery:
    direction = direction or ("forward" if follow else "backward")
    if follow and direction != "forward":
        message = "--follow requires --direction forward"
        raise ValueError(message)
    return JournalQuery(
        limit=lines,
        direction=direction,
        cursor=cursor,
        since=since,
        until=until,
        namespace=namespace,
    )


def show_diagnostics(diagnostics: str, truncated: bool) -> None:
    if diagnostics:
        diagnostic(diagnostics)
    if truncated:
        diagnostic("Log diagnostics were truncated.")


def show_record(record: JournalRecord) -> None:
    emit(
        record
        if context().json
        else f"{record.timestamp.isoformat()} {record.unit}: {record.message}"
    )


def show_journal(result: JournalResult) -> None:
    if context().json:
        emit(result)
    else:
        for record in sorted(result.records, key=lambda record: record.timestamp):
            show_record(record)
        if result.has_more:
            diagnostic(f"More records are available; next cursor: {result.next_cursor}")
    show_diagnostics(result.diagnostics, result.diagnostics_truncated)


@logs_app.default
async def journal(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    lines: int = 100,
    follow: bool = False,
    direction: Direction | None = None,
    cursor: str | None = None,
    since: Annotated[str | None, Parameter(allow_leading_hyphen=True)] = None,
    until: Annotated[str | None, Parameter(allow_leading_hyphen=True)] = None,
    namespace: str | None = None,
) -> None:
    """Read a journal page or follow new records, including stopped rooms.

    History defaults to backward; follow defaults to forward.
    --lines selects the page size or the recent history included before follow.
    """
    request = journal_request(
        lines=lines,
        follow=follow,
        direction=direction,
        cursor=cursor,
        since=since,
        until=until,
        namespace=namespace,
    )
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)
        if follow:
            async with host.follow_journal(numbers, request, shard=shard) as stream:
                async for record in stream:
                    show_record(record)
            show_diagnostics(stream.diagnostics, stream.diagnostics_truncated)
        else:
            show_journal(await host.journal(numbers, request, shard=shard))


@logs_app.command(name="telemetry")
async def telemetry(
    *,
    since: datetime,
    until: datetime | None = None,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    shard: str | None = None,
    limit: int = 200,
    filters: Annotated[tuple[str, ...], Parameter(name="--filter")] = (),
    query: str | None = None,
    fields: Annotated[tuple[str, ...], Parameter(name="--field")] = (),
    service_name: str | None = None,
    service_namespace: str | None = None,
) -> None:
    """Query Netdata's local OTel store in a bounded time window.

    Use timezone-aware ISO timestamps and repeat --filter FIELD=VALUE as needed.
    Room and shard filters are set by --room and --shard.

    Raises:
        ValueError: A filter does not use FIELD=VALUE syntax.
    """
    parsed: list[tuple[str, str]] = []
    for value in filters:
        field, separator, match = value.partition("=")
        if not separator:
            message = "--filter requires FIELD=VALUE"
            raise ValueError(message)
        parsed.append((field, match))
    request = NetdataLogQuery(
        since=since,
        until=until,
        service_name=service_name,
        service_namespace=service_namespace,
        filters=tuple(parsed),
        query=query,
        fields=fields,
        limit=limit,
    )
    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms=all_rooms, template=template)
        result = await host.telemetry(numbers, request, shard=shard)
        emit(result)
        show_diagnostics(result.diagnostics, result.diagnostics_truncated)
