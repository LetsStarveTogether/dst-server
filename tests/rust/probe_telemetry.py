"""Check SDK framing in an isolated native game using the public CLI.

Requires an immutable local image, an SDK binary and its matching script bundle.
Every room is newly prepared, offline and credential-free.
"""

# Commands target only the supplied binary and the newly prepared test room.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, start-process-with-partial-path, print]

import argparse
import hashlib
import json
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from native_agent import call, check_profile_log, exercise_profile, ready, records, stop
from prepare_probe import prepare

SIZES = (4095, 4096, 4097, 8192, 32768, 65535, 65536, 65537)
TEXT = '边界😀"\\\n\0'
INSTALL = r"""
TheWorld.R2_heartbeats = 0
TheWorld:DoStaticPeriodicTask(.1, function()
    TheWorld.R2_heartbeats = TheWorld.R2_heartbeats + 1
end)
function TheWorld.R2_emit(size, label)
    local wire = require("dst_server.wire")
    local telemetry = require("dst_server.telemetry")
    local state = require("dst_server.state")
    local data = {clients={label .. '边界😀"\\\n\0'}, players={},
        world={}, reason="periodic"}
    for _, key in ipairs({"season","phase","moonphase","nightmarephase",
        "israining","issnowing","isacidraining","islunarhailing",
        "lunar_rifts_enabled","shadow_rifts_enabled"}) do
        data.world[key] = json.null
    end
    -- Measure the SDK's own frame without publishing a record or consuming a number.
    local length
    local write, sequence = wire.write_line, state.sequence
    wire.write_line = function(line) length = #line + 1; return true end
    telemetry.emit("dst.server.presence", data)
    wire.write_line, state.sequence = write, sequence
    local missing = size - length
    while missing > 3 do
        local n = math.min(missing - 3, 120)
        local id = tostring(#data.clients) .. string.rep("x", n)
        data.clients[#data.clients+1] = string.sub(id, 1, n)
        missing = missing - n - 3
    end
    local last = #data.clients
    data.clients[last] = data.clients[last] .. string.rep("x", missing)
    telemetry.emit("dst.server.presence", data)
    print("R2_NATIVE " .. label)
    return {clients=#data.clients, last=data.clients[#data.clients],
        health=require("dst_server").health()}
end
TheWorld.R2_original_clients = GetPlayerClientTable
function TheWorld.R2_clients(count)
    local clients = {}
    for i = 1, count do
        clients[i] = { userid = string.format("KU_R2_%02d", i),
            netid = string.format("7656119800000%04d", i),
            name = string.rep('玩家😀"\\', 16) .. i, admin = false, moderator = true }
    end
    GetPlayerClientTable = function() return clients end
end
return {actual_clients=#TheWorld.R2_original_clients(), actual_players=#AllPlayers}
"""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_report(root: Path, report: dict) -> None:
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def command(args: argparse.Namespace, root: Path, name: str) -> list[str]:
    mapping = "idmap=uids=0-1000-1;gids=0-1000-1"
    return [
        "podman",
        "run",
        "--rm",
        "--init",
        "--pull=never",
        "--http-proxy=false",
        "--network",
        "none",
        "--cpus=1",
        "--memory=2g",
        "--memory-swap=2g",
        "--pids-limit=128",
        "--log-driver=none",
        "--name",
        name,
        "--cidfile",
        str(root / "container.cid"),
        "-e",
        "OTEL_SDK_DISABLED=true",
        "-e",
        f"DST_SERVER_TELEMETRY_PROFILE={args.profile}",
        "-v",
        f"{root}/cluster:/cluster:{mapping}",
        "-v",
        f"{root}/cluster/mods:/install/mods:{mapping}",
        "-v",
        f"{root}/steam-logs:/home/steam/Steam/logs:{mapping}",
        "-v",
        f"{args.scripts}:/install/data/databundles/scripts.zip:ro",
        "-v",
        f"{args.binary}:/probe/dst-server:ro",
        "--entrypoint",
        "/probe/dst-server",
        args.image,
    ]


def cleanup(root: Path) -> None:
    cidfile = root / "container.cid"
    if not cidfile.exists():
        return
    container_id = cidfile.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        return
    subprocess.run(
        ["podman", "rm", "--force", "--ignore", container_id],
        capture_output=True,
        check=True,
        timeout=30,
    )


def lua(args: argparse.Namespace, root: Path, report: dict, source: str) -> object:
    assert len(source.encode()) <= 3072
    return call(
        args.binary, root, report, "execute_json", {"source": source}, shard="surface"
    )


def wait_record(
    log: Path, predicate: Callable[[dict], bool], *, timeout: int = 75
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in records(log):
            if predicate(record):
                return record
        time.sleep(0.2)
    message = "expected structured native event was not received"
    raise TimeoutError(message)


def pressure(args: argparse.Namespace, root: Path, report: dict) -> None:
    # The CLI blocks on its output pipe; its Agent subscription then reaches capacity.
    subscriber = subprocess.Popen(
        [
            str(args.binary),
            "--json",
            "subscribe",
            "events",
            "--socket",
            str(root / "cluster/.dst-agent.sock"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        before = lua(args, root, report, "return TheWorld.R2_heartbeats")
        lua(
            args,
            root,
            report,
            """
TheWorld.R2_bursts = 0
TheWorld.R2_stress = TheWorld:DoStaticPeriodicTask(.1, function()
    if TheWorld.R2_bursts >= 60 then TheWorld.R2_stress:Cancel(); return end
    TheWorld.R2_bursts = TheWorld.R2_bursts + 1
    for i = 1, 10 do TheWorld.R2_emit(8192, "R2_STRESS") end
end)
return true
""",
        )
        latencies = []
        for _ in range(12):
            time.sleep(0.5)
            health = call(args.binary, root, report, "health", shard="surface")
            assert health["telemetry_status"] == "active", health
            latencies.append(report["calls"][-1]["seconds"])
        after = lua(
            args,
            root,
            report,
            "return {heartbeat=TheWorld.R2_heartbeats,bursts=TheWorld.R2_bursts}",
        )
        deadline = time.monotonic() + 60
        while after["bursts"] < 60 and time.monotonic() < deadline:
            time.sleep(0.2)
            health = call(args.binary, root, report, "health", shard="surface")
            assert health["telemetry_status"] == "active", health
            latencies.append(report["calls"][-1]["seconds"])
            after = lua(
                args,
                root,
                report,
                "return {heartbeat=TheWorld.R2_heartbeats,bursts=TheWorld.R2_bursts}",
            )
        assert after["heartbeat"] > before + 20, after
        assert after["bursts"] == 60, after
        collected = []
        reader = threading.Thread(
            target=lambda: collected.append(subscriber.communicate(timeout=30))
        )
        reader.start()
        time.sleep(1)
        subscriber.send_signal(signal.SIGTERM)
        reader.join(timeout=35)
        assert not reader.is_alive(), collected
        assert subscriber.returncode == 0, collected
        output, error = collected[0]
        assert not error, error
        (root / "slow-subscription.jsonl").write_bytes(output)
        batches = [json.loads(line) for line in output.splitlines()]
        dropped = sum(batch["dropped"] for batch in batches)
        assert dropped > 0, "slow subscription must exercise its capacity"

        def stress(record: dict) -> bool:
            return record["event_name"] == "dst.server.presence" and record["body"][
                "clients"
            ][:1] == ["R2_STRESS" + TEXT]

        rows = [record for record in records(root / "agent.log") if stress(record)]
        assert len(rows) == 600, len(rows)
        local = {record["attributes"]["log.record.uid"]: record for record in rows}
        delivered = [
            record for batch in batches for record in batch["records"] if stress(record)
        ]
        assert len(local) == len(rows), "duplicate local observation UID"
        assert 0 < len(delivered) < len(local), len(delivered)
        assert len({row["attributes"]["log.record.uid"] for row in delivered}) == len(
            delivered
        ), "duplicate RPC observation UID"
        for record in delivered:
            assert record == local[record["attributes"]["log.record.uid"]]
        report["pressure"] = {
            "dropped": dropped,
            "local": len(local),
            "delivered": len(delivered),
            "health_rpc_seconds": latencies,
            "heartbeat_before": before,
            "after": after,
        }
    finally:
        if subscriber.poll() is None:
            subscriber.kill()
        subscriber.wait(timeout=10)


def check_frames(args: argparse.Namespace, root: Path, report: dict, log: Path) -> None:
    report["frames"] = []
    for size in SIZES:
        label = f"R2_SIZE_{size}_"
        result = lua(args, root, report, f"return TheWorld.R2_emit({size}, '{label}')")
        if size <= 65536:
            expected = label + TEXT
            record = wait_record(
                log,
                lambda item, expected=expected: (
                    item["event_name"] == "dst.server.presence"
                    and item["body"]["clients"][:1] == [expected]
                ),
                timeout=10,
            )
            clients = record["body"]["clients"]
            assert len(clients) == result["clients"]
            assert clients[-1] == result["last"]
            wait_record(
                log,
                lambda item, label=label: (
                    item["event_name"] == "dst.server.log"
                    and f"R2_NATIVE {label}" in item["body"]["line"]
                ),
                timeout=10,
            )
        else:
            assert result["health"]["last_error"]["message"] == "event_too_large"
            recovered = lua(
                args, root, report, "return TheWorld.R2_emit(4096, 'R2_RECOVERED_')"
            )
            assert recovered["health"]["telemetry_status"] == "active"
        report["frames"].append({"line_bytes": size, "clients": result["clients"]})


def exercise(
    args: argparse.Namespace,
    root: Path,
    report: dict,
    log: Path,
    process: subprocess.Popen,
) -> None:
    state = ready(args.binary, root, report, process)
    runtime = call(args.binary, root, report, "runtime", shard="surface")
    assert runtime["app_version"] == args.game_version, runtime
    report["runtime"] = runtime
    assert lua(args, root, report, INSTALL) == {
        "actual_clients": 0,
        "actual_players": 0,
    }
    exercise_profile(args.binary, root, report, args.profile)
    expected_health = report["profile_health"]["telemetry_status"]
    lua(args, root, report, "TheWorld.R2_clients(64); return true")
    presence = call(args.binary, root, report, "presence", shard="surface")
    assert presence["client_count"] == 64, presence
    if args.profile == "off":
        assert report["profile_health"]["events_emitted"] == 0
    else:
        record = wait_record(
            log,
            lambda item: (
                item["event_name"] == "dst.server.presence"
                and len(item["body"]["clients"]) == 64
            ),
        )
        assert record["body"]["reason"] == "periodic"
        assert record["body"]["players"] == []
        assert set(record["body"]["clients"]) == {
            f"KU_R2_{i:02d}" for i in range(1, 65)
        }
        report["presence"] = {
            "clients": 64,
            "record_json_bytes": len(json.dumps(record).encode()),
        }
    lua(
        args,
        root,
        report,
        "GetPlayerClientTable=TheWorld.R2_original_clients; return true",
    )
    if args.profile == "play":
        check_frames(args, root, report, log)
        pressure(args, root, report)
    report["telemetry"] = call(args.binary, root, report, "status")["agent"][
        "telemetry"
    ]
    assert report["telemetry"]["receivers"]["surface"]["rejections"] == {}, report[
        "telemetry"
    ]
    call(args.binary, root, report, "save")
    call(args.binary, root, report, "reset", timeout=600)
    reloaded = ready(args.binary, root, report)
    report["reload"] = {
        "before": state["shards"][0]["identity"],
        "after": reloaded["shards"][0]["identity"],
    }
    assert report["reload"]["after"] != report["reload"]["before"]
    assert (
        call(args.binary, root, report, "health", shard="surface")["telemetry_status"]
        == expected_health
    )
    call(args.binary, root, report, "save")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--scripts", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--game-version", required=True)
    parser.add_argument(
        "--profile", choices=("off", "timeline", "play"), default="play"
    )
    args = parser.parse_args()
    assert re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", args.image), (
        "immutable local image ID required"
    )
    assert args.name.startswith("dst-r2-")
    assert re.fullmatch(r"[a-z0-9-]+", args.name)
    args.binary, args.scripts = args.binary.resolve(), args.scripts.resolve()
    root = args.directory.resolve()
    cluster = prepare(root, 1)
    (root / "steam-logs").mkdir()
    (cluster / ".dst-server.json").write_text(
        json.dumps({
            "policy": {"mod_auto_update": False, "idle_regeneration": False},
        })
    )
    report = {
        "passed": False,
        "profile": args.profile,
        "image": args.image,
        "binary_sha256": digest(args.binary),
        "bundle_sha256": digest(args.scripts),
        "actual_connected_players": 0,
        "calls": [],
        "exits": [],
    }
    run = [
        *command(args, root, args.name),
        "agent",
        "--cluster",
        "/cluster",
        "--offline",
    ]
    (root / "command.json").write_text(json.dumps(run, indent=2) + "\n")
    write_report(root, report)
    process = None
    watchdog = threading.Timer(1200, lambda: cleanup(root))
    watchdog.start()
    try:
        with (root / "agent.log").open("wb") as output:
            process = subprocess.Popen(run, stdout=output, stderr=output)
        exercise(args, root, report, root / "agent.log", process)
        stop(args.binary, root, report, process)
        check_profile_log(root / "agent.log", args.profile)
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = repr(error)
        raise
    else:
        report["passed"] = True
    finally:
        watchdog.cancel()
        if (root / "container.cid").exists():
            report["container_id"] = (root / "container.cid").read_text().strip()
        cleanup(root)
        if process is not None:
            process.wait(timeout=30)
        report["container_removed"] = True
        write_report(root, report)
    print(json.dumps({"passed": report["passed"], "report": str(root / "report.json")}))


if __name__ == "__main__":
    main()
