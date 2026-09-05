import json
from enum import IntEnum, StrEnum
from typing import Any

import pytest
from pydantic import JsonValue, TypeAdapter

from dst_server.game import Emoji, Emote, EmoteType
from dst_server.game.rpc import lua_value
from tests.helpers import run_lua


def lua_json(source: str, luajit: str) -> Any:
    return json.loads(run_lua(source, luajit, driver_path=False))


def test_game_enums_match_all_native_definitions(luajit: str) -> None:
    native = lua_json(
        """
        require("emoji_items")
        require("emote_items")
        FRAMES = 1 / 30
        COMMAND_PERMISSION = { USER = 0 }
        local commands = {}
        AddUserCommand = function(name, definition) commands[name] = definition end
        require("emotes")

        local emojis, emotes = {}, {}
        for item_type, definition in pairs(EMOJI_ITEMS) do
            assert(item_type == definition.data.item_type)
            emojis[string.upper(definition.input_name)] = {
                value = definition.data.utf8_str,
                item_type = item_type,
                chat_token = ":" .. definition.input_name .. ":",
            }
        end
        local function add_emote(name, category)
            local command = assert(commands[name])
            assert(command.emote and command.slash)
            emotes[string.upper(name)] = {
                value = name,
                category = category,
                item_type = command.requires_item_type or json.null,
                aliases = command.aliases or { n = 0 },
                slash_command = "/" .. name,
            }
        end
        for name, definition in pairs(GetCommonEmotes()) do
            add_emote(name, definition.type)
        end
        -- widgets/controls.lua places account emotes in the UNLOCKABLE wheel.
        for _, definition in pairs(EMOTE_ITEMS) do
            add_emote(definition.cmd_name, EMOTE_TYPE.UNLOCKABLE)
        end
        io.write(json.encode_compliant({
            emojis = emojis, emotes = emotes, categories = EMOTE_TYPE,
        }))
        """,
        luajit,
    )

    assert issubclass(Emoji, StrEnum)
    assert issubclass(Emote, StrEnum)
    assert issubclass(EmoteType, IntEnum)
    assert {name: item.value for name, item in EmoteType.__members__.items()} == (
        native["categories"]
    )
    assert {
        name: {
            "value": item.value,
            "item_type": item.item_type,
            "chat_token": item.chat_token,
        }
        for name, item in Emoji.__members__.items()
    } == native["emojis"]
    assert {
        name: {
            "value": item.value,
            "category": item.category,
            "item_type": item.item_type,
            "aliases": list(item.aliases),
            "slash_command": item.slash_command,
        }
        for name, item in Emote.__members__.items()
    } == native["emotes"]
    assert all(
        isinstance(item.category, EmoteType) and isinstance(item.aliases, tuple)
        for item in Emote
    )


@pytest.mark.parametrize(
    ("member", "value", "invalid_values"),
    [
        pytest.param(
            Emoji.BEEFALO,
            "\U000f0001",
            ("beefalo", ":beefalo:", "🦬", "unknown"),
            id="emoji",
        ),
        pytest.param(Emote.WAVE, "wave", ("/wave", "hi", "unknown"), id="emote"),
        pytest.param(EmoteType.EMOTION, 0, (-1, 3, "EMOTION"), id="category"),
    ],
)
def test_game_enum_scalars_round_trip_without_implicit_aliases(
    member: Emoji | Emote | EmoteType,
    value: str | int,
    invalid_values: tuple[str | int, ...],
    luajit: str,
) -> None:
    enum = type(member)
    adapter = TypeAdapter(enum)

    assert enum(value) is member
    assert enum[member.name] is member
    assert str(member) == str(value)
    assert json.loads(json.dumps(member)) == value
    assert json.loads(adapter.dump_json(member)) == value
    assert adapter.validate_json(json.dumps(value), strict=True) is member
    assert TypeAdapter(JsonValue).validate_python(member) == value
    assert (
        lua_json(f"io.write(json.encode_compliant({lua_value(member)}))", luajit)
        == value
    )
    for invalid in invalid_values:
        with pytest.raises(ValueError, match=f"is not a valid {enum.__name__}"):
            enum(invalid)
    with pytest.raises(KeyError, match="NOT_NATIVE"):
        enum["NOT_NATIVE"]
