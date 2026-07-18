from __future__ import annotations

import json

from dst_server import Server, ServerArgs

FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
import re
import signal
import stat

assert all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (3, 4, 5))
commands = os.fdopen(3)
results = os.fdopen(4, "w", buffering=1)
events = os.fdopen(5, "w", buffering=1)
events.write("DST_SessionId|TEST\n")

def stop(_signum, _frame):
    events.write("DST_Shutdown\nDST_Saved|session/TEST/1\nDST_Stopping\n")

signal.signal(signal.SIGTERM, stop)
busy = True
for command in commands:
    if busy:
        busy = False
        results.write("DST_LuaBusy\n")
        continue
    if "driver.install" in command:
        match = re.search(r'\\"nonce\\":\\"([^"\\]+)', command)
        assert match is not None
        nonce = match.group(1)
        health = {
            "protocol": 1,
            "installed": True,
            "profile": "history",
            "events_emitted": 0,
            "errors": 0,
            "players": 0,
            "action_hook": True,
            "shard_hook": True,
        }
        results.write(
            "DST_SERVER_RESULT|"
            + json.dumps({"ok": True, "data": health})
            + "\nDST_RemoteCommandDone\n"
        )
        event = {
            "v": 1,
            "nonce": nonce,
            "seq": 1,
            "event": "dst.world.state_changed",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {"name": "cycles", "value": 2},
        }
        print("[00:00:01]: DST_OTEL|" + json.dumps(event), flush=True)
        continue
    print("command received", flush=True)
    event = {
        "v": 1,
        "nonce": nonce,
        "seq": 2,
        "event": "dst.entity.death",
        "tick": 11,
        "monotonic_ms": 21,
        "cycle": 2,
        "data": {
            "victim": {
                "prefab": "rabbit",
                "guid": 42,
                "userid": None,
                "position": None,
            },
            "cause": None,
            "afflicter": None,
            "attributed_player": None,
            "corpsing": False,
            "caused_by_action_sequence": None,
        },
    }
    results.write("DST_OTEL|" + json.dumps(event) + "\n")
    results.write(f"result:{command.strip()}\nDST_RemoteCommandDone\n")
"""


def structured_result(data: object) -> str:
    return "unrelated print\nDST_SERVER_RESULT|" + json.dumps({
        "ok": True,
        "data": data,
    })


def room_data() -> dict[str, object]:
    return {
        "name": "Test Room",
        "description": "Description",
        "game_mode": "survival",
        "playstyle": "social",
        "max_players": 6,
        "player_count": 1,
        "pvp": False,
        "is_paused": False,
        "has_password": True,
        "is_dedicated": True,
        "is_online": True,
        "lan_only": False,
        "friends_only": False,
        "mods_enabled": True,
        "clan_id": "",
        "clan_only": False,
        "shard_id": "1",
        "is_master_shard": True,
    }


def world_data() -> dict[str, object]:
    return {
        "age": 11.5,
        "cycles": 10,
        "day": 11,
        "time": 0.5,
        "time_in_phase": 0.25,
        "phase": "day",
        "is_day": True,
        "is_dusk": False,
        "is_night": False,
        "moon_phase": "new",
        "is_waxing_moon": True,
        "is_full_moon": False,
        "is_new_moon": False,
        "season": "autumn",
        "is_spring": False,
        "is_summer": False,
        "is_autumn": True,
        "is_winter": False,
        "elapsed_days_in_season": 2,
        "season_progress": 0.2,
        "remaining_days_in_season": 8,
        "spring_length": 20,
        "summer_length": 15,
        "autumn_length": 20,
        "winter_length": 15,
        "temperature": 20.5,
        "moisture": 0,
        "moisture_ceiling": 480,
        "precipitation_probability": 0,
        "precipitation_rate": 0,
        "precipitation": "none",
        "is_raining": False,
        "is_snowing": False,
        "is_lunar_hailing": False,
        "is_acid_raining": False,
        "is_snow_covered": False,
        "snow_level": 0,
        "lunar_hail_level": 42,
        "lunar_hail_rate": 0,
        "wetness": 73,
        "is_wet": False,
        "is_cave": False,
    }


def player_data() -> dict[str, object]:
    return {
        "userid": "KU_TEST",
        "name": "Player",
        "prefab": "wilson",
        "admin": True,
        "moderator": False,
        "is_ghost": False,
        "position": {"x": 1, "y": 0, "z": 2},
        "age": {"seconds": 480.5, "days": 1, "display_days": 2},
        "vitals": {
            "health": {
                "current": 120,
                "maximum": 150,
                "percent": 0.8,
                "is_dead": False,
                "is_invincible": False,
            },
            "hunger": {"current": 105, "maximum": 150, "percent": 0.7},
            "sanity": {"current": 120, "maximum": 200, "percent": 0.6},
            "temperature": {"current": 25, "maximum": 70},
            "moisture": {"current": 10, "maximum": 100, "percent": 0.1},
        },
        "state": {
            "network_score": 0,
            "combat_target": {"prefab": "hound", "guid": 50},
            "weapon": {"prefab": "spear", "guid": 51},
            "mount": None,
            "follower_count": 1,
            "followers": [{"prefab": "chester", "guid": 52}],
            "skill_xp": 15,
            "available_skill_points": 2,
            "activated_skills": ["wilson_torch_1"],
        },
    }


def item_data(prefab: str = "twigs") -> dict[str, object]:
    return {
        "prefab": prefab,
        "guid": 100,
        "skin": None,
        "stack_size": 12,
        "moisture_percent": 0,
        "uses_percent": None,
        "freshness_percent": None,
        "fuel_percent": None,
        "armor_percent": None,
        "charge_percent": 1,
    }


def runtime_data() -> dict[str, object]:
    return {
        "session_id": "0123456789ABCDEF",
        "snapshot": 26,
        "build_version": "740477",
        "save_version": 5.23,
        "generated_on_save_version": 5.23,
        "seed": 704793166,
        "level_id": "SURVIVAL_TOGETHER",
        "branch": "release",
        "app_version": "740477",
        "shard_id": "1",
        "is_master_shard": True,
        "is_cave": False,
    }


class StubServer(Server):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.commands: list[str] = []
        super().__init__(ServerArgs(shard="test"))

    async def execute(self, command: str) -> str:
        self.commands.append(command)
        return self.responses.pop(0)
