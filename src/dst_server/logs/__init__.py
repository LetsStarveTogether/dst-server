"""Local journal and OpenTelemetry log queries with native source semantics."""

from ._process import LogProcessError
from .journal import (
    JournalCursorError,
    JournalLogs,
    JournalQuery,
    JournalRecord,
    JournalResult,
    JournalStream,
)
from .netdata import (
    NetdataLogFilter,
    NetdataLogQuery,
    NetdataLogRecord,
    NetdataLogResult,
    NetdataLogs,
)

__all__ = [
    "JournalCursorError",
    "JournalLogs",
    "JournalQuery",
    "JournalRecord",
    "JournalResult",
    "JournalStream",
    "LogProcessError",
    "NetdataLogFilter",
    "NetdataLogQuery",
    "NetdataLogRecord",
    "NetdataLogResult",
    "NetdataLogs",
]
