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
from pathlib import Path
from typing import Any

from prepare_probe import prepare


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_hashes(cluster: Path) -> dict[str, str]:
    return {
        str(path.relative_to(cluster)): digest(path)
        for path in cluster.rglob("*")
        if path.is_file() and "save" in path.relative_to(cluster).parts
    }


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

    def run(
        room: str, label: str, command: str, *arguments: str, success: bool = True
    ) -> dict[str, Any] | None:
        fixture = root / room
        cluster = fixture / "cluster"
        name = f"dst-native-{os.getpid()}-{room}"
        command_line = [
            "podman",
            "run",
            "--rm",
            "--init",
            "--pull=never",
            "--network",
            "none",
            "--name",
            name,
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
            # This unique test container may still exist after a runner timeout.
            subprocess.run(
                ["podman", "rm", "--force", "--ignore", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    for room, count in (("one", 1), ("two", 2), ("five", 5)):
        prepare(root / room, count)
        report = run(room, "normal", "probe", "--rounds", "2")
        assert report["passed"]
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
        "recovery_fixed_targets": targets,
        "recovery_repeat_changed": False,
        "invalid_session_unchanged_files": len(before),
        "players": players,
        "actual_connected_players": 0,
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
