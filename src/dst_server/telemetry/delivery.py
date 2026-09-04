import fcntl
import os
import sqlite3
from dataclasses import dataclass, replace
from itertools import starmap
from pathlib import Path
from threading import Lock

_SCHEMA_VERSION = 1
_TABLE = """CREATE TABLE records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload BLOB NOT NULL CHECK (length(payload) > 0),
    reason TEXT,
    rejected INTEGER CHECK (rejected IS NULL OR rejected >= 0),
    quarantine_order INTEGER NOT NULL DEFAULT 0 CHECK (quarantine_order >= 0)
) STRICT"""
_INDEX = "CREATE INDEX pending ON records(id) WHERE reason IS NULL"


@dataclass(frozen=True, slots=True)
class PendingLog:
    id: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class DeliveryStatus:
    pending: int = 0
    quarantined: int = 0
    bytes: int = 0
    last_error: str | None = None


class OutboxFull(RuntimeError):  # ruff: ignore[error-suffix-on-exception-name]
    pass


class Outbox:
    def __init__(self, path: Path, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            message = "outbox capacity must be a positive integer"
            raise ValueError(message)
        self._max_bytes = max_bytes
        self._lock = Lock()
        self._database: sqlite3.Connection | None = None
        self._owner: int | None = None
        self._status = DeliveryStatus()
        self._quarantine_order = 0
        path = path.resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            # Lock the database inode itself; aliases must not create two owners.
            self._owner = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._database = sqlite3.connect(
                path,
                isolation_level="IMMEDIATE",
                check_same_thread=False,
                # sqlite3 explicitly supports this mode; typeshed only lists bool.
                autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,  # ty: ignore[invalid-argument-type]
            )
            self._initialize(self._database)
        except BaseException:
            self.close()
            raise

    def _initialize(self, database: sqlite3.Connection) -> None:
        version = database.execute("PRAGMA user_version").fetchone()[0]
        schema = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        new = version == 0 and not schema
        if not new and (
            version != _SCHEMA_VERSION
            or schema != {"records": _TABLE, "pending": _INDEX}
        ):
            message = "unrecognized outbox schema; database retained for recovery"
            raise RuntimeError(message)
        if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            message = "outbox integrity check failed; database retained for recovery"
            raise sqlite3.DatabaseError(message)
        database.execute("PRAGMA journal_mode = WAL")
        database.execute("PRAGMA synchronous = FULL")
        # Payload budget excludes SQLite pages. Checkpoint regularly so WAL space
        # is reused, including while the receiver is unavailable.
        database.execute("PRAGMA wal_autocheckpoint = 256")
        database.execute("PRAGMA journal_size_limit = 1048576")
        if new:
            with database:
                database.execute("BEGIN IMMEDIATE")
                database.execute(_TABLE)
                database.execute(_INDEX)
                database.execute("PRAGMA user_version = 1")
        pending, quarantined, size, self._quarantine_order = database.execute(
            "SELECT count(*) FILTER (WHERE reason IS NULL), "
            "count(*) FILTER (WHERE reason IS NOT NULL), "
            "coalesce(sum(length(payload)), 0), "
            "coalesce(max(quarantine_order), 0) FROM records"
        ).fetchone()
        last = database.execute(
            "SELECT reason FROM records WHERE reason IS NOT NULL "
            "ORDER BY quarantine_order DESC LIMIT 1"
        ).fetchone()
        self._status = DeliveryStatus(
            pending, quarantined, size, last[0] if last else None
        )

    def _open_database(self) -> sqlite3.Connection:
        if self._database is None:
            message = "outbox is closed"
            raise RuntimeError(message)
        return self._database

    def append(self, payload: bytes) -> int:
        if not isinstance(payload, bytes) or not payload:
            message = "outbox payload must be nonempty bytes"
            raise ValueError(message)
        with self._lock:
            database = self._open_database()
            status = self._status
            if status.bytes + len(payload) > self._max_bytes:
                message = "outbox payload capacity exhausted"
                raise OutboxFull(message)
            with database:
                identity = database.execute(
                    "INSERT INTO records(payload) VALUES (?) RETURNING id", (payload,)
                ).fetchone()[0]
            self._status = replace(
                status, pending=status.pending + 1, bytes=status.bytes + len(payload)
            )
            return identity

    def read_batch(self, limit: int = 128) -> tuple[PendingLog, ...]:
        if type(limit) is not int or limit <= 0:
            message = "outbox batch limit must be a positive integer"
            raise ValueError(message)
        with self._lock:
            rows = (
                self
                ._open_database()
                .execute(
                    "SELECT id, payload FROM records WHERE reason IS NULL "
                    "ORDER BY id LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        return tuple(starmap(PendingLog, rows))

    def acknowledge(self, identities: tuple[int, ...]) -> None:
        with self._lock:
            database = self._open_database()
            with database:
                removed = database.execute(
                    "DELETE FROM records WHERE reason IS NULL AND id IN "  # ruff: ignore[hardcoded-sql-expression]
                    f"({','.join('?' for _ in identities)}) RETURNING length(payload)",
                    identities,
                ).fetchall()
            self._status = replace(
                self._status,
                pending=self._status.pending - len(removed),
                bytes=self._status.bytes - sum(row[0] for row in removed),
            )

    def quarantine(
        self,
        identities: tuple[int, ...],
        reason: str,
        *,
        rejected: int | None = None,
    ) -> None:
        if not isinstance(reason, str) or not reason:
            message = "quarantine reason must be a nonempty string"
            raise ValueError(message)
        if rejected is not None and (type(rejected) is not int or rejected < 0):
            message = "rejected record count must be a nonnegative integer"
            raise ValueError(message)
        with self._lock:
            database = self._open_database()
            with database:
                changed = database.execute(
                    "UPDATE records SET reason = ?, rejected = ?, "  # ruff: ignore[hardcoded-sql-expression]
                    "quarantine_order = ? "
                    "WHERE reason IS NULL AND id IN "
                    f"({','.join('?' for _ in identities)}) RETURNING id",
                    (reason, rejected, self._quarantine_order + 1, *identities),
                ).fetchall()
            if changed:
                self._quarantine_order += 1
                self._status = replace(
                    self._status,
                    pending=self._status.pending - len(changed),
                    quarantined=self._status.quarantined + len(changed),
                    last_error=reason,
                )

    def stats(self) -> DeliveryStatus:
        # Publishing an immutable snapshot after COMMIT keeps health checks off
        # the writer lock, including during a slow fsync or checkpoint.
        return self._status

    def close(self) -> None:
        with self._lock:
            database, self._database = self._database, None
            try:
                if database is not None:
                    database.close()
            finally:
                if self._owner is not None:
                    os.close(self._owner)
                    self._owner = None
