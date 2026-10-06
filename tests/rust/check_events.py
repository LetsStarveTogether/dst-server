"""Validate actual Lua producer output through the native event contract."""

# The Lua programs and scripts directory are trusted repository test fixtures.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print]
import json
import shutil
import subprocess
from pathlib import Path

from dst_server import DstError
from dst_server.events import event_names, validate_event

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    names = set(event_names())
    count = 0
    for name in ("lua5.1", "luajit"):
        runtime = shutil.which(name)
        assert runtime, f"{name} is required to validate live event producers"
        result = subprocess.run(
            [
                runtime,
                str(ROOT / "tests/lua/event_contract.lua"),
                str(ROOT),
                str(ROOT / "dst-scripts/scripts"),
                "play",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        emitted = set()
        for line in result.stdout.splitlines():
            record = json.loads(line.removeprefix(b"DST_OTEL|"))
            validated = validate_event(record)
            assert validated == record
            assert validated["data"] is not record["data"]
            emitted.add(record["event"])
            count += 1
        assert emitted == names, (names - emitted, emitted - names)

    event = {
        "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": 1,
        "session_id": None,
        "seq": 1,
        "event": "dst.world.state_changed",
        "tick": 0,
        "monotonic_ms": 0,
        "cycle": None,
        "data": {"name": "cycles", "value": 2**53 - 1},
    }
    assert validate_event(event) == event
    projected_chat = {
        "userid": "KU_PLAYER",
        "name": None,
        "prefab": None,
        "message": "SECRET_TOKEN",
        "emote": False,
    }
    for changes in (
        {"event": "dst.player.chat", "data": projected_chat},
        {"generation": True},
        {"tick": 0.0},
        {"data": {"name": "cycles", "value": 2**53}},
        {"data": {"name": "season", "value": "SECRET_TOKEN", "extra": True}},
        {"data": {"name": "season", "value": 5}},
        {"data": {"name": "cycles", "value": 1.0}},
    ):
        try:
            validate_event(event | changes)
        except DstError as error:
            code, message = error.code, str(error)
        else:
            raise AssertionError(changes)
        assert code == "invalid"
        assert "SECRET_TOKEN" not in message
    print(f"Validated {count} live Lua events and native rejection boundaries")


if __name__ == "__main__":
    main()
