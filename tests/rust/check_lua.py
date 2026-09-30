"""Run the preserved Lua contracts under both supported native interpreters."""

# Build/test commands pass trusted repository paths as separate arguments.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print]
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

NONCE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
IDENTIFIER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


def invoke(
    runtime: str, fixture: Path, *arguments: object, source: bytes = b""
) -> bytes:
    result = subprocess.run(
        [runtime, str(fixture), *(str(argument) for argument in arguments)],
        input=source,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        message = (
            f"{runtime} {fixture.name} {arguments}:\n"
            f"{result.stderr.decode(errors='replace')}"
        )
        raise AssertionError(message)
    for line in result.stdout.splitlines():
        if line.startswith(b"DST_OTEL|"):
            record = json.loads(line.removeprefix(b"DST_OTEL|"))
            assert record["v"] == 3
            assert isinstance(record["data"], dict)
            assert isinstance(record["seq"], int)
    return result.stdout


def check_driver(runtime: str, root: Path, scripts: Path) -> int:
    fixture = root / "tests/lua/driver_spec.lua"
    scenarios = re.findall(
        r"^function scenarios\.([a-z_]+)\(\)", fixture.read_text(), re.MULTILINE
    )
    for scenario in scenarios:
        arguments = (
            (65536, "source")
            if scenario == "print_boundary"
            else ("source", "print", "log_error_event")
            if scenario == "print_mixed"
            else ()
        )
        result = invoke(runtime, fixture, root, scenario, scripts, *arguments)
        assert result.splitlines()[-1] == b"ok"
    return len(scenarios)


def check_rpc(runtime: str, root: Path, scripts: Path) -> int:
    fixture = root / "tests/lua/rpc_spec.lua"
    request = {
        "v": 1,
        "nonce": NONCE,
        "id": IDENTIFIER,
        "generation": 7,
        "method": "ping",
        "arguments": {},
    }
    count = 0

    def encode(value: object) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode()

    def send(
        changes: dict[str, object] | None = None,
        *,
        scenario: str = "ready",
        payload: bytes | None = None,
    ) -> list[dict[str, object]]:
        nonlocal count
        output = invoke(
            runtime,
            fixture,
            root,
            scenario,
            scripts,
            source=b"DST_RPC|"
            + (encode(request | (changes or {})) if payload is None else payload)
            + b"\n",
        )
        records = []
        for line in output.splitlines(keepends=True):
            assert line.startswith(b"DST_RPC|")
            assert len(line) <= 65536
            assert b"SECRET_TOKEN" not in line
            assert b"private chat" not in line
            records.append(json.loads(line.removeprefix(b"DST_RPC|")))
        count += 1
        return records

    header = {"v": 1, "nonce": NONCE, "id": IDENTIFIER, "generation": 7}
    for scenario in ("ready", "changed_install", "noise"):
        records = send(
            {"method": "noise" if scenario == "noise" else "ping"}, scenario=scenario
        )
        assert records == [
            header | {"accepted": True},
            header | {"result": {"ok": True, "data": True}},
        ]
    assert send({"method": "save"})[-1]["result"] == {"ok": True, "data": True}
    assert invoke(runtime, fixture, root, "native", scripts) == b""
    count += 1
    for changes, error in (
        ({"v": 3}, "invalid_request"),
        ({"nonce": IDENTIFIER}, "invalid_request"),
        ({"id": "SECRET_TOKEN private chat"}, "invalid_request"),
        ({"generation": True}, "invalid_request"),
        ({"generation": 0.5}, "invalid_request"),
        ({"generation": 2**53}, "invalid_request"),
        ({"generation": 6}, "stale_generation"),
        ({"generation": 8}, "stale_generation"),
        ({"method": ""}, "invalid_request"),
        ({"method": "return os.execute('private chat')"}, "invalid_request"),
        ({"arguments": []}, "invalid_request"),
        ({"arguments": None}, "invalid_request"),
        ({"extra": "SECRET_TOKEN private chat"}, "invalid_request"),
    ):
        records = send(changes)
        assert len(records) == 1
        assert "accepted" not in records[0]
        assert records[0]["result"] == {"ok": False, "error": error}
    for payload in (
        b"null",
        b"[]",
        b"{}",
        b"SECRET_TOKEN private chat",
        b'{"v":1+2}',
        b"{'v':1}",
        b'{"v":1,/* comment */"id":2}',
        b'{"v":1,"v":1}',
        b'{"v":null,"v":1}',
        b'{"v":1,}',
        b'{"v":01}',
        b'{"v":+1}',
        b'{"v":1.}',
        b'{"v":1e}',
        b'{"v":1e999}',
        b'{"v":NaN}',
        rb'{"v":"\x41"}',
        rb'{"v":"\ud800"}',
        rb'{"v":"\udc00"}',
        rb'{"v":"\ud800\u0000"}',
        b'{"v":"\xff"}',
        b'{"v":"\x00"}',
        b'{"v":true false}',
        b'{"v":1} trailing',
        b"[" * 65 + b"0" + b"]" * 65,
    ):
        records = send(payload=payload)
        assert len(records) == 1
        assert records[0]["id"] is None
        assert records[0]["result"] == {"ok": False, "error": "invalid_request"}
    for method, error in (
        ("throw", "lua_error"),
        ("invalid_utf8", "invalid_utf8"),
        ("invalid_value", "invalid_json_value"),
        ("indeterminate", "indeterminate"),
    ):
        accepted, result = send({"method": method})
        assert accepted["accepted"] is True
        assert result["result"] == {"ok": False, "error": error}
    values = {
        "null": None,
        "empty": {},
        "array": [None, {}, [], False, 0, "", 0.25],
        "text": '你好👩🏽‍💻\x00\n\t"\\',
        "number": -0.25e3,
    }
    assert send({"method": "echo", "arguments": values})[-1]["result"] == {
        "ok": True,
        "data": values,
    }
    assert send({"method": "nothing"})[-1]["result"] == {"ok": True, "data": None}
    empty = header | {"result": {"ok": True, "data": ""}}
    response_size = 65536 - len(b"DST_RPC|" + encode(empty) + b"\n")
    request_size = 4096 - len(
        b"DST_RPC|" + encode(request | {"arguments": {"pad": ""}}) + b"\n"
    )
    for excess in (0, 1):
        assert send({"method": "large", "arguments": {"size": response_size + excess}})[
            -1
        ]["result"] == (
            {"ok": False, "error": "response_too_large"}
            if excess
            else {"ok": True, "data": "x" * response_size}
        )
        assert send({"arguments": {"pad": "x" * (request_size + excess)}})[-1][
            "result"
        ] == (
            {"ok": False, "error": "invalid_request"}
            if excess
            else {"ok": True, "data": True}
        )
    for scenario, error in (("not_ready", "not_ready"), ("write_error", "lua_error")):
        records = send(scenario=scenario)
        assert len(records) == 1
        assert records[0]["result"] == {"ok": False, "error": error}
    return count


def check_customize(runtime: str, root: Path, scripts: Path) -> None:
    contract = json.loads(
        invoke(runtime, root / "tests/lua/customize_contract.lua", scripts)
    )
    schemas = json.loads(
        (root / "crates/dst-server/resources/settings/schemas.json").read_text()
    )

    def definition(schema: dict, value: dict) -> dict:
        if "$ref" in value:
            return schema["$defs"][value["$ref"].removeprefix("#/$defs/")]
        return value

    for location, model in (("forest", "ForestOverrides"), ("cave", "CaveOverrides")):
        schema = schemas[model]
        properties = schema["properties"]
        options = contract["options"][location]
        assert options.keys() | contract["misc"].keys() <= properties.keys()
        for name, native in options.items():
            field = properties[name]
            allowed = definition(schema, field)
            assert field["default"] == native["default"], (location, name)
            assert set(allowed.get("enum", [allowed.get("const")])) == set(
                native["values"]
            ), (location, name)
        for name, native in contract["misc"].items():
            field = properties[name]
            expected = {native["forest"]["kind"]}
            if native[location]["kind"] == "nil":
                expected.add("null")
            allowed = definition(schema, field)
            actual = {
                definition(schema, variant)["type"]
                for variant in allowed.get("anyOf", [allowed])
            }
            assert actual == expected, (location, name)
            assert field["default"] == native[location]["default"], (location, name)
        assert set(definition(schema, properties["layout_mode"])["enum"]) == {
            native["default"] for native in contract["misc"]["layout_mode"].values()
        }


def check(runtime: str, root: Path, scripts: Path, lua: Path) -> int:
    fixtures = root / "tests/lua"
    check_customize(runtime, root, scripts)
    count = 1 + check_driver(runtime, root, scripts) + check_rpc(runtime, root, scripts)
    for fixture in (
        "rust_recovery_spec.lua",
        "rust_save_spec.lua",
        "rust_load_spec.lua",
        "rust_player_guard_spec.lua",
    ):
        assert (
            invoke(runtime, fixtures / fixture, root, scripts).splitlines()[-1] == b"ok"
        )
        count += 1
    for scenario in ("default", "save", "catalog", "invalid"):
        assert (
            invoke(
                runtime, fixtures / "rust_bootstrap_spec.lua", root, scripts, scenario
            ).strip()
            == b"ok"
        )
        count += 1
    for fixture in (
        "input_events_spec.lua",
        "gameplay_events_spec.lua",
        "messages_spec.lua",
        "lifecycle_contract.lua",
    ):
        for profile in ("off", "critical", "history"):
            invoke(runtime, fixtures / fixture, root, scripts, profile)
            count += 1
    for scenario in (
        "critical",
        "history",
        "off",
        "optional_failure",
        "snapshot_failure",
        "invalid_authentication",
        "authentication_output_failure",
    ):
        invoke(runtime, fixtures / "connections_spec.lua", root, scripts, scenario)
        count += 1
    for scenario in (
        "passed",
        "failed",
        "cancelled",
        "custom",
        "secondary",
        "error",
        "capture_error",
    ):
        invoke(runtime, fixtures / "votes_spec.lua", root, scripts, scenario)
        count += 1
    for mode in ("forge", "gorge", "forge_no", "forge_tie"):
        invoke(runtime, fixtures / "mode_votes_spec.lua", root, scripts, mode)
        count += 1
    for fixture in (
        "event_contract.lua",
        "rpc_contract.lua",
        "announcement_contract.lua",
    ):
        invoke(runtime, fixtures / fixture, lua, fixtures, scripts)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--scripts", type=Path)
    parser.add_argument("--lua", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    scripts = (args.scripts or root / "dst-scripts/scripts").resolve()
    lua = (args.lua or root / "resources/lua").resolve()
    for runtime in ("lua5.1", "luajit"):
        executable = shutil.which(runtime)
        if executable is None:
            parser.error(f"{runtime} is required")
        count = check(executable, root, scripts, lua)
        print(f"{runtime}: {count} Lua contracts passed")


if __name__ == "__main__":
    main()
