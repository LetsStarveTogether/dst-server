"""Run the opt-in native engine acceptance matrix in isolated Podman containers.

The supplied image must contain the game under /install and run as UID 1000.
The binary must be compatible with the supplied image.
"""

# Standalone native harnesses invoke explicit trusted executables and print reports.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, start-process-with-partial-path, print]

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from native_agent import call, check_profile_log, exercise_profile, ready, stop
from prepare_probe import prepare


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_hashes(cluster: Path) -> dict[str, str]:
    return {
        str(path.relative_to(cluster)): digest(path)
        for path in cluster.rglob("*")
        if path.is_file() and "save" in path.relative_to(cluster).parts
    }


def check_save(binary: Path, fixture: Path, state: dict, report: dict) -> None:
    names = {shard["name"] for shard in state["shards"]}
    assert names == {"surface", "world-2"}, state
    before = {
        name: call(binary, fixture, report, "runtime", shard=name) for name in names
    }
    existing = {
        file
        for name in names
        for file in (fixture / "cluster" / name / "save" / "session").glob("*/*")
    }
    assert call(binary, fixture, report, "save") is True
    deadline = time.monotonic() + 120
    saved = {}
    while names - saved.keys():
        for name in names - saved.keys():
            runtime = call(binary, fixture, report, "runtime", shard=name)
            assert runtime["session_id"] == before[name]["session_id"], runtime
            if runtime["snapshot"] > before[name]["snapshot"]:
                catalog = call(binary, fixture, report, "list_snapshots", shard=name)
                assert catalog["session_id"] == runtime["session_id"], catalog
                worlds = (
                    fixture / "cluster" / name / "save" / entry["world_file"]
                    for entry in catalog["snapshots"]
                    if entry["world_file"]
                    and entry["snapshot_id"] >= before[name]["snapshot"]
                )
                if any(
                    world not in existing and world.is_file() and world.stat().st_size
                    for world in worlds
                ):
                    saved[name] = runtime
        if names - saved.keys():
            assert time.monotonic() < deadline, ("native save did not finish", saved)
            time.sleep(0.2)
    report["saved"] = saved


def main() -> None:  # ruff: ignore[too-many-locals, too-many-statements]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--source-scripts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    bundle = root / "scripts.zip"
    subprocess.run(
        [
            str(binary),
            "scripts",
            "build",
            str(args.source_scripts.resolve()),
            "--output",
            str(bundle),
        ],
        check=True,
    )
    # These native tests use rootful Podman with idmapped mounts for image UID 1000.
    if os.getuid() != 0:
        parser.error("run as root with rootful Podman (for example: sudo -E python3)")
    mapping = "idmap=uids=0-1000-1;gids=0-1000-1"

    def container(room: str, profile: str = "play") -> list[str]:
        cluster = root / room / "cluster"
        return [
            "podman",
            "run",
            "--rm",
            "--init",
            "--pull=never",
            "--network",
            "none",
            "--name",
            container_name(room),
            "-e",
            "OTEL_SDK_DISABLED=true",
            "-e",
            f"DST_SERVER_TELEMETRY_PROFILE={profile}",
            "-v",
            f"{cluster}:/p0/cluster:{mapping}",
            "-v",
            f"{cluster}/mods:/install/mods:{mapping}",
            "-v",
            f"{bundle}:/install/data/databundles/scripts.zip:ro",
            "-v",
            f"{binary}:/p0/dst-server:ro",
            "--entrypoint",
            "/p0/dst-server",
            args.image,
        ]

    def container_name(room: str) -> str:
        return f"dst-native-{os.getpid()}-{room}"

    def remove(room: str) -> None:
        subprocess.run(
            ["podman", "rm", "--force", "--ignore", container_name(room)],
            capture_output=True,
            check=True,
            timeout=45,
        )

    def run(
        room: str, label: str, command: str, *arguments: str, success: bool = True
    ) -> dict[str, Any] | None:
        fixture = root / room
        command_line = [
            *container(room),
            command,
            "--cluster",
            "/p0/cluster",
            "--timeout",
            "180",
            *arguments,
        ]
        try:
            with (
                (fixture / f"{label}.json").open("w") as report,
                (fixture / f"{label}.log").open("w") as log,
            ):
                result = subprocess.run(
                    command_line, stdout=report, stderr=log, timeout=240, check=False
                )
            assert (result.returncode == 0) == success, (room, label, result.returncode)
            print(room, label, "passed", flush=True)
            return (
                json.loads((fixture / f"{label}.json").read_text()) if success else None
            )
        finally:
            remove(room)

    for room, count in (("one", 1), ("two", 2), ("five", 5)):
        prepare(root / room, count)
        report = run(room, "normal", "probe", "--rounds", "2")
        assert report["passed"]
    fixture = root / "agent"
    cluster = prepare(fixture, 2)
    (cluster / ".dst-server.json").write_text(
        json.dumps({"policy": {"mod_auto_update": False}})
    )
    for profile in ("play", "off", "timeline"):
        agent_log = fixture / f"{profile}.log"
        with (
            agent_log.open("wb") as log,
            subprocess.Popen(
                [
                    *container("agent", profile),
                    "agent",
                    "--cluster",
                    "/p0/cluster",
                    "--offline",
                ],
                stdout=log,
                stderr=log,
            ) as process,
        ):
            try:
                report = {"calls": [], "exits": []}
                state = ready(binary, fixture, report, process)
                exercise_profile(binary, fixture, report, profile)
                if profile == "play":
                    check_save(binary, fixture, state, report)
                    lifecycle = report
                stop(binary, fixture, report, process)
            finally:
                remove("agent")
        check_profile_log(agent_log, profile)
    assert {entry["shard"] for entry in lifecycle["exits"][0]["shards"]} == set(
        lifecycle["saved"]
    )
    (fixture / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2) + "\n")
    catalog = run("five", "recovery-catalog", "probe-recovery")
    assert catalog["passed"]
    assert not catalog["worlds_loaded"]
    targets = {
        name: dict(session_id=record["session_id"], **record["snapshots"][1])
        for name, record in catalog["shards"].items()
    }
    (root / "five/cluster/targets.json").write_text(json.dumps(targets))
    applied = run(
        "five",
        "recovery-apply",
        "probe-recovery",
        "--targets",
        "/p0/cluster/targets.json",
    )
    assert all(
        record["changed"] and record["generation"] == 1
        for record in applied["shards"].values()
    )
    repeated = run(
        "five",
        "recovery-repeat",
        "probe-recovery",
        "--targets",
        "/p0/cluster/targets.json",
    )
    assert all(
        not record["changed"] and record["generation"] == 1
        for record in repeated["shards"].values()
    )
    invalid = {
        name: {
            **record,
            "session_id": "0000000000000000",
            "world_file": f"session/0000000000000000/{record['snapshot_id']:010}",
        }
        for name, record in targets.items()
    }
    (root / "five/cluster/invalid-targets.json").write_text(json.dumps(invalid))
    before = save_hashes(root / "five/cluster")
    run(
        "five",
        "recovery-invalid",
        "probe-recovery",
        "--targets",
        "/p0/cluster/invalid-targets.json",
        success=False,
    )
    assert save_hashes(root / "five/cluster") == before
    run("five", "post-recovery", "probe", "--rounds", "2")
    players = []
    for encoded in (True, False):
        label = "player" if encoded else "raw-player"
        server = root / "one/cluster/surface/server.ini"
        server.write_text(
            server.read_text().replace(
                "encode_user_path = true", f"encode_user_path = {str(encoded).lower()}"
            )
        )
        initial = run("one", label + "-63", "probe-player", "--health", "63")
        player = initial["synthetic_player"]
        assert player["health"] == 63
        assert player["encoded"] == encoded
        original_file = root / "one/cluster/surface/save" / player["file"]
        original = original_file.read_bytes()
        changed = run("one", label + "-31", "probe-player", "--health", "31")
        assert changed["synthetic_player"]["health"] == 31
        shard = initial["shards"][0]
        snapshot = initial["saved_snapshot"]
        session = shard["runtime"]["session_id"]
        target = {
            "surface": {
                "session_id": session,
                "snapshot_id": snapshot,
                "world_file": f"session/{session}/{snapshot:010}",
            }
        }
        (root / "one/cluster/targets.json").write_text(json.dumps(target))
        recovered = run(
            "one",
            label + "-recovery",
            "probe-recovery",
            "--targets",
            "/p0/cluster/targets.json",
        )
        assert recovered["shards"]["surface"]["changed"]
        assert original_file.read_bytes() == original
        assert all(
            int(path.name) <= snapshot
            for path in original_file.parent.iterdir()
            if path.name.isdigit()
        )
        restored = run("one", label + "-restored", "probe-player")
        assert restored["synthetic_player"]["health"] == 63
        assert restored["synthetic_player"]["encoded"] == encoded
        players.append({
            "encoded": encoded,
            "target_snapshot": snapshot,
            "original_sha256": hashlib.sha256(original).hexdigest(),
            "restored_health": 63,
        })
    summary = {
        "passed": True,
        "image": args.image,
        "binary_sha256": digest(binary),
        "bundle_sha256": digest(bundle),
        "normal_world_counts": [1, 2, 5],
        "normal_rounds": 2,
        "profiles": ["off", "timeline", "play"],
        "agent_saved_shards": sorted(lifecycle["saved"]),
        "recovery_fixed_targets": targets,
        "recovery_repeat_changed": False,
        "invalid_session_unchanged_files": len(before),
        "players": players,
        "actual_connected_players": 0,
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
