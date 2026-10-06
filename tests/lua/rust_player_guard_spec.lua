local root, native = assert(arg[1]), assert(arg[2])
package.path = root .. "/resources/lua/?.lua;" .. native .. "/?.lua;" .. package.path
json = require("json")
local state = require("dst_server.state")
state.installed = true
state.generation = 7
TheWorld = { ismastershard = true, meta = { session_identifier = "SESSION" } }
local teleports = 0
local player = {
    userid = "KU_TEST", GUID = 17, prefab = "wilson",
    components = {}, HasTag = function() return false end,
    IsValid = function() return true end,
    Transform = { GetWorldPosition = function() return 1, 0, 2 end },
    Physics = { Teleport = function() teleports = teleports + 1 end },
}
AllPlayers = {player}
LookupPlayerInstByUserID = function(userid) return userid == "KU_TEST" and player or nil end
GetPlayerClientTable = function() return { { userid = "KU_TEST" }, { userid = "KU_LOADING" } } end
local driver = require("dst_server")
local wire = require("dst_server.wire")
local function teleport(session, guid, generation)
    return json.decode(wire.response(function()
        return driver.call("teleport", { userid = "KU_TEST", x = 1, y = 0, z = 2,
            _expected_session_id = session, _expected_guid = guid, _expected_generation = generation or 7 })
    end))
end
assert(teleport("SESSION", 17).ok and teleports == 1)
POSTACTIVATEHANDSHAKE = { STC_SENDINGSTATE = 2, READY = 3 }
player._PostActivateHandshakeState_Server = POSTACTIVATEHANDSHAKE.STC_SENDINGSTATE
local pending = driver.call("locate_player", { userid = "KU_TEST", include_details = true })
assert(pending.loaded == false and pending.guid == 17 and pending.player.prefab == "wilson")
assert(pending.player.vitals == json.null and pending.player.state == json.null)
local summary = driver.call("list_players")[1]
assert(summary.position == json.null and summary.age == json.null and summary.is_ghost == json.null)
assert(teleport("SESSION", 17).error == "stale_reference" and teleports == 1)
player._PostActivateHandshakeState_Server = POSTACTIVATEHANDSHAKE.READY
local ready = driver.call("locate_player", { userid = "KU_TEST", include_details = true })
assert(ready.loaded and ready.player.position.x == 1 and ready.player.vitals ~= json.null)
local clients = GetPlayerClientTable
GetPlayerClientTable = function() error("active routing read the roster") end
player.components.skilltreeupdater = {GetSkillXP = function() error("active routing read character details") end}
local route = driver.call("locate_player", { userid = "KU_TEST" })
assert(route.present and route.loaded and route.guid == player.GUID and route.player == nil)
GetPlayerClientTable, player.components.skilltreeupdater = clients, nil
assert(teleport("OLD", 17).error == "stale_reference" and teleports == 1)
assert(teleport("SESSION", 18).error == "stale_reference" and teleports == 1)
assert(teleport("SESSION", 17, 6).error == "stale_reference" and teleports == 1)
player._despawning = true
assert(teleport("SESSION", 17).error == "stale_reference" and teleports == 1)
local departing = driver.call("locate_player", { userid = "KU_TEST" })
assert(departing.departing and departing.guid == 17 and departing.session_id == "SESSION")
local loading = driver.call("locate_player", { userid = "KU_LOADING" })
assert(loading.present and loading.guid == json.null and loading.player == nil)
local kicked = {}
TheNet = { Kick = function(_, userid) kicked[#kicked + 1] = userid end }
assert(driver.call("kick", { userid = "KU_LOADING" }), "character selection has no entity")
assert(driver.call("kick", { userid = "KU_TEST" }), "migration must not require an active entity")
assert(not driver.call("kick", { userid = "KU_GONE" }) and #kicked == 2)
assert(not driver.call("locate_player", { userid = "KU_GONE" }).present)
player._despawning = false
player = nil
assert(driver.call("kick", { userid = "KU_TEST" }), "loading must not require a GUID")
assert(teleport("SESSION", 17).error == "stale_reference" and teleports == 1)

local largest = 0
TheNet = {
    IsOnlineMode = function() return true end,
    ListSnapshots = function(_, _, _, count)
        largest = math.max(largest, count)
        return { { snapshot_id = 9, world_file = "session/SESSION/0000000009" } }, true
    end,
}
local ok = pcall(driver.call, "list_snapshots", { limit = 1, before = 0 })
assert(not ok and largest == 6400)
largest = 0
ok = pcall(driver.call, "rollback_to_snapshot", { session_id = "SESSION", snapshot_id = 1 })
assert(not ok and largest == 6400)
local target = "session/SESSION/0000000001"
TheNet.ListSnapshots = function(_, _, _, count)
    if count < 6400 then return {}, true end
    return { { snapshot_id = 1, world_file = target } }, true
end
TheNet.GetCurrentSnapshot = function() return 10 end
local truncated, rolled_back = false, false
TheNet.TruncateSnapshots = function(_, session, offset)
    assert(session == "SESSION" and offset == -9)
    truncated = true
end
TheNet.GetWorldSessionFile = function() return target end
WorldRollbackFromSim = function(count)
    assert(count == 0 and truncated)
    rolled_back = true
end
assert(driver.call("rollback_to_snapshot", { session_id = "SESSION", snapshot_id = 1 }))
assert(rolled_back)
print("ok")
