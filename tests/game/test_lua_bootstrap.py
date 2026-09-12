from pathlib import Path

import orjson
import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.models.driver import DRIVER_RECORD_ADAPTER
from tests.lua.helpers import run_lua_process

OPTIONS = {
    "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "profile": "history",
    "actions": [],
}


def run_bootstrap(scenario: str, configuration: str | None, lua_runtime: str) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/bootstrap_spec.lua",
        root,
        scenario,
        input=(configuration or "").encode(),
    )
    *lines, status = output.decode().splitlines()
    assert status == "ok"
    for line in lines:
        if line.startswith("DST_OTEL|"):
            GAME_EVENT_ADAPTER.validate_json(
                line.removeprefix("DST_OTEL|"), strict=True
            )
            continue
        assert line.startswith("DST_DRIVER|")
        payload = line.removeprefix("DST_DRIVER|")
        DRIVER_RECORD_ADAPTER.validate_json(payload, strict=True)
        record = orjson.loads(payload)
        assert record["nonce"] in {None, OPTIONS["nonce"]}
        assert set(record) in (
            {"nonce", "generation"},
            {"nonce", "health"},
            {"nonce", "generation", "error"},
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "active",
        "off",
        "native_results",
        "invalid_generation",
        "component_failure",
        "installation_failure",
        "optional_failure",
        "votes_install_failure",
        "connections_install_failure",
        "connections_start_failure",
        "publication_failure",
        "delayed_configuration",
        "configuration_read_failure",
    ],
)
def test_lua_bootstrap(scenario: str, lua_runtime: str) -> None:
    options = OPTIONS | {"profile": "off" if scenario == "off" else "history"}
    run_bootstrap(scenario, orjson.dumps(options).decode(), lua_runtime)


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        "SECRET_TOKEN private chat",
        "null",
        "[]",
        "{}",
        orjson.dumps(OPTIONS | {"nonce": "SECRET_TOKEN private chat"}).decode(),
        orjson.dumps(OPTIONS | {"profile": "SECRET_TOKEN private chat"}).decode(),
        orjson.dumps(OPTIONS | {"actions": {"action": "CHOP"}}).decode(),
        orjson.dumps(OPTIONS | {"actions": [False]}).decode(),
        orjson.dumps(OPTIONS | {"actions": [""]}).decode(),
        orjson.dumps(OPTIONS).decode() + " SECRET_TOKEN private chat",
    ],
)
def test_lua_bootstrap_rejects_invalid_configuration(
    configuration: str | None, lua_runtime: str
) -> None:
    run_bootstrap("configuration_failure", configuration, lua_runtime)
