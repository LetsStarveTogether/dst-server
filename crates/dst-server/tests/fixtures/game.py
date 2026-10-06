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
from collections.abc import Mapping

PLAYER_GUID = 17
args = sys.argv[1:]


def arg(name: str) -> str:
    return args[args.index(name) + 1]


root = pathlib.Path(arg("-persistent_storage_root")) / arg("-cluster")
name = arg("-shard")
options = json.loads((root / name / "dst_server_driver.json").read_text())["driver"]
nonce = options["nonce"]
snapshot, generation = 5, 1
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


def control(record: Mapping[str, object]) -> None:
    emit(4, b"DST_CONTROL|", {"nonce": nonce, "generation": generation, **record})


def migration() -> dict[str, str] | None:
    try:
        return json.loads((root / "migration.json").read_text())
    except FileNotFoundError:
        return None


def record_lifecycle(event: str) -> None:
    with (root / name / "native-lifecycle").open("a") as output:
        output.write(event + "\n")
    with (root / name / "native-times.jsonl").open("a") as output:
        output.write(json.dumps({"event": event, "time": time.monotonic()}) + "\n")


def perform_save(shutdown: bool = False) -> None:
    global snapshot
    record_lifecycle("shutdown_save" if shutdown else "save")
    snapshot += 1


def health() -> dict[str, object]:
    profile = options["profile"]
    return {
        "generation": generation,
        "profile": profile,
        "control_ready": not (name == "Caves" and (root / "unhealthy").exists()),
        "telemetry_status": "disabled" if profile == "off" else "active",
        "capabilities": {"connections": "active", "voting": "active"},
        "errors": 0,
        "revision": 2,
        "play_queue_dropped": 0,
        "events_emitted": 0,
        "last_error": None,
        "faults": {},
    }


def bootstrap() -> None:
    os.write(5, ("DST_SessionId|" + session + "\nDST_Master_Ready\n").encode())
    emit(4, b"DST_DRIVER|", {"nonce": nonce, "generation": generation})
    emit(4, b"DST_DRIVER|", {"nonce": nonce, "health": health()})


recovery_request = options.get("control", {}).get("recovery")
if recovery_request is not None:
    calls = root / name / "catalog-calls"
    count = int(calls.read_text()) + 1 if calls.exists() else 1
    calls.write_text(str(count))
    if (root / name / "hold-catalog").exists():
        waiting = root / name / "catalog-waiting"
        waiting.with_suffix(".new").write_text(str(os.getpid()))
        waiting.with_suffix(".new").replace(waiting)
        while (root / name / "hold-catalog").exists():
            time.sleep(0.005)
    fail_after = root / "catalog-fail-after"
    if fail_after.exists() and count > int(fail_after.read_text()):
        record = {"event": "recovery_failed", "error": "missing_session"}
    else:
        snapshots = [
            {
                "snapshot_id": int(path.name),
                "world_file": "session/" + session + "/" + path.name,
            }
            for path in sorted((root / name / "save/session" / session).iterdir())
            if path.name.isdecimal()
        ]
        record = {
            "event": "recovery_catalog",
            "session_id": session,
            "has_more": False,
            "latest_world_file": snapshots[-1]["world_file"],
            "snapshots": snapshots,
        }
    control(record)
    raise SystemExit(0)


if (root / "ignore-term").exists():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
record_lifecycle("start")
bootstrap()
while True:
    fault = root / name / "fault-mode"
    if fault.exists():
        mode = fault.read_text()
        if mode in ("decode", "generation-exit", "generation-hold"):
            generation += 1
            record = {"event": "world_load_started"}
            if mode == "decode":
                control(record)
                record = {
                    "event": "world_load_failed",
                    "session_id": session,
                    "world_file": f"session/{session}/0000000005",
                    "phase": "decode",
                    "reason": "parse_failed",
                    "read_succeeded": True,
                    "callback_entered": False,
                    "source_bytes": 4,
                    "decoder_unchanged": True,
                }
            control(record)
        if mode in ("generation-hold", "diagnostic-hold"):
            if mode == "diagnostic-hold":
                os.write(1, b"LUA ERROR stack traceback:\n")
                os.write(4, b"DST_OTEL|{}\n")
            (root / name / "fault-waiting").touch()
            while fault.exists():
                time.sleep(0.005)
            if mode == "generation-hold":
                bootstrap()
        else:
            raise SystemExit(7)
    if (
        pending_migration is not None
        and time.monotonic() >= pending_migration[1]
        and not (root / "hold-migration").exists()
    ):
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
    if marker.exists() and not (root / "hold-save").exists():
        save = json.loads(marker.read_text())
        if save["id"] > seen_save:
            seen_save = save["id"]
            snapshot = save["snapshot_id"]
            perform_save()
    if not select.select([3], [], [], 0.01)[0]:
        continue
    encoded = os.read(3, 4096)
    if not encoded:
        break
    if encoded == b"c_shutdown()\n":
        record_lifecycle("shutdown")
        if (root / "hang-shutdown").exists():
            time.sleep(5)
        time.sleep(0.08)
        perform_save(shutdown=True)
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
    header = {key: request[key] for key in ("nonce", "id", "generation")}
    emit(4, b"DST_RPC|", {**header, "accepted": True})
    reload_during = root / name / ("reload-during-" + method)
    if reload_during.exists():
        reload_during.unlink()
        record_lifecycle("reload")
        generation += 1
        control({"event": "world_load_started"})
        bootstrap()
        continue
    if (root / name / ("hold-" + method)).exists():
        (root / name / (method + "-waiting")).touch()
        while (root / name / ("hold-" + method)).exists():
            time.sleep(0.005)
    data = True
    if method == "mods":
        with (root / name / "mod-queries").open("a") as calls:
            calls.write("mods\n")
        source = root / name / "enabled-mods.json"
        data = json.loads(source.read_text()) if source.exists() else []
    if method == "runtime":
        data = {
            "session_id": session,
            "snapshot": snapshot,
            "shard_id": shards[name],
            "is_master_shard": name == "Master",
        }
    if method == "health":
        data = health()
    if method == "connected_shards":
        data = [
            {"id": shard_id, "name": shard, "is_current": shard == name, "ready": True}
            for shard, shard_id in shards.items()
        ]
        response = root / name / "connected-shards.json"
        if response.exists():
            data = json.loads(response.read_text())
    if method == "presence":
        data = {
            "day": 3,
            "reliable": True,
            "client_count": 0,
            "player_count": 0,
            "idle_seconds": 0,
            "observed_seconds": 0,
            "session_id": session,
            "observation_epoch": 0,
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
            log.write(json.dumps({**params, "time": time.monotonic()}) + "\n")
        response = root / "announce-response.json"
        if response.exists():
            emit(4, b"DST_RPC|", {**header, "result": json.loads(response.read_text())})
            os.write(4, b"DST_RemoteCommandDone\n")
            continue
    if method == "save":
        marker = root / "save-request"
        serial = json.loads(marker.read_text())["id"] + 1 if marker.exists() else 1
        (root / "save-request.new").write_text(
            json.dumps({"id": serial, "snapshot_id": snapshot})
        )
        (root / "save-request.new").replace(marker)
    if method == "evaluate":
        source = params["source"]
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
        handshake = (root / "player-handshake").exists() and not (
            root / "kicked"
        ).exists()
        guid = (
            PLAYER_GUID if active and not (root / "player-loading").exists() else None
        )
        data = {
            "present": active or loading or handshake,
            "guid": guid,
            "loaded": not handshake if guid is not None else None,
            "departing": active
            and ((root / "player-departing").exists() or moving is not None),
            "session_id": session,
        }
        if params["include_details"] and data["present"]:
            data["player"] = {"userid": params["userid"]}
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
    if method in {"kick", "is_connected"}:
        location = (
            (root / "player-location").read_text()
            if (root / "player-location").exists()
            else "Master"
        )
        moving = migration()
        connected = not (root / "kicked").exists() and (
            name == location or (moving is not None and name == moving["destination"])
        )
        if method == "kick":
            if "_expected_guid" in params:
                message = "kick must target a connection without requiring an entity"
                raise AssertionError(message)
            with (root / "kick-requests").open("a") as log:
                log.write(name + "\n")
            data = connected
            if connected:
                (root / "kicked").write_text("disconnected")
        else:
            data = connected
    if method == "kick" and (root / ("invalid-kick-" + name)).exists():
        data = "malformed result"
    if method == "check_vote_access":
        (root / (name + ".vote-access")).write_text(json.dumps(params))
        data = json.loads((root / "vote-access.json").read_text())
    if method in {"get_vote", "list_vote_commands", "start_vote", "cancel_vote"}:
        (root / (name + ".vote-call")).write_text(json.dumps(request))
        data = json.loads((root / "vote-response.json").read_text())
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
    if method in {"ban", "unban"}:
        data = None
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
    response = root / name / (method + "-response.json")
    result = (
        json.loads(response.read_text())
        if response.exists()
        else {"ok": True, "data": data}
    )
    emit(4, b"DST_RPC|", {**header, "result": result})
    os.write(4, b"DST_RemoteCommandDone\n")
