"""Explicitly run disposable native lifecycle acceptance under Podman.

Requires the native image, a static Agent, its Lua bundle, pycapnp and psutil.
The output directory must not already exist; no deployed room is inspected.
"""

# The disposable acceptance harness deliberately polls external processes and uses
# pycapnp dynamic schema values. All executable paths are explicit test inputs.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, missing-return-type-special-method, missing-type-function-argument, missing-return-type-undocumented-public-function, missing-return-type-private-function, missing-type-args, async-function-with-timeout, async-busy-wait, blocking-path-method-in-async-function, too-many-nested-blocks, too-many-statements, pytest-composite-assertion, print]

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import capnp
import psutil


class RejectedError(Exception):
    def __init__(self, method, error):
        super().__init__(method, error)
        self.error = error


class Client:
    async def connect(self, socket, schema):
        self.stream = await capnp.AsyncIoStream.create_unix_connection(str(socket))
        self.client = capnp.TwoPartyClient(self.stream)
        self.room = self.client.bootstrap().cast_as(schema.Room)
        return self

    async def call(self, method, arguments=None, shard=None, timeout=150):
        payload = {
            "target": {"scope": "room"}
            if shard is None
            else {"scope": "shard", "shard": shard},
            "request": {"method": method, "arguments": arguments or {}},
            "timeout": timeout,
        }
        async with asyncio.timeout(timeout + 5):
            reply = await self.room.call(request=json.dumps(payload).encode())
        if reply.result.which() != "value":
            raise RejectedError(method, json.loads(reply.result.error))
        return json.loads(reply.result.value)

    def close(self):
        self.client.close()
        self.stream.close()


def run(*arguments):
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, timeout=45
    ).stdout


def native_processes(init_pid):
    result = {}
    for process in psutil.Process(init_pid).children(recursive=True):
        try:
            argv = process.cmdline()
            if argv and (
                argv[0] == "/test/dst-server"
                or "dontstarve_dedicated_server" in argv[0]
            ):
                result[process.pid] = argv
        except psutil.NoSuchProcess, psutil.AccessDenied:
            pass
    return result


class Fixture:
    def __init__(self, arguments, count, schema, label=None, pause_when_empty=False):
        self.arguments = arguments
        label = label or str(count)
        self.root = arguments.directory / label
        self.root.mkdir()
        self.cluster = self.root / "cluster"
        source = arguments.seed / ("one" if count == 1 else "two") / "cluster"
        shutil.copytree(source, self.cluster)
        for path in self.cluster.glob(".dst-*"):
            if path.is_file() or path.is_socket():
                path.unlink()
        (self.cluster / ".dst-control.json").write_text(
            json.dumps({"policy": {"mod_auto_update": False}})
        )
        if pause_when_empty:
            config = self.cluster / "cluster.ini"
            config.write_text(
                config.read_text().replace(
                    "[GAMEPLAY]", "[GAMEPLAY]\npause_when_empty = true"
                )
            )
        self.name = f"dst-rust-lifecycle-{os.getpid()}-{label}"
        self.count = count
        self.schema = schema
        self.boot = 0
        self.client = None
        self.running = False

    def launch(self):
        self.boot += 1
        mapping = "idmap=uids=0-1000-1;gids=0-1000-1"
        # Keep PID 1 alive after killing the Agent so OCI container teardown
        # cannot hide a missing native parent monitor or unreaped descendants.
        command = (
            "/test/dst-server agent --cluster /cluster --offline --profile history "
            f">/cluster/acceptance-agent-{self.boot}.log 2>&1 & "
            "wait $!; exec sleep infinity"
        )
        run(
            "podman",
            "run",
            "-d",
            "--rm",
            "--init",
            "--pull=never",
            "--network",
            "none",
            "--name",
            self.name,
            "-e",
            "OTEL_LOGS_EXPORTER=none",
            "-e",
            "OTEL_METRICS_EXPORTER=none",
            "-e",
            "OTEL_TRACES_EXPORTER=none",
            "-v",
            f"{self.cluster}:/cluster:{mapping}",
            "-v",
            f"{self.cluster}/mods:/install/mods:{mapping}",
            "-v",
            f"{self.arguments.directory}/scripts.zip:/install/data/databundles/scripts.zip:ro",
            "-v",
            f"{self.arguments.directory}/dst-server:/test/dst-server:ro",
            "--entrypoint",
            "/bin/sh",
            self.arguments.image,
            "-c",
            command,
        )
        self.running = True
        self.init_pid = int(
            run("podman", "inspect", "--format", "{{.State.Pid}}", self.name)
        )

    async def connect(self, phase="running"):
        async with asyncio.timeout(180):
            while self.client is None:
                try:
                    self.client = await Client().connect(
                        self.cluster / ".dst-agent.sock", self.schema
                    )
                except FileNotFoundError, ConnectionRefusedError:
                    await asyncio.sleep(0.1)
            while True:
                state = await self.client.call("status")
                if state["agent"]["ready"] and state["phase"] == phase:
                    if phase == "running":
                        assert len(state["shards"]) == self.count, state
                        assert all(
                            shard["readiness"]["world_loaded"]
                            and shard["readiness"]["control_ready"]
                            and not shard["readiness"]["missing_shards"]
                            for shard in state["shards"]
                        ), state
                        for shard in state["shards"]:
                            runtime = await self.client.call(
                                "runtime", shard=shard["name"]
                            )
                            assert runtime["app_version"] == "756039", runtime
                    return state
                if state["phase"] == "failed" and phase != "failed":
                    raise AssertionError(state)
                await asyncio.sleep(0.2)

    async def kill_agent(self):
        processes = native_processes(self.init_pid)
        agents = [
            pid for pid, argv in processes.items() if argv[0] == "/test/dst-server"
        ]
        games = [
            pid
            for pid, argv in processes.items()
            if "dontstarve_dedicated_server" in argv[0]
        ]
        assert len(agents) == 1 and len(games) == self.count, processes
        for pid in games:
            assert "-monitor_parent_process" in processes[pid], processes[pid]
        self.client.close()
        self.client = None
        started = time.monotonic()
        os.kill(agents[0], signal.SIGKILL)
        async with asyncio.timeout(40):
            while any(psutil.pid_exists(pid) for pid in agents + games):
                await asyncio.sleep(0.05)
        assert (
            run(
                "podman", "inspect", "--format", "{{.State.Running}}", self.name
            ).strip()
            == "true"
        )
        assert native_processes(self.init_pid) == {}
        control = json.loads((self.cluster / ".dst-control.json").read_text())
        assert control["agent_run"]["active"] is True, control
        return {
            "agent_pid": agents[0],
            "game_pids": games,
            "reaped_seconds": time.monotonic() - started,
            "container_init_remained_alive": True,
            "durable_control": control,
        }

    def close_container(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.running:
            run("podman", "stop", "--time", "10", self.name)
            self.running = False

    async def paused_save(self):
        client = self.client
        names = [shard["name"] for shard in (await client.call("status"))["shards"]]
        pause_result = await client.call("pause", {"paused": True})
        for name in names:
            try:
                async with asyncio.timeout(5):
                    while not (await client.call("room", shard=name))["is_paused"]:
                        await asyncio.sleep(0.1)
            except TimeoutError:
                source = (
                    "return {requested=TheNet:IsServerPaused(true),"
                    "actual=TheNet:IsServerPaused(),"
                    "sim=require('dst_server.state').sim_paused}"
                )
                flags = await client.call("execute_json", {"source": source}, name)
                await client.call("pause", {"paused": False})
                return {
                    "confirmed": False,
                    "pause_result": pause_result,
                    "shard": name,
                    "native_flags": flags,
                }
        before = {
            name: (await client.call("runtime", shard=name))["snapshot"]
            for name in names
        }
        started = time.monotonic()
        try:
            await client.call("save", timeout=10)
        except RejectedError as error:
            rejection = error.error
            assert rejection["code"] not in {"timeout", "unknown"}, rejection
        else:
            message = "paused save unexpectedly reported success"
            raise AssertionError(message)
        elapsed = time.monotonic() - started
        after = {
            name: (await client.call("runtime", shard=name))["snapshot"]
            for name in names
        }
        assert before == after, (before, after)
        await client.call("pause", {"paused": False})
        saved = await client.call("save")
        assert all(
            shard["result"]["status"] == "success" for shard in saved["shards"]
        ), saved
        return {
            "confirmed": True,
            "paused_error": rejection,
            "pause_result": pause_result,
            "rejected_seconds": elapsed,
            "unchanged_snapshots": before,
            "resumed_save": saved,
        }

    async def empty_paused_save(self):
        names = [
            shard["name"] for shard in (await self.client.call("status"))["shards"]
        ]
        async with asyncio.timeout(10):
            for name in names:
                while not (await self.client.call("room", shard=name))["is_paused"]:
                    await asyncio.sleep(0.1)
        source = (
            "return {requested=TheNet:IsServerPaused(true),"
            "actual=TheNet:IsServerPaused(),"
            "sim=require('dst_server.state').sim_paused,tick=GetTick(),"
            "snapshot=TheNet:GetCurrentSnapshot()}"
        )
        before = {
            name: await self.client.call("execute_json", {"source": source}, name)
            for name in names
        }
        assert all(state["sim"] is True for state in before.values()), before
        await asyncio.sleep(0.5)
        started = time.monotonic()
        try:
            await self.client.call("save", timeout=10)
        except RejectedError as error:
            rejection = error.error
            assert rejection["code"] == "invalid", rejection
        else:
            message = "save in an automatically paused room reported success"
            raise AssertionError(message)
        elapsed = time.monotonic() - started
        after = {
            name: await self.client.call("execute_json", {"source": source}, name)
            for name in names
        }
        assert before == after, (before, after)
        stopped = await self.client.call("stop", {"notice": None})
        assert all(
            shard["result"]["status"] == "success" for shard in stopped["shards"]
        ), stopped
        assert all(
            shard["result"]["value"]["saved_snapshot"] is not None
            and not shard["result"]["value"]["forced"]
            and shard["result"]["value"]["output_drained"]
            for shard in stopped["shards"]
        ), stopped
        return {
            "before": before,
            "after": after,
            "save_error": rejection,
            "rejected_seconds": elapsed,
            "stop": stopped,
            "passed": True,
        }


async def synthetic_player(client):
    user = "KU_RUSTTEST"

    async def lua(source):
        return await client.call("execute_json", {"source": source}, "surface")

    created = await lua(
        f"local p=SpawnPrefab('wilson');p.userid='{user}';"
        "p.Physics:Teleport(4,0,5);return {guid=p.GUID,userid=p.userid}"
    )
    location = await client.call("get_player", {"userid": user})
    assert location["state"] == "active" and location["shard"] == "surface", location
    given = await client.call(
        "give", {"userid": user, "item": "goldnugget", "count": 7}
    )
    assert given == 7, given
    removed = await client.call(
        "remove", {"userid": user, "item": "goldnugget", "count": 3}
    )
    assert removed == 3, removed
    count = await lua(
        f"local p=LookupPlayerInstByUserID('{user}');"
        "local _,n=p.components.inventory:Has('goldnugget',99);return n"
    )
    assert count == 4, count
    assert (
        await client.call("set_vitals", {"userid": user, "health": 0.5, "hunger": 0.5})
        is True
    )
    assert await client.call("kill_player", {"userid": user}) is True
    dead = await client.call("get_player", {"userid": user})
    assert dead["player"]["vitals"]["health"]["is_dead"] is True, dead
    # A console-spawned character has no client animation acknowledgement.
    # Enter the native ghost state explicitly before testing public revival.
    await lua(
        f"LookupPlayerInstByUserID('{user}'):PushEvent('makeplayerghost');return true"
    )
    ghost = await client.call("get_player", {"userid": user})
    assert ghost["player"]["is_ghost"] is True, ghost
    assert await client.call("revive", {"userid": user}) is True
    async with asyncio.timeout(20):
        while (await client.call("get_player", {"userid": user}))["player"]["is_ghost"]:
            await asyncio.sleep(0.1)
    alive = await client.call("get_player", {"userid": user})
    await lua(f"LookupPlayerInstByUserID('{user}'):Remove();return true")
    return {
        "created": created,
        "given": given,
        "removed": removed,
        "remaining_items": count,
        "dead": dead,
        "ghost_fixture": ghost,
        "revived": alive,
        "actual_connected_players": 0,
    }


async def check(arguments, report):
    schema = capnp.load(
        str(Path(__file__).resolve().parents[2] / "crates/dst-server/schema/room.capnp")
    )
    async with capnp.kj_loop():
        for count in (1, 2):
            fixture = Fixture(arguments, count, schema)
            evidence = report["rooms"][str(count)] = {"kills": []}
            try:
                fixture.launch()
                evidence["initial"] = await fixture.connect()
                evidence["pause"] = await fixture.paused_save()
                report_path(arguments, report)
                print(f"{count} worlds pause evidence: {evidence['pause']}", flush=True)
                if count == 1:
                    try:
                        evidence["synthetic_player"] = await synthetic_player(
                            fixture.client
                        )
                    except (RejectedError, AssertionError, TimeoutError) as error:
                        evidence["synthetic_player"] = {
                            "error": repr(error),
                            "passed": False,
                        }
                    report_path(arguments, report)
                    await fixture.client.call("save")
                    for attempt in range(1, 4):
                        evidence["kills"].append(await fixture.kill_agent())
                        report_path(arguments, report)
                        print(
                            f"Agent kill {attempt}: all {count} native games reaped",
                            flush=True,
                        )
                        fixture.close_container()
                        fixture.launch()
                        state = await fixture.connect(
                            "running" if attempt <= 2 else "failed"
                        )
                        recovery = state["agent"]["recovery"]
                        assert recovery["restarts_used"] == min(attempt, 2), state
                        if attempt <= 2:
                            assert recovery["closed"] is None, state
                        else:
                            assert recovery["closed"] == "current_retries_exhausted", (
                                state
                            )
                            assert all(
                                shard["pid"] is None for shard in state["shards"]
                            ), state
                        evidence.setdefault("recreated", []).append(state)
                else:
                    source = (
                        "DST_ACCEPT_SAVE=false;"
                        "SaveGame(false,function() DST_ACCEPT_SAVE=true end);"
                        "return true"
                    )
                    await fixture.client.call(
                        "execute_json", {"source": source}, "world-2"
                    )
                    async with asyncio.timeout(30):
                        while not await fixture.client.call(
                            "execute_json",
                            {"source": "return DST_ACCEPT_SAVE"},
                            "world-2",
                        ):
                            await asyncio.sleep(0.1)
                    evidence["kills"].append(await fixture.kill_agent())
                    fixture.close_container()
                    fixture.launch()
                    state = await fixture.connect("failed")
                    assert (
                        state["agent"]["recovery"]["closed"]
                        == "latest_snapshots_differ"
                    ), state
                    assert state["agent"]["recovery"]["restarts_used"] == 0, state
                    assert all(shard["pid"] is None for shard in state["shards"]), state
                    evidence["mismatched_recreated"] = state
                report_path(arguments, report)
                print(f"{count} native worlds passed lifecycle acceptance", flush=True)
            finally:
                fixture.close_container()
        fixture = Fixture(arguments, 2, schema, label="paused", pause_when_empty=True)
        try:
            fixture.launch()
            await fixture.connect()
            report["paused_save"] = await fixture.empty_paused_save()
            report_path(arguments, report)
            print(
                "Two paused native worlds reject save and confirm shutdown saves",
                flush=True,
            )
        finally:
            fixture.close_container()


def report_path(arguments, report):
    (arguments.directory / "report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--image", default="localhost/dst-rust-native:756039")
    arguments = parser.parse_args()
    arguments.directory.mkdir(exist_ok=False, parents=True)
    for source, name in [
        (arguments.binary, "dst-server"),
        (arguments.bundle, "scripts.zip"),
    ]:
        shutil.copy2(source, arguments.directory / name)
    report = {
        "passed": False,
        "native_build": "756039",
        "image": arguments.image,
        "binary_sha256": hashlib.sha256(arguments.binary.read_bytes()).hexdigest(),
        "bundle_sha256": hashlib.sha256(arguments.bundle.read_bytes()).hexdigest(),
        "actual_connected_players": 0,
        "rooms": {},
    }
    try:
        asyncio.run(check(arguments, report))
        report["passed"] = report["paused_save"]["passed"] and all(
            "error" not in room.get("synthetic_player", {})
            for room in report["rooms"].values()
        )
        assert report["passed"], report
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        report_path(arguments, report)


if __name__ == "__main__":
    main()
