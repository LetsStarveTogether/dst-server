import asyncio
import sys
from collections import deque
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import orjson
import pytest

from dst_server import commands as c
from dst_server.archive import export_cluster
from dst_server.errors import (
    IndeterminateCommandError,
)
from dst_server.events import player
from dst_server.game.rpc import LuaRequestError
from dst_server.klei_id import encode_klei_id
from dst_server.lua_codec import lua_string
from dst_server.models.cluster import (
    GameEventRecord,
    LifecycleRecord,
)
from dst_server.rooms import Room, RoomStore
from dst_server.rpc import ClusterClient, Subscription, rpc_runtime
from tests.system.helpers import (
    GAME_EXECUTABLE,
    IMAGE,
    MASTER,
    OPERATION_TIMEOUT,
    STARTUP_TIMEOUT,
    VOLUME_IDMAP,
    check_console_contract,
    copy_image_bundle,
    make_server,
    read_player,
    reap_server,
    remove_container,
    replace_bundle_script,
    run_command,
    running_server,
    running_sharded_cluster,
    single_shard_configuration,
    startup_phase,
    wait_for_client,
    wait_for_stopping,
    write_cluster,
)

pytestmark = pytest.mark.system


async def test_export_restores_world_and_encoded_player_save(tmp_path: Path) -> None:
    py7zr = pytest.importorskip("py7zr")
    source = write_cluster(tmp_path / "source", encode_user_path=False)
    userid = "KU_1234567_"
    async with running_server(tmp_path, source) as server:
        original = await server.game.invoke(c.Runtime())
        await server.game.invoke(
            c.ExecuteJson(
                source="local player=SpawnPrefab('wilson');"
                f"player.userid={lua_string(userid)};"
                "player.Physics:Teleport(4,0,5);"
                "player.components.health:SetCurrentHealth(63);"
                "player.components.hunger:SetCurrent(71);"
                "local item=SpawnPrefab('goldnugget');"
                "item.components.stackable:SetStackSize(7);"
                "player.components.inventory:GiveItem(item);"
                "SerializeUserSession(player,true);return true"
            )
        )
        await server.save(completion_timeout=OPERATION_TIMEOUT)
        before = await read_player(server, userid)
        assert f"/{userid}_/" in str(before["file"])
        assert before["health"] == 63
        assert before.pop("hunger") == pytest.approx(71, abs=1)

    with (
        export_cluster(source, room_id="cluster") as exported,
        py7zr.SevenZipFile(exported.stream) as archive,
    ):
        archive.extractall(tmp_path / "restored")
    restored = tmp_path / "restored" / "cluster"
    session = Path("forest/save/session") / original.session_id
    source_player = source / session / f"{userid}_"
    restored_player_directory = restored / session / encode_klei_id(userid)
    assert {path.name: path.read_bytes() for path in source_player.iterdir()} == {
        path.name: path.read_bytes() for path in restored_player_directory.iterdir()
    }
    (restored / "cluster_token.txt").write_text("", encoding="utf-8")
    async with running_server(tmp_path, restored) as server:
        runtime = await server.game.invoke(c.Runtime())
        assert runtime.session_id == original.session_id
        assert runtime.seed == original.seed
        assert await server.game.invoke(
            c.ExecuteJson(source="return TheNet:GetDefaultEncodeUserPath()")
        )
        after = await read_player(server, userid)
        assert f"/{encode_klei_id(userid)}/" in str(after.pop("file"))
        assert after.pop("hunger") == pytest.approx(71, abs=1)
        before.pop("file")
        assert after == before
        await server.game.invoke(
            c.ExecuteJson(
                source=f"RestoreSnapshotUserSession(TheWorld.meta.session_identifier,{lua_string(userid)});"
                "return true"
            )
        )
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while True:
                restored_player = await server.game.invoke(
                    c.ExecuteJson(
                        source="for _,player in pairs(Ents) do "
                        f"if player.userid=={lua_string(userid)} "
                        "and player.is_snapshot_user_session then "
                        "return {prefab=player.prefab,"
                        "health=player.components.health.currenthealth} "
                        "end end;return nil"
                    )
                )
                if restored_player is not None:
                    break
                await asyncio.sleep(0.1)
        assert restored_player == {"prefab": "wilson", "health": 63}


async def test_player_activity_needs_no_files_and_resets_with_process(
    tmp_path: Path,
) -> None:
    async with running_sharded_cluster(tmp_path) as (controller, agents):
        sessions = {
            shard: (await agent.invoke(c.Runtime())).session_id
            for shard, agent in agents.items()
        }
        master = agents[MASTER]
        shard_directory = tmp_path / "cluster" / MASTER
        marker = shard_directory / "save/session" / sessions[MASTER] / ".last_login"
        snapshots = await master.invoke(c.Snapshots())
        assert snapshots.snapshots
        assert not marker.exists()
        userid = "KU_1234567_"
        with closing(controller.subscribe("events")) as events:

            async def observe(
                event_type: type[player.ShardEnteredEvent | player.PlayerLoadedEvent],
            ) -> GameEventRecord:
                async with asyncio.timeout(OPERATION_TIMEOUT):
                    while True:
                        record = (await events.next(1))[0]
                        if (
                            isinstance(record, GameEventRecord)
                            and record.shard == MASTER
                            and isinstance(record.event, event_type)
                            and record.event.data.player.userid == userid
                        ):
                            return record

            # Exercise the native handshake on a real entity without a network client.
            await master.invoke(
                c.ExecuteJson(
                    source="DST_LOGIN_PLAYER=SpawnPrefab('wilson');"
                    f"DST_LOGIN_PLAYER.userid={lua_string(userid)};return true"
                )
            )
            await observe(player.ShardEnteredEvent)
            assert not marker.exists()
            await master.invoke(
                c.ExecuteJson(
                    source="DST_LOGIN_PLAYER:OnPostActivateHandshake_Server("
                    "POSTACTIVATEHANDSHAKE.READY);return true"
                )
            )
            loaded = await observe(player.PlayerLoadedEvent)
        assert loaded.event.session_id == sessions[MASTER]
        timestamp = datetime.fromtimestamp(loaded.observed_timestamp_ns / 1e9, UTC)
        last_active_at = (await master.runtime_status()).last_active_at
        assert last_active_at is not None
        assert last_active_at >= timestamp
        assert not marker.exists()
        assert await master.invoke(c.Snapshots()) == snapshots
        await controller.restart()
        for shard, agent in agents.items():
            assert (await agent.invoke(c.Runtime())).session_id == sessions[shard]
        assert not marker.exists()
        await controller.regenerate(
            expected_session_id=sessions[MASTER],
            require_empty=True,
            timeout=STARTUP_TIMEOUT,
        )
        for shard, agent in agents.items():
            session = (await agent.invoke(c.Runtime())).session_id
            assert session != sessions[shard]
            assert not (
                tmp_path / "cluster" / shard / "save/session" / session / ".last_login"
            ).exists()


async def test_rollback_to_day_restores_both_shards_and_player_saves(
    tmp_path: Path,
) -> None:
    users = {"forest": "KU_1234567_", "cave": "KU_7654321_"}
    health = {"forest": 63, "cave": 74}
    async with running_sharded_cluster(tmp_path) as (controller, agents):
        sessions = {
            shard: (await agent.invoke(c.Runtime())).session_id
            for shard, agent in agents.items()
        }
        for shard, agent in agents.items():
            await agent.invoke(
                c.ExecuteJson(
                    source="TheWorld:PushEvent('ms_setautosaveenabled',false);"
                    "local clock=TheWorld.net.components.clock;"
                    "local data=clock:OnSave();data.cycles=9;clock:OnLoad(data);"
                    "DST_SNAPSHOT_PLAYER=SpawnPrefab('wilson');"
                    "local player=DST_SNAPSHOT_PLAYER;"
                    f"player.userid={lua_string(users[shard])};"
                    f"player.components.health:SetCurrentHealth({health[shard]});"
                    "player.Physics:Teleport(4,0,5);"
                    "local item=SpawnPrefab('goldnugget');"
                    "item.components.stackable:SetStackSize(7);"
                    "player.components.inventory:GiveItem(item);return true"
                )
            )
        target = await controller.save()
        for shard, later_health in {"forest": 41, "cave": 42}.items():
            await agents[shard].invoke(
                c.ExecuteJson(
                    source="DST_SNAPSHOT_PLAYER.components.health:SetCurrentHealth("
                    f"{later_health});return true"
                )
            )
        later_same_day = await controller.save()
        for agent in agents.values():
            await agent.invoke(
                c.ExecuteJson(
                    source="DST_SNAPSHOT_PLAYER.components.health:SetCurrentHealth(17);"
                    "local clock=TheWorld.net.components.clock;"
                    "local data=clock:OnSave();data.cycles=19;"
                    "clock:OnLoad(data);return true"
                )
            )
        missing = await controller.save()
        latest = await controller.save()
        assert target.snapshot is not None
        assert later_same_day.snapshot is not None
        assert missing.snapshot is not None
        assert latest.snapshot is not None
        assert latest.snapshot > missing.snapshot > later_same_day.snapshot
        assert later_same_day.snapshot > target.snapshot
        for shard in agents:
            for path in (tmp_path / "cluster" / shard / "save").rglob(
                f"{missing.snapshot:010d}*"
            ):
                if path.is_file():
                    path.unlink()
        page = await controller.list_snapshots(limit=1)
        assert page.has_more
        assert page.snapshots[0].snapshot_id == latest.snapshot
        assert page.snapshots[0].metadata is not None
        assert page.snapshots[0].metadata.day == 20
        previous = await controller.list_snapshots(limit=1, before=latest.snapshot)
        assert previous.snapshots[0].snapshot_id == later_same_day.snapshot
        assert previous.snapshots[0].metadata is not None
        assert previous.snapshots[0].metadata.day == 10
        earliest = await controller.list_snapshots(
            limit=1, before=later_same_day.snapshot
        )
        assert earliest.snapshots[0].snapshot_id == target.snapshot
        assert earliest.snapshots[0].metadata is not None
        assert earliest.snapshots[0].metadata.day == 10
        # Schedule a native autosave at the exact point rollback begins truncating.
        await agents["forest"].invoke(
            c.ExecuteJson(
                source="local methods=getmetatable(TheNet).__index;"
                "local original=methods.TruncateSnapshots;"
                "methods.TruncateSnapshots=function(self,session,count)"
                "methods.TruncateSnapshots=original;"
                "TheWorld:PushEvent('ms_save');"
                "return original(self,session,count) end;return true"
            )
        )
        restored = await controller.rollback_to_day(10)
        assert restored.snapshot_id == target.snapshot
        for shard, agent in agents.items():
            assert (await agent.invoke(c.Runtime())).session_id == sessions[shard]
            assert (await agent.invoke(c.World())).day == 10
            saved_player = await read_player(agent.server, users[shard])
            assert saved_player["health"] == health[shard]
            assert saved_player["inventory"] == [
                {
                    "prefab": "goldnugget",
                    "x": 4,
                    "z": 5,
                    "data": {"stackable": {"stack": 7}},
                }
            ]


async def test_image_entrypoint_runs_single_shard_and_handles_sigterm(
    tmp_path: Path,
    container_name: str,
) -> None:
    rooms = RoomStore(tmp_path)
    rooms.save(Room(number=299, cluster=single_shard_configuration()))
    cluster = rooms.path(299)
    assert (cluster / "cluster.ini").is_file()
    assert (cluster / MASTER / "server.ini").is_file()
    client: ClusterClient | None = None
    lifecycle: Subscription[LifecycleRecord] | None = None
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await run_command(
            "podman",
            "run",
            "--pull=never",
            "--detach",
            "--name",
            container_name,
            "--network",
            "none",
            "--volume",
            f"{cluster}:/cluster:idmap={VOLUME_IDMAP}",
            IMAGE,
            "dst-server",
            "agent",
            "master",
        )
        async with rpc_runtime():
            client, status = await wait_for_client(
                cluster / ".dst-server.sock",
                startup_phase,
            )
            assert [(shard.name, shard.ready) for shard in status.shards] == [
                (MASTER, True)
            ]
            await check_console_contract(client)
            assert not (cluster / "console").exists()
            _, output = await run_command(
                sys.executable,
                "-m",
                "dst_server",
                "--cluster-root",
                str(tmp_path),
                "--json",
                "console",
                "1 + 2",
                "--room",
                "299",
            )
            assert orjson.loads(output) == [
                {
                    "room": 299,
                    "ok": True,
                    "result": {
                        "output": "",
                        "values": [{"type": "number", "text": "3"}],
                        "error": None,
                        "truncated": False,
                    },
                }
            ]
            _, process_status = await run_command(
                "podman", "exec", container_name, "cat", "/proc/1/status"
            )
            for field in ("Uid:", "Gid:"):
                identity = next(
                    line
                    for line in process_status.splitlines()
                    if line.startswith(field)
                )
                assert identity.split()[1:] == ["1000"] * 4
            socket_metadata = (cluster / ".dst-server.sock").stat()
            assert (socket_metadata.st_uid, socket_metadata.st_gid) == (0, 0)
            _, processes = await run_command("podman", "top", container_name, "args")
            assert GAME_EXECUTABLE in processes
            lifecycle = await client.subscribe("lifecycle")
            stopping = asyncio.create_task(
                wait_for_stopping(lifecycle, frozenset({MASTER}))
            )
            try:
                await run_command(
                    "podman",
                    "stop",
                    "--time",
                    "40",
                    container_name,
                    seconds=OPERATION_TIMEOUT,
                )
                assert await stopping == {MASTER}
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            with suppress(Exception):
                await lifecycle.close()
            lifecycle = None
            client.close()
            client = None
        _, exit_code = await run_command(
            "podman",
            "inspect",
            "--format={{.State.ExitCode}}",
            container_name,
        )
        assert exit_code.strip() == "0"
    except BaseException as error:
        _, output = await run_command("podman", "logs", container_name, check=False)
        error.add_note("recent container logs:\n" + output)
        raise
    finally:
        if lifecycle is not None:
            with suppress(Exception):
                await lifecycle.close()
        if client is not None:
            client.close()


async def test_sdk_real_game_core_contract(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path, configured=True)
    logs: deque[str] = deque(maxlen=200)
    server = make_server(tmp_path, cluster, container_name, log_handler=logs.append)
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            assert server.driver_health.telemetry_status == "active"
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            assert await server.game.invoke(
                c.ExecuteJson(
                    source="return {"
                    "day=TheWorld.topology.overrides.day,"
                    "game_mode=TheNet:GetDefaultGameMode(),"
                    "max_players=TheNet:GetDefaultMaxPlayers(),"
                    "encode_user_path=TheNet:GetDefaultEncodeUserPath()"
                    "}"
                )
            ) == {
                "day": "onlyday",
                "encode_user_path": True,
                "game_mode": "survival",
                "max_players": 16,
            }
            runtime = await server.game.invoke(c.Runtime())
            assert runtime.session_id == server.session_id
            assert await server.game.players.list() == ()
            assert await server.game.invoke(
                c.ExecuteJson(
                    source="local action; "
                    'action=BufferedAction(TheWorld,nil,{id="CHOP",fn=function(self) '
                    "assert(self==action and self.doer==TheWorld); "
                    'return true,"native-action" end}); '
                    "local ok,reason=action:Do(); return {ok=ok,reason=reason}"
                )
            ) == {"ok": True, "reason": "native-action"}
            text = "before\u0085middle\u2028after\u2029"
            assert (
                await server.game.invoke(
                    c.ExecuteJson(source=f"return {lua_string(text)}")
                )
                == text
            )
            with pytest.raises(IndeterminateCommandError) as failure:
                await server.game.invoke(
                    c.ExecuteJson(source='error("injected SDK request failure", 0)')
                )
            assert isinstance(failure.value.__cause__, LuaRequestError)
            assert failure.value.__cause__.code == "lua_error"
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            saved = await server.save(completion_timeout=OPERATION_TIMEOUT)
            assert saved.snapshot is None or saved.snapshot >= runtime.snapshot
            generation = server.driver_health.generation
            await server.game.invoke(c.Reset(timeout=OPERATION_TIMEOUT))
            assert server.driver_health.generation > generation
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server, container_name)


async def test_native_callbacks_publish_sdk_events(tmp_path: Path) -> None:
    cluster = write_cluster(tmp_path)
    script = cluster / "native-contract.lua"
    script.write_text(
        (Path(__file__).parents[1] / "lua/native_integration.lua").read_text()
    )
    async with running_server(tmp_path, cluster) as server:
        await server.game.invoke(
            c.ExecuteJson(
                source="for _,userid in ipairs({'KU_NATIVE','KU_ATTACKER'}) do "
                "local actor=SpawnPrefab('wilson');actor.userid=userid;"
                "actor.Physics:Teleport("
                "TheWorld.components.playerspawner:GetAnySpawnPoint()) "
                "end;return true"
            )
        )
        joined = set()
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while joined != {"KU_NATIVE", "KU_ATTACKER"}:
                observed = await server.read_game_event()
                assert observed is not None
                if isinstance(observed.record, player.ShardEnteredEvent):
                    joined.add(observed.record.data.player.userid)
        result = await server.game.invoke(
            c.Evaluate(source=f"return dofile({lua_string(str(script))})")
        )
        assert result.error is None
        records = []
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while True:
                observed = await server.read_game_event()
                assert observed is not None
                record = observed.record.model_dump(mode="json")
                records.append(record)
                if (
                    record["event"] == "dst.server.system_message"
                    and record["data"]["message"] == "native-contract-complete"
                ):
                    break
        events = {record["event"] for record in records}
        assert {
            "dst.client.authenticated",
            "dst.client.disconnected",
            "dst.player.spawned",
            "dst.player.chat",
            "dst.mod.outdated",
            "dst.server.pause_changed",
            "dst.player.combat_received",
            "dst.player.combat_hit",
            "dst.player.unequipped",
            "dst.player.finished_work",
            "dst.player.picked",
            "dst.player.action",
            "dst.player.incident",
            "dst.vote.started",
            "dst.vote.cast",
            "dst.vote.closed",
            "dst.vote.result",
        } <= events
        assert "dst.telemetry.error" not in events
        assert server.driver_health.errors == 0
        received = next(
            record["data"]
            for record in records
            if record["event"] == "dst.player.combat_received"
        )
        assert received["damage"] == received["original_damage"] == 10
        assert received["damage_resolved"] == 10
        assert received["player"]["userid"] == "KU_NATIVE"
        assert received["attacker"]["userid"] == "KU_ATTACKER"
        hit = next(
            record["data"]
            for record in records
            if record["event"] == "dst.player.combat_hit"
        )
        assert hit["damage"] == hit["damage_resolved"] == 10
        assert hit["player"]["userid"] == "KU_ATTACKER"
        assert hit["target"]["userid"] == "KU_NATIVE"
        incidents = {
            record["data"]["kind"]
            for record in records
            if record["event"] == "dst.player.incident"
            and record["data"]["player"]["userid"] == "KU_NATIVE"
        }
        assert {"sink", "fall_in_void"} <= incidents
        chat = next(
            record["data"] for record in records if record["event"] == "dst.player.chat"
        )
        assert chat["message"] == "你好\n世界"
        assert chat["player"]["userid"] == "KU_NATIVE"
        votes = [
            record["data"]
            for record in records
            if record["event"].startswith("dst.vote.")
        ]
        assert len(votes) == 5
        assert all(vote["vote_id"] == votes[0]["vote_id"] for vote in votes)
        assert votes[-1]["passed"] is True
        assert votes[-1]["total_voted"] == 2


async def test_real_game_driver_restarts_with_unchanged_session_id(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path)
    logs: deque[str] = deque(maxlen=200)
    server = make_server(tmp_path, cluster, container_name, log_handler=logs.append)
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        session_id = server.session_id
        generation = server.driver_health.generation
        async with asyncio.timeout(OPERATION_TIMEOUT):
            await server.execute("c_reset()", completion_timeout=OPERATION_TIMEOUT)
            await server.game.wait_reload(
                generation,
                asyncio.get_running_loop().time() + OPERATION_TIMEOUT,
            )
            assert server.driver_health.generation > generation
            assert server.session_id == session_id
            generation = server.driver_health.generation
            await server.game.invoke(c.Reset(timeout=OPERATION_TIMEOUT))
            assert server.driver_health.generation > generation
            assert server.session_id == session_id
            assert server.driver_error is None
            health = await server.game.invoke(c.Health())
            assert health.generation == server.driver_health.generation
            assert health.telemetry_status == "active"
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server, container_name)


@pytest.mark.parametrize("fault", ["telemetry", "core"])
async def test_real_game_driver_degrades_safely(  # ruff: ignore[too-many-statements]
    tmp_path: Path,
    container_name: str,
    fault: Literal["telemetry", "core"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = write_cluster(tmp_path)
    bundle = tmp_path / f"{fault}-failure-scripts.zip"
    await copy_image_bundle(bundle)
    script = (
        "scripts/dst_server/world_events.lua"
        if fault == "telemetry"
        else "scripts/dst_server.lua"
    )
    with ZipFile(bundle) as archive:
        original = archive.read(script)
    if fault == "telemetry":
        needle = b"    local state_names = {"
        assert needle in original
        modified = original.replace(
            needle,
            b'    error("injected telemetry install failure", 0)\n\n' + needle,
            1,
        )
    else:
        modified = b'error("injected core driver failure", 0)\n'
    replace_bundle_script(bundle, script, modified)
    logs: deque[str] = deque(maxlen=200)
    server = make_server(
        tmp_path,
        cluster,
        container_name,
        bundle,
        log_handler=logs.append,
    )
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        if fault == "core":
            kill = server.kill

            async def kill_container() -> int:
                # The Podman wrapper's game owns the inherited output FDs.
                await remove_container(container_name)
                return await kill()

            monkeypatch.setattr(server, "kill", kill_container)
            with pytest.raises(RuntimeError, match="installation_failed"):
                await server.start(startup_timeout=STARTUP_TIMEOUT)
            assert server.driver_error == "installation_failed"
            assert server.returncode is not None
            await reap_server(server, container_name)
            replace_bundle_script(bundle, script, original)
            server = make_server(
                tmp_path, cluster, container_name, bundle, log_handler=logs.append
            )
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            health = server.driver_health
            if fault == "telemetry":
                assert health.telemetry_status == "degraded"
                assert health.last_error is not None
                assert health.last_error.stage == "world.install"
                assert health.last_error.message == "installation_failed"
                diagnostic = await server.read_game_event()
                assert diagnostic is not None
                assert diagnostic.record.event == "dst.telemetry.error"
            else:
                assert health.telemetry_status == "active"
                assert server.driver_error is None
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            await server.save(completion_timeout=OPERATION_TIMEOUT)
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server, container_name)
