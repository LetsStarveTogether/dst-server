import importlib
import sqlite3
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from types import ModuleType

import pytest


@pytest.fixture
def delivery() -> ModuleType:
    return importlib.import_module("dst_server.telemetry.delivery")


def test_outbox_reopens_exact_committed_wire_bytes(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    outbox = delivery.Outbox(path)
    payloads = (b"\x00first\xff", b"second\x00")
    identities = tuple(outbox.append(payload) for payload in payloads)
    outbox.close()

    reopened = delivery.Outbox(path)
    try:
        records = reopened.read_batch()
        assert tuple(record.id for record in records) == identities
        assert tuple(record.payload for record in records) == payloads
        assert reopened.stats().pending == 2
    finally:
        reopened.close()


def test_committed_events_survive_process_exit_without_cleanup(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    source = """
import os
import sys
from pathlib import Path
from types import ModuleType
from dst_server.telemetry.delivery import Outbox
outbox = Outbox(Path(sys.argv[1]))
outbox.append(b"committed-before-crash")
os._exit(0)
"""
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", source, str(path)], check=True, timeout=10
    )
    outbox = delivery.Outbox(path)
    try:
        assert [row.payload for row in outbox.read_batch()] == [
            b"committed-before-crash"
        ]
    finally:
        outbox.close()


def test_acknowledgement_only_removes_the_confirmed_batch(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3")
    try:
        first = outbox.append(b"first")
        second = outbox.append(b"second")
        assert [row.id for row in outbox.read_batch(limit=1)] == [first]
        third = outbox.append(b"third-during-export")
        outbox.acknowledge((first,))
        outbox.acknowledge((first,))
        assert [row.id for row in outbox.read_batch()] == [second, third]
    finally:
        outbox.close()


def test_unacknowledged_export_is_replayed_after_reopen(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    outbox = delivery.Outbox(path)
    identity = outbox.append(b"server-may-have-accepted-this")
    assert outbox.read_batch()[0].id == identity
    outbox.close()
    reopened = delivery.Outbox(path)
    try:
        assert reopened.read_batch()[0].id == identity
        reopened.acknowledge((identity,))
    finally:
        reopened.close()
    reopened = delivery.Outbox(path)
    try:
        assert reopened.read_batch() == ()
    finally:
        reopened.close()


def test_quarantine_survives_restart_and_does_not_block_new_records(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    outbox = delivery.Outbox(path)
    rejected = outbox.append(b"partially-rejected-batch")
    valid = outbox.append(b"valid")
    outbox.quarantine((rejected,), "receiver rejected 1 record", rejected=1)
    outbox.close()
    reopened = delivery.Outbox(path)
    try:
        assert [row.id for row in reopened.read_batch()] == [valid]
        status = reopened.stats()
        assert status.pending == 1
        assert status.quarantined == 1
        assert status.bytes == len(b"partially-rejected-batchvalid")
        assert "receiver rejected 1 record" in status.last_error
    finally:
        reopened.close()


def test_capacity_never_evicts_unacknowledged_records(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3", max_bytes=8)
    try:
        identity = outbox.append(b"12345678")
        with pytest.raises(delivery.OutboxFull):
            outbox.append(b"9")
        assert [row.payload for row in outbox.read_batch()] == [b"12345678"]
        assert outbox.stats().bytes == 8
        outbox.acknowledge((identity,))
        outbox.append(b"12345678")
        assert outbox.stats().pending == 1
    finally:
        outbox.close()


def test_quarantine_counts_against_capacity(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3", max_bytes=8)
    try:
        identity = outbox.append(b"12345678")
        outbox.quarantine((identity,), "bad payload")
        with pytest.raises(delivery.OutboxFull):
            outbox.append(b"9")
        assert outbox.stats().quarantined == 1
    finally:
        outbox.close()


def test_last_quarantine_reason_survives_out_of_order_rejections(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    outbox = delivery.Outbox(path)
    first = outbox.append(b"first")
    second = outbox.append(b"second")
    outbox.quarantine((second,), "second rejected")
    outbox.quarantine((first,), "first rejected later")
    expected = outbox.stats()
    outbox.close()
    outbox = delivery.Outbox(path)
    try:
        assert outbox.stats() == expected
    finally:
        outbox.close()


def test_capacity_check_and_insert_are_atomic_across_threads(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3", max_bytes=16)

    def append(_: int) -> bool:
        try:
            outbox.append(b"1234")
        except delivery.OutboxFull:
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            accepted = list(workers.map(append, range(64)))
        assert sum(accepted) == 4
        assert outbox.stats().bytes == 16
        assert len({row.id for row in outbox.read_batch()}) == 4
    finally:
        outbox.close()


def test_outbox_has_one_exporting_owner(delivery: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    outbox = delivery.Outbox(path)
    try:
        with pytest.raises(BlockingIOError):
            delivery.Outbox(path)
        outbox.append(b"original-owner-still-works")
    finally:
        outbox.close()
    delivery.Outbox(path).close()


def test_corrupt_storage_is_never_replaced(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    original = b"corrupt data retained for recovery"
    path.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        delivery.Outbox(path)
    assert path.read_bytes() == original


def test_unknown_storage_schema_is_never_replaced(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE retained(value TEXT)")
        database.execute("INSERT INTO retained VALUES ('keep this data')")
        database.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="schema"):
        delivery.Outbox(path)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT value FROM retained").fetchone() == (
            "keep this data",
        )


def test_unversioned_nonempty_storage_is_never_adopted(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE retained(value TEXT)")
        database.execute("INSERT INTO retained VALUES ('keep this data')")
    with pytest.raises(RuntimeError, match="schema"):
        delivery.Outbox(path)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT value FROM retained").fetchone() == (
            "keep this data",
        )


def test_initialization_failure_releases_owner_lock(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    path.write_bytes(b"damaged database")
    for _ in range(2):
        with pytest.raises(sqlite3.DatabaseError):
            delivery.Outbox(path)


def test_old_acknowledgement_cannot_remove_new_events_after_queue_drains(
    delivery: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "events.sqlite3"
    outbox = delivery.Outbox(path)
    previous = outbox.append(b"previous")
    outbox.acknowledge((previous,))
    outbox.close()
    outbox = delivery.Outbox(path)
    try:
        current = outbox.append(b"current")
        assert current != previous
        outbox.acknowledge((previous,))
        assert [row.id for row in outbox.read_batch()] == [current]
    finally:
        outbox.close()


def test_status_reads_committed_snapshot_without_waiting_for_commit(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3")
    committing = Event()
    release = Event()

    def trace(statement: str) -> None:
        if statement == "COMMIT":
            committing.set()
            release.wait(3)

    outbox._database.set_trace_callback(trace)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            append = workers.submit(outbox.append, b"awaiting-commit")
            try:
                assert committing.wait(3)
                status = workers.submit(outbox.stats).result(timeout=0.5)
                assert status.pending == 0
                assert status.bytes == 0
            finally:
                release.set()
            append.result(timeout=3)
        assert outbox.stats().pending == 1
    finally:
        outbox.close()


def test_failed_commit_rolls_back_without_publishing_acceptance(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3")

    def authorize(action: int, argument: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        outbox._database.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            outbox.append(b"not-committed")
        outbox._database.set_authorizer(None)
        assert outbox.stats().pending == 0
        assert outbox.stats().bytes == 0
        assert outbox.read_batch() == ()
    finally:
        outbox._database.set_authorizer(None)
        outbox.close()


def test_close_is_idempotent_and_rejects_new_events(
    delivery: ModuleType, tmp_path: Path
) -> None:
    outbox = delivery.Outbox(tmp_path / "events.sqlite3")
    outbox.close()
    outbox.close()
    with pytest.raises(RuntimeError, match="closed"):
        outbox.append(b"late-event")


@pytest.mark.parametrize("limit", [0, -1, True])
def test_storage_rejects_invalid_capacity(
    delivery: ModuleType, tmp_path: Path, limit: int
) -> None:
    with pytest.raises(ValueError, match="positive"):
        delivery.Outbox(tmp_path / "events.sqlite3", max_bytes=limit)
