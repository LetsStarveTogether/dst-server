"""Describe native SDK release artifacts without requiring the SDK at build time."""

# Build/test commands pass trusted repository paths as separate arguments.
# ruff: file-ignore[implicit-namespace-package, suspicious-subprocess-import, subprocess-without-shell-equals-true, start-process-with-partial-path]
import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def revision(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def protocol(path: Path, expression: str) -> int:
    match = re.search(expression, path.read_text(encoding="utf-8"))
    if match is None:
        message = f"missing protocol declaration in {path}"
        raise ValueError(message)
    return int(match[1])


def source_digest(root: Path) -> str:
    source_files = {}
    for name in (
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "pyproject.toml",
        "Containerfile",
        "README.md",
        "LICENSE",
        "tools/build_manifest.py",
        "crates",
        "python",
        "resources",
    ):
        source = root / name
        for path in source.rglob("*") if source.is_dir() else (source,):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and not {"target", "__pycache__"}.intersection(relative.parts)
                and path.suffix not in {".so", ".pyc"}
            ):
                source_files[str(relative)] = sha256(path)
    return hashlib.sha256(
        json.dumps(source_files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--lua", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision")
    parser.add_argument("--native-scripts-revision")
    parser.add_argument("--game-version")
    parser.add_argument("--beta", action="store_true")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.game_version and not args.game_version.isascii():
        parser.error("game version must contain ASCII digits")
    if args.game_version and not args.game_version.isdigit():
        parser.error("game version must contain ASCII digits")
    root = args.root.resolve()
    lua = args.lua or root / "resources/lua"
    files = {
        str(path.relative_to(lua)): sha256(path) for path in sorted(lua.rglob("*.lua"))
    }
    if not files:
        parser.error(f"no Lua resources found in {lua}")
    cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    toolchain = tomllib.loads(
        (root / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    rustc = subprocess.run(
        ["rustc", "--version", "--verbose"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    python = (
        subprocess.run(
            [str(args.python), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if args.python
        else None
    )
    schema = root / "crates/dst-server/schema/room.capnp"
    artifacts = {}
    for path in args.artifact:
        if path.name in artifacts or path.resolve() == args.output.resolve():
            parser.error(f"duplicate artifact or manifest self-reference: {path}")
        artifacts[path.name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    manifest = {
        "format": 1,
        "sdk": {
            "version": cargo["workspace"]["package"]["version"],
            "python_version": package["project"].get("version"),
            "revision": args.revision or revision(root),
            "source_sha256": source_digest(root),
        },
        "toolchain": {
            "rust": {"channel": toolchain["toolchain"]["channel"], "compiler": rustc},
            "python": python,
        },
        "lua": {
            "sha256": hashlib.sha256(
                json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "files": files,
        },
        "protocol": {
            "room_capnp_sha256": sha256(schema),
            "lua_rpc": protocol(lua / "dst_server/rpc.lua", r"request\.v == ([0-9]+)"),
            "lua_control": protocol(
                lua / "dst_server/bootstrap.lua", r"record\.v = ([0-9]+)"
            ),
            "game_events": protocol(
                lua / "dst_server/state.lua", r"protocol = ([0-9]+)"
            ),
        },
        "native_game": {
            "steam_app_id": 343050,
            "installed_version": args.game_version or None,
            "channel": "beta" if args.beta else "release",
            "scripts_revision": args.native_scripts_revision
            or revision(root / "dst-scripts/scripts"),
        },
        "artifacts": artifacts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
