import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, JsonValue, SecretStr, ValidationError
from ulid import ULID

from dst_server.cluster.config import (
    ClusterConfig,
    ClusterSettings,
    LevelDataOverride,
    ShardConfig,
    ShardSettings,
)
from dst_server.cluster.overrides import (
    CustomWorldOverrides,
    ModOverride,
    ModOverrides,
    WorldgenOverride,
)
from dst_server.cluster.world import ForestOverrides, WorldOverrides
from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.events.server import SavedEvent
from dst_server.rpc.codec import (
    _payload_contract,
    decode_json_value,
    decode_model,
    encode_json_value,
    encode_model,
)
from dst_server.rpc.models import GameEventRecord, LifecycleRecord
from dst_server.rpc.schema import SCHEMA_FINGERPRINT, _schema_fingerprint, load_schema


class ModdedWorldOverrides(WorldOverrides):
    mod_option: Literal["hard"]


def round_trip[ModelT: BaseModel](value: ModelT) -> ModelT:
    return decode_model(type(value), encode_model(value))


def test_rpc_schema_loads_and_has_a_stable_fingerprint() -> None:
    assert len(SCHEMA_FINGERPRINT) == 64
    payload = _payload_contract()
    assert json.loads(payload)["wireFormat"] == 1
    assert _schema_fingerprint(payload) == SCHEMA_FINGERPRINT
    assert _schema_fingerprint(payload + b"changed") != SCHEMA_FINGERPRINT
    assert load_schema().Cluster is not None
    assert load_schema().Subscription is not None


def test_cluster_configuration_round_trip_preserves_presence_and_secrets() -> None:
    configuration = ClusterConfig(
        settings=ClusterSettings(
            cluster_name="RPC room",
            cluster_password=SecretStr("secret"),
            max_players=6,
            override_dns=None,
            pvp=False,
        ),
        shards={
            "Master": ShardConfig(
                settings=ShardSettings(server_port=10999),
                world=WorldgenOverride.forest(
                    overrides=ForestOverrides(day="onlynight", has_ocean=True)
                ),
                mods=ModOverrides(
                    entries={
                        "workshop-1": ModOverride(
                            enabled=True,
                            configuration_options={
                                "$world": {"value": "ordinary MOD data"},
                                "world_like": ["forest", {"day": "onlynight"}],
                            },
                        )
                    }
                ),
            )
        },
        token=SecretStr("pds-g^rpc-token"),
        adminlist="KU_ADMIN\n",
    )

    restored = round_trip(configuration)

    assert restored == configuration
    assert (
        restored.model_fields_set,
        restored.settings.model_fields_set,
        restored.shards["Master"].settings.model_fields_set,
    ) == (
        configuration.model_fields_set,
        configuration.settings.model_fields_set,
        configuration.shards["Master"].settings.model_fields_set,
    )
    assert restored.settings.cluster_password is not None
    assert restored.settings.cluster_password.get_secret_value() == "secret"
    assert restored.token.get_secret_value() == "pds-g^rpc-token"


@pytest.mark.parametrize(
    ("value", "valid"),
    [(0, True), (2**63, True), (2**64 - 1, True), (-1, False), (2**64, False)],
)
def test_uint64_model_boundaries_survive_json(value: int, valid: bool) -> None:
    if not valid:
        with pytest.raises(ValidationError):
            ClusterSettings(steam_group_id=value)
        return
    settings = ClusterSettings(steam_group_id=value)
    assert round_trip(settings).steam_group_id == value


@pytest.mark.parametrize(
    ("location", "identifier", "task_set", "field", "value"),
    [
        ("forest", "SURVIVAL_TOGETHER", "default", "islands", "always"),
        ("cave", "DST_CAVE", "cave_default", "roads", "never"),
    ],
)
def test_loaded_world_override_discriminator_round_trip(
    tmp_path: Path,
    location: str,
    identifier: str,
    task_set: str,
    field: str,
    value: str,
) -> None:
    path = tmp_path / f"{location}.lua"
    path.write_text(
        "return {\n"
        f'  id = "{identifier}", name = "{location}", desc = "",\n'
        f'  location = "{location}",\n'
        f'  overrides = {{ task_set = "{task_set}", {field} = "{value}" }},\n'
        "}\n"
    )
    loaded = LevelDataOverride.load(path)

    restored = round_trip(loaded)

    assert restored == loaded
    assert type(restored.overrides) is type(loaded.overrides)
    assert getattr(restored.overrides, field) == value


def test_custom_world_and_event_discriminators_round_trip() -> None:
    configuration = ClusterConfig(
        shards={
            "Master": ShardConfig(
                settings=ShardSettings(),
                world=WorldgenOverride(
                    worldgen_preset="MOD_WORLD",
                    overrides=CustomWorldOverrides(mod_mode="hard", count=2),
                ),
            )
        }
    )
    restored = round_trip(configuration)
    world = restored.shards["Master"].world
    assert restored == configuration
    assert world is not None
    assert isinstance(world.overrides, CustomWorldOverrides)

    event = GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 1,
            "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "seq": 1,
            "event": "dst.world.state_changed",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {"name": "cycles", "value": 2},
        },
        strict=True,
    )
    attempt = ULID()
    records = (
        GameEventRecord(
            shard="Master",
            game_attempt=attempt,
            sequence=1,
            observed_timestamp_ns=2,
            event=event,
        ),
        LifecycleRecord(
            shard="Master",
            game_attempt=attempt,
            sequence=3,
            observed_timestamp_ns=4,
            event=SavedEvent(path="session/1", snapshot=1),
        ),
    )
    assert tuple(round_trip(record) for record in records) == records


def test_codec_rejects_unregistered_world_override_subclasses() -> None:
    value = ModdedWorldOverrides(mod_option="hard")
    with pytest.raises(TypeError, match="unsupported RPC world override type"):
        encode_model(value)


def test_world_discriminator_is_stable_and_checked() -> None:
    encoded = json.loads(encode_model(ForestOverrides(day="onlynight")))
    assert encoded == ["forest", {"day": "onlynight"}]
    with pytest.raises(ValueError, match="unknown RPC world override type"):
        decode_model(WorldOverrides, b'["future",{}]')
    with pytest.raises(ValueError, match="is not ForestOverrides"):
        decode_model(ForestOverrides, b'["cave",{}]')


@pytest.mark.parametrize(
    "value",
    [None, [], {}, [1, {"value": None}], {"nested": ["x"]}],
)
def test_json_values_round_trip(value: JsonValue) -> None:
    assert decode_json_value(encode_json_value(value)) == value


@pytest.mark.parametrize(
    "payload",
    [b'{"key":1,"key":2}', b'{"nested":{"key":1,"key":2}}'],
)
def test_codec_rejects_duplicate_json_keys(payload: bytes) -> None:
    with pytest.raises(ValueError, match="duplicate RPC JSON object key"):
        decode_json_value(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_codec_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        encode_json_value(value)
