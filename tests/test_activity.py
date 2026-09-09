from datetime import UTC, datetime
from pathlib import Path

import pytest

from dst_server.activity import read_last_login, write_last_login


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b" \t\n",
        b"invalid timestamp",
        b"\xff",
        b"2026-09-10T08:00:00",
        b"0001-01-01T00:00:00+01:00",
        b"9999-12-31T23:59:59-01:00",
    ],
)
def test_unusable_login_records_can_be_replaced(
    tmp_path: Path, content: bytes | None
) -> None:
    path = tmp_path / "save/session/WORLD/.last_login"
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    assert read_last_login(tmp_path, "WORLD") is None

    timestamp = datetime(2026, 9, 10, 8, tzinfo=UTC)
    write_last_login(tmp_path, "WORLD", timestamp)
    assert read_last_login(tmp_path, "WORLD") == timestamp
    assert path.read_text(encoding="utf-8") == "2026-09-10T08:00:00+00:00\n"


@pytest.mark.parametrize("text", ["2026-09-10T08:00:00Z", "2026-09-10T16:00:00+08:00"])
def test_login_times_are_utc_and_belong_to_one_world(tmp_path: Path, text: str) -> None:
    path = tmp_path / "save/session/OLD/.last_login"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    expected = datetime(2026, 9, 10, 8, tzinfo=UTC)
    loaded = read_last_login(tmp_path, "OLD")
    assert loaded == expected
    assert loaded is not None
    assert loaded.tzinfo is UTC

    write_last_login(tmp_path, "OLD", datetime.fromisoformat(text))
    assert path.read_text(encoding="utf-8") == "2026-09-10T08:00:00+00:00\n"
    assert read_last_login(tmp_path, "OLD") == expected
    assert read_last_login(tmp_path, "NEW") is None
    assert read_last_login(tmp_path, None) is None


@pytest.mark.parametrize("session_id", ["../escape", "/escape", "nested/world"])
def test_login_records_reject_path_escape(tmp_path: Path, session_id: str) -> None:
    assert read_last_login(tmp_path, session_id) is None
    with pytest.raises(ValueError, match="invalid world session ID"):
        write_last_login(tmp_path, session_id, datetime(2026, 9, 10, tzinfo=UTC))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("parent", ["save", "save/session/WORLD"])
def test_login_records_reject_symlink_parents(tmp_path: Path, parent: str) -> None:
    shard = tmp_path / "forest"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = shard / parent
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    assert read_last_login(shard, "WORLD") is None
    with pytest.raises(ValueError, match="directories cannot be symlinks"):
        write_last_login(shard, "WORLD", datetime(2026, 9, 10, tzinfo=UTC))
    assert list(outside.iterdir()) == []
