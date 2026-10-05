"""Native game callbacks, driver behavior and typed configuration contracts."""

# The SDK executable and Lua fixtures are trusted repository build outputs.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


def test_driver(
    lua: Callable[..., bytes], root: Path, scripts: Path, driver_scenario: str
) -> None:
    arguments = {
        "print_boundary": (65536, "source"),
        "print_mixed": ("source", "print", "log_error_event"),
    }.get(driver_scenario, ())
    result = lua(
        root / "tests/lua/driver_spec.lua", root, driver_scenario, scripts, *arguments
    )
    assert result.splitlines()[-1] == b"ok"


@pytest.mark.parametrize(
    "fixture",
    ["rust_recovery", "rust_load", "rust_player_guard"],
)
def test_native_lifecycle(
    lua: Callable[..., bytes], root: Path, scripts: Path, fixture: str
) -> None:
    output = lua(root / f"tests/lua/{fixture}_spec.lua", root, scripts)
    assert output.splitlines()[-1] == b"ok"


@pytest.mark.parametrize("scenario", ["default", "load", "catalog", "invalid"])
def test_bootstrap(
    lua: Callable[..., bytes], root: Path, scripts: Path, scenario: str
) -> None:
    output = lua(root / "tests/lua/rust_bootstrap_spec.lua", root, scripts, scenario)
    assert output.strip() == b"ok"


@pytest.mark.parametrize(
    "fixture",
    [
        "input_events_spec",
        "gameplay_events_spec",
        "messages_spec",
        "lifecycle_contract",
    ],
)
@pytest.mark.parametrize("profile", ["off", "critical", "history"])
def test_event_profiles(
    lua: Callable[..., bytes], root: Path, scripts: Path, fixture: str, profile: str
) -> None:
    lua(root / f"tests/lua/{fixture}.lua", root, scripts, profile)


@pytest.mark.parametrize(
    "scenario",
    [
        "critical",
        "history",
        "off",
        "optional_failure",
        "snapshot_failure",
        "invalid_authentication",
        "authentication_output_failure",
    ],
)
def test_connections(
    lua: Callable[..., bytes], root: Path, scripts: Path, scenario: str
) -> None:
    lua(root / "tests/lua/connections_spec.lua", root, scripts, scenario)


@pytest.mark.parametrize(
    "scenario",
    ["passed", "failed", "cancelled", "custom", "secondary", "error", "capture_error"],
)
def test_votes(
    lua: Callable[..., bytes], root: Path, scripts: Path, scenario: str
) -> None:
    lua(root / "tests/lua/votes_spec.lua", root, scripts, scenario)


@pytest.mark.parametrize("mode", ["forge", "gorge", "forge_no", "forge_tie"])
def test_mode_votes(
    lua: Callable[..., bytes], root: Path, scripts: Path, mode: str
) -> None:
    lua(root / "tests/lua/mode_votes_spec.lua", root, scripts, mode)


@pytest.mark.parametrize("contract", ["event", "rpc", "announcement"])
def test_game_contracts(
    lua: Callable[..., bytes], root: Path, scripts: Path, contract: str
) -> None:
    lua(
        root / f"tests/lua/{contract}_contract.lua",
        root / "resources/lua",
        root / "tests/lua",
        scripts,
    )


def test_settings_match_native_customization(
    lua: Callable[..., bytes], root: Path, scripts: Path, binary: Path
) -> None:
    contract = json.loads(lua(root / "tests/lua/customize_contract.lua", scripts))

    def validate(model: str, values: dict, *, valid: bool = True) -> dict:
        result = subprocess.run(
            [str(binary), "config", "validate", model, "-", "--resolved"],
            input=json.dumps(values).encode(),
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert (result.returncode == 0) is valid, (model, values, result.stderr)
        return json.loads(result.stdout) if valid else {}

    for location, model in [("forest", "ForestOverrides"), ("cave", "CaveOverrides")]:
        defaults = validate(model, {})
        validate(model, defaults)
        options = contract["options"][location]
        assert options.keys() | contract["misc"].keys() <= defaults.keys()
        for name, native in options.items():
            assert defaults[name] == native["default"], (location, name)
            validate(model, {name: "__unsupported__"}, valid=False)
        # Exercise every native choice through the public Rust parser.
        for index in range(max(len(native["values"]) for native in options.values())):
            validate(
                model,
                {
                    name: native["values"][index % len(native["values"])]
                    for name, native in options.items()
                },
            )
        for name, native in contract["misc"].items():
            assert defaults[name] == native[location]["default"], (location, name)
            validate(model, {name: native["forest"]["default"]})
            validate(model, {name: None}, valid=native[location]["kind"] == "nil")
            validate(model, {name: {"unexpected": True}}, valid=False)
        for native in contract["misc"]["layout_mode"].values():
            validate(model, {"layout_mode": native["default"]})
