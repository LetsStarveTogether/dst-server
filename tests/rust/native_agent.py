"""Exercise ready, RPC and clean shutdown through the public CLI."""

# The executable is the test binary supplied by the caller.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path


def call(
    binary: Path,
    root: Path,
    report: dict,
    method: str,
    value: dict | None = None,
    *,
    shard: str | None = None,
    timeout: int = 30,
) -> object:
    started = time.monotonic()
    request = [
        str(binary),
        "--json",
        "call",
        method,
        "-",
        "--socket",
        str(root / "cluster/.dst-agent.sock"),
        "--timeout",
        str(timeout),
    ]
    if shard:
        request.extend(["--shard", shard])
    result = subprocess.run(
        request,
        input=json.dumps(value or {}).encode(),
        capture_output=True,
        check=False,
        timeout=timeout + 10,
    )
    assert result.returncode == 0, (method, result.stdout, result.stderr)
    result_value = json.loads(result.stdout)[0]["result"]["value"]
    report["calls"].append({"method": method, "seconds": time.monotonic() - started})
    return result_value


def ready(
    binary: Path, root: Path, report: dict, process: subprocess.Popen | None = None
) -> dict:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if process is not None:
            assert process.poll() is None, (
                "Agent exited before readiness",
                process.returncode,
            )
        if (root / "cluster/.dst-agent.sock").exists():
            state = call(binary, root, report, "status")
            assert state["phase"] != "failed", state
            if (
                state["agent"]["ready"]
                and state["phase"] == "running"
                and state["shards"]
                and all(
                    shard["readiness"]["process_running"]
                    and shard["readiness"]["world_loaded"]
                    and shard["readiness"]["control_ready"]
                    and not shard["readiness"]["missing_shards"]
                    for shard in state["shards"]
                )
            ):
                return state
        time.sleep(0.5)
    message = "native room did not become ready"
    raise TimeoutError(message)


def exercise_profile(binary: Path, root: Path, report: dict, profile: str) -> None:
    def lua(source: str) -> object:
        return call(
            binary, root, report, "execute_json", {"source": source}, shard="surface"
        )

    health = call(binary, root, report, "health", shard="surface")
    assert health["control_ready"], health
    assert health["profile"] == profile, health
    assert health["telemetry_status"] == (
        "disabled" if profile == "off" else "active"
    ), health
    # The native join task attaches observers on the next tick.
    assert lua(
        "TheWorld.TraceProbePlayer=SpawnPrefab('wilson');"
        "TheWorld.TraceProbePlayer.userid='KU_TRACE_PROBE';"
        "TheWorld.TraceProbePeer=SpawnPrefab('wilson');"
        "TheWorld.TraceProbePeer.userid='KU_TRACE_PEER';return true"
    )
    deadline = time.monotonic() + 10
    while not lua(
        "return not TheWorld.TraceProbePlayer.jointask"
        " and not TheWorld.TraceProbePeer.jointask"
    ):
        assert time.monotonic() < deadline, "synthetic player never joined"
        time.sleep(0.05)
    assert lua("""
local p = TheWorld.TraceProbePlayer
local colour = {1, 1, 1, 1}
Networking_Say(p.GUID,p.userid,"Probe","wilson","public trace probe",colour,false,true)
Networking_Say(p.GUID,p.userid,"Probe","wilson","whisper trace probe",colour,true,true)
local fire, fuel = SpawnPrefab("firepit"), SpawnPrefab("log")
p.components.inventory:GiveItem(fuel)
assert(not BufferedAction(p, p, ACTIONS.ADDFUEL, fuel):Do())
assert(BufferedAction(p, fire, ACTIONS.ADDFUEL, fuel):Do())
fire:Remove()
local statue = SpawnPrefab("resurrectionstatue")
assert(statue.components.attunable:LinkToPlayer(p))
assert(not statue.components.attunable:LinkToPlayer(p))
statue:Remove()
local x, _, z = TheWorld.components.playerspawner:GetAnySpawnPoint()
local kit = SpawnPrefab("wall_wood_item")
local function place_wall()
    for dx = -4, 4 do
        for dz = -4, 4 do
            local point = Vector3(math.floor(x) + dx + .5, 0, math.floor(z) + dz + .5)
            if kit.components.deployable:CanDeploy(point, nil, p) then
                return kit.components.deployable:Deploy(point, p)
            end
        end
    end
end
assert(place_wall())
local peer = TheWorld.TraceProbePeer
local spear = SpawnPrefab("spear")
local function hit(source, target, weapon)
    local combat = target.components.combat
    assert(combat:GetAttacked_Internal(source, 1, weapon, nil, nil, true))
end
for _ = 1, 2 do
    local spider = SpawnPrefab("spider")
    hit(p, spider, spear)
    hit(spider, p)
    spider:Remove()
end
local wall = SpawnPrefab("wall_wood")
local hp = wall.components.health.currenthealth
hit(p, wall, spear)
assert(wall.components.health.currenthealth < hp)
wall:Remove()
hit(p, peer, spear)
spear:Remove()
peer:Remove()
TheWorld.TraceProbePeer=nil
local ate = p.components.eater:Eat(SpawnPrefab("berries"))
p:Remove()
TheWorld.TraceProbePlayer=nil
Networking_ModOutOfDateAnnouncement("trace_probe")
return ate
""")
    presence = call(binary, root, report, "presence", shard="surface")
    assert presence["reliable"], presence
    assert "trace_probe" in presence["outdated_mods"], presence
    report["profile_health"] = call(binary, root, report, "health", shard="surface")
    assert report["profile_health"]["errors"] == 0, report["profile_health"]


def records(log: Path) -> Iterator[dict]:
    with log.open("rb") as source:
        for line in source:
            if line.startswith(b"DST_RECORD|") and line.endswith(b"\n"):
                yield json.loads(line.removeprefix(b"DST_RECORD|"))


def check_profile_log(log: Path, profile: str) -> None:
    # A clean stop drains the Agent's output, so missing events can be tested here.
    messages, meals, actions, walls, bindings, contacts = [], [], [], [], [], []
    for record in records(log):
        if "dst.event.sequence" not in record["attributes"]:
            continue
        name, body = record["event_name"], record["body"]
        assert profile != "off", name
        if body.get("userid") == "KU_TRACE_PROBE" and "message" in body:
            messages.append((name, body["message"]))
        if name == "dst.player.combat_contact" and "KU_TRACE_PROBE" in {
            body["source"]["userid"],
            body["target"]["userid"],
        }:
            source, target = body["source"], body["target"]
            contacts.append((
                source["userid"] or source["prefab"],
                target["userid"] or target["prefab"],
                body["kind"],
                body["weapon"],
            ))
        if (body.get("player") or {}).get("userid") != "KU_TRACE_PROBE":
            continue
        if name == "dst.player.ate":
            meals.append(body["food"])
        elif name == "dst.player.action_performed":
            actions.append((body["action_id"], body["target"]["prefab"], body["item"]))
        elif name == "dst.player.item_deployed":
            assert set(body["position"]) == {"x", "y", "z"}
            walls.append(body["item"])
        elif name == "dst.player.revival_bound":
            bindings.append(body["source"])
    assert messages == (
        [] if profile == "off" else [("dst.player.public_chat", "public trace probe")]
    ), messages
    assert meals == (["berries"] if profile == "play" else []), meals
    assert actions == ([("ADDFUEL", "firepit", "log")] if profile == "play" else []), (
        actions
    )
    assert walls == (["wall_wood_item"] if profile == "play" else []), walls
    assert bindings == ([] if profile == "off" else ["resurrectionstatue"]), bindings
    assert contacts == (
        [
            ("KU_TRACE_PROBE", "spider", "ordinary", "spear"),
            ("spider", "KU_TRACE_PROBE", "ordinary", None),
            ("KU_TRACE_PROBE", "wall_wood", "ordinary", "spear"),
            ("KU_TRACE_PROBE", "KU_TRACE_PEER", "pvp", "spear"),
        ]
        if profile == "play"
        else []
    ), contacts


def stop(binary: Path, root: Path, report: dict, process: subprocess.Popen) -> None:
    result = call(binary, root, report, "stop", {"notice": None}, timeout=120)
    for shard in result["shards"]:
        assert shard["result"]["status"] == "success", result
        value = shard["result"]["value"]
        assert value["returncode"] == 0, result
        assert value["output_drained"], result
        assert not value["forced"], result
    assert process.wait(timeout=150) == 0
    assert not (root / "cluster/.dst-agent.sock").exists()
    report["exits"].append(result)
