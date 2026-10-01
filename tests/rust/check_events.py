"""Validate actual Lua producer output through the native event contract."""

# The Lua programs and scripts directory are trusted repository test fixtures.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print]
import json
import shutil
import subprocess
from pathlib import Path

from dst_server import DstError
from dst_server.events import event_schema, validate_event

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    schema = event_schema()
    assert len(schema["discriminator"]["mapping"]) == 54
    schema["discriminator"]["mapping"].clear()
    assert len(event_schema()["discriminator"]["mapping"]) == 54

    count = 0
    for name in ("lua5.1", "luajit"):
        emitted = set()
        runtime = shutil.which(name)
        assert runtime, f"{name} is required to validate live event producers"
        scripts = ROOT / "dst-scripts/scripts"
        fixtures = [
            ("event_contract.lua", ROOT / "resources/lua", ROOT / "tests/lua", scripts),
            ("input_events_spec.lua", ROOT, scripts, "critical"),
            ("gameplay_events_spec.lua", ROOT, scripts, "critical"),
            ("mode_votes_spec.lua", ROOT, scripts, "forge"),
        ]
        for fixture, *arguments in fixtures:
            result = subprocess.run(
                [runtime, str(ROOT / "tests/lua" / fixture), *map(str, arguments)],
                capture_output=True,
                check=True,
                timeout=30,
            )
            records = [
                json.loads(line.removeprefix(b"DST_OTEL|"))
                for line in result.stdout.splitlines()
            ]
            assert records, fixture
            for record in records:
                validated = validate_event(record)
                assert validated == record
                assert validated is not record
                assert validated["data"] is not record["data"]
                emitted.add(validated["event"])
                count += 1
        assert emitted == set(event_schema()["discriminator"]["mapping"])

    event = {
        "v": 3,
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
    for changes in (
        {"event": "dst.unknown"},
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
