"""Native game callbacks, driver behavior and typed configuration contracts."""

# The SDK executable and Lua fixtures are trusted repository build outputs.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "fixture",
    [
        "bootstrap_transport",
        "driver",
        "rust_recovery",
        "rust_load",
        "rust_player_guard",
        "voting",
    ],
)
def test_native_contract(
    lua: Callable[..., bytes], root: Path, scripts: Path, fixture: str
) -> None:
    output = lua(root / f"tests/lua/{fixture}_spec.lua", root, scripts)
    assert output.splitlines()[-1] == b"ok"


@pytest.mark.parametrize(
    "scenario",
    [
        "off",
        "delayed_configuration",
        "connections_install_failure",
        "optional_failure",
        "write_before",
        "write_after",
    ],
)
def test_runtime_publishes_ready(
    lua: Callable[..., bytes], root: Path, scripts: Path, scenario: str
) -> None:
    output = lua(root / "tests/lua/bootstrap_spec.lua", root, scripts, scenario)
    assert output.splitlines()[-1] == b"ok"


@pytest.mark.parametrize(
    "scenario",
    [
        "default",
        "load",
        "catalog",
        "invalid",
        "observer_failure",
        "recovery_observer_failure",
    ],
)
def test_bootstrap(
    lua: Callable[..., bytes], root: Path, scripts: Path, scenario: str
) -> None:
    output = lua(root / "tests/lua/rust_bootstrap_spec.lua", root, scripts, scenario)
    assert output.strip() == b"ok"


@pytest.mark.parametrize("fixture", ["event_contract", "messages_spec"])
@pytest.mark.parametrize("profile", ["off", "timeline", "play"])
def test_event_profiles(
    lua: Callable[..., bytes], root: Path, scripts: Path, fixture: str, profile: str
) -> None:
    lua(root / f"tests/lua/{fixture}.lua", root, scripts, profile)


def test_gameplay_burst(lua: Callable[..., bytes], root: Path, scripts: Path) -> None:
    assert (
        lua(root / "tests/lua/event_contract.lua", root, scripts, "play", "burst")
        == b"ok\n"
    )


@pytest.mark.parametrize("scenario", ["complete", "secondary"])
def test_votes(
    lua: Callable[..., bytes], root: Path, scripts: Path, scenario: str
) -> None:
    lua(root / "tests/lua/votes_spec.lua", root, scripts, scenario)


@pytest.mark.parametrize("contract", ["rpc", "announcement"])
def test_game_contracts(
    lua: Callable[..., bytes], root: Path, scripts: Path, contract: str
) -> None:
    lua(
        root / f"tests/lua/{contract}_contract.lua",
        root / "resources/lua",
        root / "tests/lua",
        scripts,
    )


def test_vine_rescue(lua: Callable[..., bytes], root: Path, scripts: Path) -> None:
    assert lua(root / "tests/lua/lifecycle_contract.lua", root, scripts) == b"ok\n"


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
