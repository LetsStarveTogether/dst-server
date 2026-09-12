# ruff: file-ignore[print] -- This subprocess emits the game's stdout protocol.
import os
import signal
import stat
import sys
from pathlib import Path
from types import FrameType

import orjson

assert all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (3, 4, 5))
arguments = sys.argv[1:]
shard = arguments[arguments.index("-shard") + 1]


def argument(name: str) -> str:
    return arguments[arguments.index(name) + 1]


directory = Path(
    argument("-persistent_storage_root")
    + "/"
    + argument("-conf_dir")
    + "/"
    + argument("-cluster")
    + "/"
    + shard
)
options = orjson.loads((directory / "dst_server_driver.json").read_bytes())
nonce, generation = options["nonce"], 0
commands = os.fdopen(3)
results = os.fdopen(4, "w", buffering=1)
events = os.fdopen(5, "w", buffering=1)
events.write("DST_SessionId|TEST\n")


def stop(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    events.write("DST_Shutdown\nDST_Saved|session/TEST/1\nDST_Stopping\n")


signal.signal(signal.SIGTERM, stop)


def driver_record(record: dict[str, object]) -> None:
    print("DST_DRIVER|" + orjson.dumps({"nonce": nonce, **record}).decode(), flush=True)


if shard != "driver-eof":
    driver_record({"generation": generation})
    if shard == "core-failure":
        driver_record({"error": "installation_failed", "generation": generation})
    else:
        failed = shard == "telemetry-failure"
        health = {
            "protocol": 2,
            "generation": generation,
            "telemetry_status": "failed" if failed else "active",
            "last_error": {
                "stage": "install",
                "message": "installation_failed",
                "count": 1,
            }
            if failed
            else None,
            "events_emitted": 0,
            "errors": 1 if failed else 0,
        }
        driver_record({"health": health})
        if not failed:
            event = {
                "v": 2,
                "nonce": nonce,
                "generation": generation,
                "session_id": "TEST",
                "seq": 1,
                "event": "dst.world.state_changed",
                "tick": 10,
                "monotonic_ms": 20,
                "cycle": 2,
                "data": {"name": "cycles", "value": 2},
            }
            print("[00:00:01]: DST_OTEL|" + orjson.dumps(event).decode(), flush=True)

busy = True
for command in commands:
    assert "driver.install" not in command, "unexpected console injection"
    if busy:
        busy = False
        results.write("DST_LuaBusy\n")
        continue
    request = orjson.loads(command.removeprefix("DST_RPC|"))
    header = {key: request[key] for key in ("v", "nonce", "id", "generation")}
    results.write(
        "DST_RPC|" + orjson.dumps(header | {"accepted": True}).decode() + "\n"
    )
    method = request["method"]
    if method == "list_players":
        data = []
    elif method == "save":
        data = {"snapshot": "session/TEST/0000000001"}
    else:
        print("command received", flush=True)
        event = {
            "v": 2,
            "nonce": nonce,
            "generation": generation,
            "session_id": "TEST",
            "seq": 2,
            "event": "dst.entity.death",
            "tick": 11,
            "monotonic_ms": 21,
            "cycle": 2,
            "data": {
                "victim": {
                    "prefab": "rabbit",
                    "guid": 42,
                    "userid": None,
                    "position": None,
                },
                "cause": None,
                "afflicter": None,
                "attributed_player": None,
                "corpsing": False,
                "caused_by_action_sequence": None,
            },
        }
        print("DST_OTEL|" + orjson.dumps(event).decode(), flush=True)
        data = {
            "output": "result:" + request["arguments"]["source"],
            "values": [],
            "error": None,
            "truncated": False,
        }
    results.write(
        "DST_RPC|"
        + orjson.dumps(header | {"result": {"ok": True, "data": data}}).decode()
        + "\n"
    )
    results.write("DST_RemoteCommandDone\n")
