local root, native = assert(arg[1]), assert(arg[2])
package.path = root .. "/resources/lua/?.lua;" .. native .. "/?.lua;" .. package.path
json = require("json")
package.preload["dst_server.player_values"] = function()
    return { player = function(client, player) return { userid = client.userid, loaded = player ~= nil } end }
end
local state = require("dst_server.state")
state.installed = true
state.generation = 7
TheWorld = { ismastershard = true, meta = { session_identifier = "SESSION" } }
local teleports = 0
local player = {
    userid = "KU_TEST", GUID = 17, prefab = "wilson",
    IsValid = function() return true end,
    GetDisplayName = function() return "Test" end,
    Physics = { Teleport = function() teleports = teleports + 1 end },
}
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
assert(teleport("OLD", 17).error == "stale_reference" and teleports == 1)
assert(teleport("SESSION", 18).error == "stale_reference" and teleports == 1)
assert(teleport("SESSION", 17, 6).error == "stale_reference" and teleports == 1)
player._despawning = true
assert(teleport("SESSION", 17).error == "stale_reference" and teleports == 1)
local departing = driver.call("locate_player", { userid = "KU_TEST" })
assert(departing.departing and departing.guid == 17 and departing.session_id == "SESSION")
local loading = driver.call("locate_player", { userid = "KU_LOADING" })
assert(loading.guid == json.null and loading.player.loaded == false)
assert(driver.call("locate_player", { userid = "KU_GONE" }).player == json.null)
player._despawning = false
player = nil
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
print("ok")
