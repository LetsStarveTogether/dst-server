import re
from datetime import UTC, datetime
from pathlib import Path

from dst_server.configuration.files import atomic_write


def _last_login_path(shard: Path, session_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9_-]+", session_id) is None:
        msg = "invalid world session ID"
        raise ValueError(msg)
    directory = shard
    for name in ("save", "session", session_id):
        if directory.is_symlink():
            msg = "login record directories cannot be symlinks"
            raise ValueError(msg)
        directory /= name
    if directory.is_symlink():
        msg = "login record directories cannot be symlinks"
        raise ValueError(msg)
    return directory / ".last_login"


def read_last_login(shard: Path, session_id: str | None) -> datetime | None:
    if session_id is None:
        return None
    try:
        path = _last_login_path(shard, session_id)
        if path.is_symlink() or not path.is_file():
            return None
        value = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        return value.astimezone(UTC) if value.tzinfo is not None else None
    except OSError, ValueError, OverflowError:
        return None


def write_last_login(shard: Path, session_id: str, timestamp: datetime) -> None:
    if timestamp.tzinfo is None:
        msg = "login timestamp must include a timezone"
        raise ValueError(msg)
    path = _last_login_path(shard, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, f"{timestamp.astimezone(UTC).isoformat()}\n", 0o600)
