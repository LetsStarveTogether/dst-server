"""Exercise one packaged gameplay template in a fresh, disposable container.

Requires an installed native wheel, Podman and an existing game image.
Network access is used only by the explicitly requested native Mod downloader.
"""

# Commands and paths are explicit test inputs; no production room is accepted.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, start-process-with-partial-path, too-many-statements]

import argparse
import asyncio
import hashlib
import json
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dst_server import Client, DstError
from dst_server.settings import ClusterSettings, build_template, template_names


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_report(root: Path, report: dict) -> None:
    temporary = root / "report.tmp"
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(root / "report.json")


def container(arguments: argparse.Namespace, root: Path, suffix: str) -> list[str]:
    mapping = "idmap=uids=0-1000-1;gids=0-1000-1"
    (root / "steam-logs").mkdir(exist_ok=True)
    return [
        "podman",
        "run",
        "--rm",
        "--init",
        "--pull=never",
        "--http-proxy=false",
        "--name",
        f"dst-rust-mode-{arguments.template}-{suffix}",
        "-e",
        "OTEL_SDK_DISABLED=true",
        "-v",
        f"{root}/cluster:/cluster:{mapping}",
        "-v",
        f"{root}/cluster/mods:/install/mods:{mapping}",
        "-v",
        f"{root}/steam-logs:/home/steam/Steam/logs:{mapping}",
        "-v",
        f"{arguments.scripts.resolve()}:/install/data/databundles/scripts.zip:ro",
        "-v",
        f"{arguments.binary.resolve()}:/probe/dst-server:ro",
    ]


def stop(name: str) -> None:
    subprocess.run(
        ["podman", "stop", "--time", "360", name],
        capture_output=True,
        check=False,
        timeout=390,
    )


def download(arguments: argparse.Namespace, root: Path, report: dict) -> bool:
    wanted = report["expected_mods"]
    if not wanted:
        report["download"] = {"required": False}
        return True
    if not arguments.download_mods:
        report["download"] = {
            "required": True,
            "skipped": "--download-mods was not selected",
        }
        return False
    storage = root / "cluster/updater"
    (storage / "conf/cluster/shard").mkdir(parents=True, exist_ok=True)
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as game,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as steam,
    ):
        game.bind(("0.0.0.0", 0))  # ruff: ignore[hardcoded-bind-all-interfaces]
        steam.bind(("0.0.0.0", 0))  # ruff: ignore[hardcoded-bind-all-interfaces]
        ports = game.getsockname()[1], steam.getsockname()[1]
    command = [
        *container(arguments, root, "download"),
        "--network",
        "host",
        "--workdir",
        "/install/bin64",
        "--entrypoint",
        "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64",
        arguments.image,
        "-only_update_server_mods",
        "-port",
        str(ports[0]),
        "-steam_master_server_port",
        str(ports[1]),
        "-ugc_directory",
        "/cluster/mods/ugc",
        "-persistent_storage_root",
        "/cluster/updater",
        "-conf_dir",
        "conf",
        "-cluster",
        "cluster",
        "-shard",
        "shard",
    ]
    then = time.monotonic()
    name = f"dst-rust-mode-{arguments.template}-download"
    with (root / "download.log").open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=log,
                check=False,
                timeout=arguments.download_timeout,
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            stop(name)
            returncode = None
    source = (root / "download.log").read_text(encoding="utf-8", errors="replace")
    complete = "FinishDownloadingServerMods Complete!" in source
    modinfo = {}
    for path in (root / "cluster/mods/ugc/content/322330").glob("*/modinfo.lua"):
        modinfo[str(path.relative_to(root / "cluster/mods"))] = digest(path)
    failures = [
        line
        for line in source.splitlines()
        if "timed out" in line
        or "failed entirely" in line
        or "FAILED: DownloadPublishedFile" in line
        or "#ERROR: Failure to load dedicated_server_mods_setup.lua:" in line
    ]
    downloaded = {
        mod
        for mod in wanted
        if any(mod.removeprefix("workshop-") in Path(path).parts for path in modinfo)
    }
    report["download"] = {
        "required": True,
        "returncode": returncode,
        "seconds": time.monotonic() - then,
        "completion_marker": complete,
        "failure_lines": failures,
        "modinfo_sha256": modinfo,
        "missing": sorted(set(wanted) - downloaded),
    }
    return returncode == 0 and complete and not failures and downloaded == set(wanted)


def verify_world(template: str, name: str, observed: dict) -> None:
    if name == "forest" and "endless" in template:
        assert observed["overrides"]["resettime"] == "none"
        assert observed["overrides"]["portalresurection"] == "always"
    if name == "forest" and template.startswith("lights_out"):
        assert observed["overrides"]["day"] == "onlynight"


def verify_lifecycle(
    method: str, value: Any, expected: set[str], *, saves: bool = True
) -> None:
    if method not in {"save", "stop"}:
        return
    assert {shard["shard"] for shard in value["shards"]} == expected, value
    for shard in value["shards"]:
        result = shard["result"]
        assert result["status"] == "success", shard
        if method == "save":
            assert result["value"]["snapshot_id"] > 0, shard
        else:
            assert result["value"]["forced"] is False, shard
            assert result["value"]["output_drained"] is True, shard
            assert result["value"]["returncode"] == 0, shard
            saved = result["value"]["saved_snapshot"]
            assert saved["snapshot_id"] > 0 if saves else saved is None, shard


def verify_unconfirmed(error: DstError, expected: set[str], seconds: float) -> None:
    assert seconds < 15, (seconds, error)
    assert error.code == "partial_failure", error
    shards = error.details["shards"]
    assert {shard["shard"] for shard in shards} == expected, error.details
    for shard in shards:
        outcome = shard["result"]
        assert outcome["status"] == "failure", outcome
        assert outcome["error"]["code"] == "unknown", outcome
        assert "snapshot" in outcome["error"]["message"], outcome


async def connect_ready(
    arguments: argparse.Namespace, root: Path, report: dict
) -> tuple[Client, dict]:
    client = None
    async with asyncio.timeout(arguments.start_timeout):
        while client is None:
            try:
                client = await Client.connect(root / "cluster/.dst-agent.sock")
            except DstError, OSError:
                await asyncio.sleep(0.2)
        while True:
            state = await client.call("status")
            report["last_status"] = state
            if state["phase"] == "failed":
                message = "room failed before readiness"
                raise RuntimeError(message)
            if state["phase"] == "running" and all(
                shard["readiness"]["world_loaded"]
                and shard["readiness"]["control_ready"]
                and not shard["readiness"]["missing_shards"]
                for shard in state["shards"]
            ):
                break
            await asyncio.sleep(0.2)
    return client, state


async def exercise(arguments: argparse.Namespace, root: Path, report: dict) -> None:
    client, state = await connect_ready(arguments, root, report)
    report["ready_at"] = datetime.now(UTC).isoformat()
    save_report(root, report)
    operations = report["operations"]

    async def call(
        method: str, value: dict | None = None, shard: str | None = None
    ) -> Any:
        then = time.monotonic()
        report["pending_operation"] = {
            "method": method,
            "arguments": value,
            "shard": shard,
            "started_at": datetime.now(UTC).isoformat(),
        }
        save_report(root, report)
        before = {}
        if method == "save" and report["save_expectation"] == "unconfirmed":
            for name in report["mods_by_shard"]:
                before[name] = await client.call("runtime", shard=name)
        try:
            result = await client.call(method, value, shard=shard, timeout=300)
        except DstError as error:
            seconds = time.monotonic() - then
            operations.append({
                "method": method,
                "arguments": value,
                "shard": shard,
                "seconds": seconds,
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "details": error.details,
                },
            })
            save_report(root, report)
            if method != "save" or report["save_expectation"] != "unconfirmed":
                raise
            verify_unconfirmed(error, set(report["mods_by_shard"]), seconds)
            after = {}
            for name, previous in before.items():
                after[name] = await client.call("runtime", shard=name)
                assert after[name]["snapshot"] == previous["snapshot"]
                assert after[name]["session_id"] == previous["session_id"]
            operations[-1]["runtime_before"] = before
            operations[-1]["runtime_after"] = after
            del report["pending_operation"]
            save_report(root, report)
            return None
        operations.append({
            "method": method,
            "arguments": value,
            "shard": shard,
            "seconds": time.monotonic() - then,
            "value": result,
        })
        del report["pending_operation"]
        save_report(root, report)
        saves = report["save_expectation"] == "confirmed"
        assert method != "save" or saves, "save unexpectedly reported success"
        verify_lifecycle(method, result, set(report["mods_by_shard"]), saves=saves)
        return result

    try:
        for shard in state["shards"]:
            name = shard["name"]
            runtime = await call("runtime", shard=name)
            assert runtime["app_version"] == arguments.game_version
            room = await call("room", shard=name)
            assert room["max_players"] == report["max_players"]
            assert room["player_count"] == 0
            assert room["game_mode"] == report["game_mode"]
            enabled = await call("mods", shard=name)
            actual = {mod["id"] for mod in enabled}
            assert set(report["mods_by_shard"][name]) <= actual, enabled
            await call("world", shard=name)
            observed = await call(
                "execute_json",
                {
                    "source": "return {overrides=TheWorld.topology.overrides,"
                    "prefab=TheWorld.prefab,game_mode=TheNet:GetServerGameMode()}"
                },
                name,
            )
            verify_world(arguments.template, name, observed)
        pause_result = await call("pause", {"paused": True})
        pause_states = {}
        for shard in state["shards"]:
            name = shard["name"]
            pause_states[name] = (await call("room", shard=name))["is_paused"]
        report["pause"] = {
            "request": pause_result,
            "actual_by_shard": pause_states,
            "required_for_mode_pass": False,
        }
        report["pause"]["resume"] = await call("pause", {"paused": False})
        await call("save")
        await call("stop", {"notice": None})
        stopped = await call("status")
        assert stopped["phase"] == "stopped"
        assert all(shard["pid"] is None for shard in stopped["shards"])
        await call("start")
        restarted = await call("status")
        assert restarted["phase"] == "running"
        assert {
            shard["name"]: shard["identity"]["session_id"]
            for shard in restarted["shards"]
        } == {
            shard["name"]: shard["identity"]["session_id"] for shard in state["shards"]
        }
        await call("save")
        await call("stop", {"notice": None})
        final = await call("status")
        assert final["phase"] == "stopped"
        report["passed"] = True
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("template", choices=template_names())
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--scripts", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--game-version", required=True)
    parser.add_argument("--download-mods", action="store_true")
    parser.add_argument("--download-timeout", type=float, default=600)
    parser.add_argument("--start-timeout", type=float, default=900)
    parser.add_argument(
        "--save-expectation", choices=("confirmed", "unconfirmed"), default="confirmed"
    )
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=False)
    settings = ClusterSettings(
        cluster_name=f"Rust mode probe: {args.template}",
        offline_cluster=True,
        lan_only_cluster=True,
        pause_when_empty=False,
    )
    configuration = build_template(args.template, number=299, settings=settings)
    config = configuration.dump(secrets=True)
    if args.template == "pure_survival":
        config["settings"]["max_players"] = 64
        configuration = type(configuration)("ClusterConfig", config)
    cluster = root / "cluster"
    configuration.save(cluster)
    (cluster / ".dst-rust-probe").touch()
    (cluster / ".dst-control.json").write_text(
        json.dumps({"policy": {"mod_auto_update": False}}), encoding="utf-8"
    )
    (cluster / "mods/ugc").mkdir(exist_ok=True)
    mods = {
        name: sorted(
            mod
            for mod, value in shard.get("mods", {}).get("entries", {}).items()
            if value.get("enabled", True)
        )
        for name, shard in config["shards"].items()
    }
    report = {
        "passed": False,
        "template": args.template,
        "image": args.image,
        "native_build": args.game_version,
        "actual_connected_players": 0,
        "max_players": configuration.dump(defaults=True)["settings"]["max_players"],
        "game_mode": configuration.dump(defaults=True)["settings"]["game_mode"],
        "save_expectation": args.save_expectation,
        "expected_mods": sorted({mod for values in mods.values() for mod in values}),
        "mods_by_shard": mods,
        "binary_sha256": digest(args.binary),
        "bundle_sha256": digest(args.scripts),
        "operations": [],
        "started_at": datetime.now(UTC).isoformat(),
    }
    save_report(root, report)
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        if not download(args, root, report):
            message = "required Mods were not downloaded"
            report["blocked"] = message
            raise RuntimeError(message)  # ruff: ignore[raise-within-try]
        save_report(root, report)
        command = [
            *container(args, root, "game"),
            "--network",
            "none",
            "--entrypoint",
            "/probe/dst-server",
            args.image,
            "agent",
            "--cluster",
            "/cluster",
            "--offline",
            "--profile",
            "history",
        ]
        with (root / "agent.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            try:
                asyncio.run(exercise(args, root, report))
            finally:
                stop(f"dst-rust-mode-{args.template}-game")
                process.wait(timeout=390)
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = repr(error)
    finally:
        report["completed_at"] = datetime.now(UTC).isoformat()
        save_report(root, report)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
