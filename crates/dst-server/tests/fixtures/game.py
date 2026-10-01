#!/usr/bin/env python3
# The Rust harness makes an executable copy of this stateful process fixture.
# ruff: file-ignore[global-statement, literal-membership]

import configparser
import json
import os
import pathlib
import resource
import select
import signal
import sys
import time

PLAYER_GUID = 17
args = sys.argv[1:]


def arg(name: str) -> str:
    return args[args.index(name) + 1]


root = pathlib.Path(arg("-persistent_storage_root")) / arg("-cluster")
name = arg("-shard")
options = json.loads((root / name / "dst_server_driver.json").read_text())
nonce = options["nonce"]
snapshot, generation, save_id = 5, 1, 0
if (root / name / "initial-snapshot").exists():
    snapshot = int((root / name / "initial-snapshot").read_text())
session = "S_" + name
seen_save = 0
seen_reload = 0
pending_migration = None
shards = {}
for directory in root.iterdir():
    if (directory / "server.ini").is_file():
        settings = configparser.ConfigParser()
        settings.read(directory / "server.ini")
        shards[directory.name] = settings["SHARD"]["id"]


def emit(fd: int, prefix: bytes, record: dict[str, object]) -> None:
    os.write(fd, prefix + json.dumps(record, separators=(",", ":")).encode() + b"\n")


def migration() -> dict[str, str] | None:
    try:
        return json.loads((root / "migration.json").read_text())
    except FileNotFoundError:
        return None


def control(shutdown: bool = False, failed: bool = False) -> None:
    global save_id, snapshot
    save_id += 1
    emit(
        4,
        b"DST_CONTROL|",
        {
            "v": 1,
            "nonce": nonce,
            "generation": generation,
            "event": "save_unconfirmed" if failed else "save_complete",
            "session_id": session,
            "snapshot_id": snapshot,
            "save_id": save_id,
            "shutdown": shutdown,
        },
    )
    snapshot += 1


def bootstrap() -> None:
    os.write(5, ("DST_SessionId|" + session + "\nDST_Master_Ready\n").encode())
    emit(1, b"DST_DRIVER|", {"nonce": nonce, "generation": generation})
    emit(
        1,
        b"DST_DRIVER|",
        {
            "nonce": nonce,
            "health": {
                "generation": generation,
                "protocol": 3,
                "telemetry_status": "active",
                "capabilities": {"players": "active"},
            },
        },
    )


bootstrap()
while True:
    if pending_migration is not None and time.monotonic() >= pending_migration[1]:
        (root / "player-location.new").write_text(pending_migration[0])
        (root / "player-location.new").replace(root / "player-location")
        (root / "migration.json").unlink()
        pending_migration = None
    reload_marker = root / "reload-request"
    if reload_marker.exists():
        reload = json.loads(reload_marker.read_text())
        if reload["id"] > seen_reload:
            seen_reload = reload["id"]
            generation += 1
            if reload["regenerate"]:
                session += "_NEW"
                snapshot = 1
            else:
                snapshot = reload["snapshot_id"] + 1
            bootstrap()
    marker = root / "save-request"
    if marker.exists():
        save = json.loads(marker.read_text())
        if save["id"] > seen_save:
            seen_save = save["id"]
            snapshot = save["snapshot_id"]
            control(failed=name == "Caves" and (root / "fail-save").exists())
    if not select.select([3], [], [], 0.01)[0]:
        continue
    encoded = os.read(3, 4096)
    if not encoded:
        break
    if encoded == b"c_shutdown()\n":
        if (root / "hang-shutdown").exists():
            time.sleep(5)
        time.sleep(0.08)
        control(shutdown=True)
        os.write(5, b"DST_Stopping\nDST_Shutdown\n")
        shutdown_mode = root / name / "shutdown-mode"
        if shutdown_mode.exists():
            mode = shutdown_mode.read_text()
            if mode == "exit":
                sys.exit(7)
            if mode == "signal":
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                os.kill(os.getpid(), signal.SIGABRT)
            if mode == "protocol":
                os.write(4, b"DST_RPC|invalid-json\n")
                time.sleep(0.03)
        break
    request = json.loads(encoded.removeprefix(b"DST_RPC|"))
    method, params = request["method"], request["arguments"]
    header = {key: request[key] for key in ("v", "nonce", "id", "generation")}
    emit(4, b"DST_RPC|", {**header, "accepted": True})
    if (root / name / ("hold-" + method)).exists():
        (root / name / (method + "-waiting")).touch()
        while (root / name / ("hold-" + method)).exists():
            time.sleep(0.005)
    data = True
    if method == "runtime":
        data = {
            "session_id": session,
            "snapshot": snapshot,
            "shard_id": shards[name],
            "is_master_shard": name == "Master",
        }
    if method == "health":
        data = {
            "generation": generation,
            "protocol": 3,
            "telemetry_status": "active",
            "capabilities": {
                "players": "failed"
                if name == "Caves" and (root / "unhealthy").exists()
                else "active"
            },
        }
    if method == "connected_shards":
        data = [
            {"id": shard_id, "name": shard, "ready": True}
            for shard, shard_id in shards.items()
        ]
    if method == "presence":
        data = {
            "reliable": True,
            "client_count": 0,
            "player_count": 0,
            "idle_seconds": 0,
            "observed_seconds": 0,
            "session_id": session,
            "observation": nonce + ":" + str(generation),
            "outdated_mods": [],
        }
    if method == "list_snapshots":
        snapshots = [
            {"snapshot_id": i, "world_file": f"session/{session}/{i:010}"}
            for i in range(snapshot - 1, 0, -1)
            if params.get("before") is None or i < params["before"]
        ]
        data = {
            "session_id": session,
            "snapshots": snapshots[: params["limit"]],
            "has_more": len(snapshots) > params["limit"],
        }
    if method in ("rollback_to_snapshot", "regenerate"):
        marker = root / "reload-request"
        serial = json.loads(marker.read_text())["id"] + 1 if marker.exists() else 1
        marker.write_text(
            json.dumps({
                "id": serial,
                "regenerate": method == "regenerate",
                "snapshot_id": params.get("snapshot_id"),
            })
        )
    if method == "world":
        data = {"day": 3, "cycles": 2, "season": "autumn"}
    if method == "announce":
        with (root / "announcements").open("a") as log:
            log.write(json.dumps(params) + "\n")
    if method == "save":
        marker = root / "save-request"
        serial = json.loads(marker.read_text())["id"] + 1 if marker.exists() else 1
        (root / "save-request.new").write_text(
            json.dumps({"id": serial, "snapshot_id": snapshot})
        )
        (root / "save-request.new").replace(marker)
    if method == "evaluate":
        source = params["source"]
        if source == "old_shutdown_save":
            control(shutdown=True)
        if source == "crash":
            os._exit(7)
        if source == "slow":
            counter = root / "executions"
            counter.write_text(
                str(int(counter.read_text()) + 1 if counter.exists() else 1)
            )
            time.sleep(0.2)
        data = {"output": "captured", "values": [], "error": None, "truncated": False}
    if method == "locate_player":
        location = (
            (root / "player-location").read_text()
            if (root / "player-location").exists()
            else "Master"
        )
        moving = migration()
        active = name == location and not (root / "kicked").exists()
        loading = moving is not None and name == moving["destination"]
        data = {
            "player": {"userid": params["userid"]} if active or loading else None,
            "guid": PLAYER_GUID if active else None,
            "departing": moving is not None and name == moving["source"],
            "session_id": session,
        }
    if method == "migrate":
        data = (
            not (root / "refuse-migration").exists()
            and params.get("_expected_guid") == PLAYER_GUID
            and params.get("_expected_session_id") == session
            and params.get("_expected_generation") == generation
        )
        with (root / "migration-requests").open("a") as log:
            log.write(name + "\n")
        if data:
            destination = next(
                shard
                for shard, shard_id in shards.items()
                if shard_id == params["shard_id"]
            )
            (root / "migration.new").write_text(
                json.dumps({"source": name, "destination": destination})
            )
            (root / "migration.new").replace(root / "migration.json")
            pending_migration = (destination, time.monotonic() + 0.12)
    if method == "kick":
        data = (
            params.get("_expected_guid") == PLAYER_GUID
            and params.get("_expected_session_id") == session
            and params.get("_expected_generation") == generation
        )
        if data:
            (root / "kicked").write_text("disconnected")
    if method == "ban":
        data = "seconds" not in params or isinstance(params["seconds"], int)
        if data:
            (root / "banned").write_text(name + ":" + params["userid"])
    if method == "blocklist":
        data = (
            [(root / "banned").read_text().split(":", 1)[1]]
            if (root / "banned").exists()
            else []
        )
    if method == "is_blocked":
        data = (root / "banned").exists() and (
            root / "banned"
        ).read_text() == name + ":" + params["userid"]
    if method == "unban":
        data = (root / "banned").exists() and (
            root / "banned"
        ).read_text() == name + ":" + params["userid"]
        if data:
            (root / "banned").unlink()
    if method == "reload_permissions":
        (root / (name + ".permissions")).write_bytes(
            (root / "adminlist.txt").read_bytes()
        )
        data = not (root / ("refuse-permissions-" + name)).exists()
    if method == "set_vitals":
        data = all(value is not None for value in params.values())
        if data:
            (root / "vitals").write_text(json.dumps(params))
    if method == "teleport":
        data = (
            params.get("_expected_guid") == PLAYER_GUID
            and params.get("_expected_session_id") == session
        )
    emit(4, b"DST_RPC|", {**header, "result": {"ok": True, "data": data}})
    os.write(4, b"DST_RemoteCommandDone\n")
