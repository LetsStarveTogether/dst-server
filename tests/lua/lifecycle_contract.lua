local root, scripts = assert(arg[1]), assert(arg[2])
package.path = root .. "/resources/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
json = require("json")
require("class")
Entity = {}
require("entityscript")

local outputs = {}
dofile(root .. "/tests/lua/capture_remote.lua")(function(line) outputs[#outputs + 1] = line end)
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local function entity(prefab, guid, userid)
    return setmetatable({
        prefab = prefab, GUID = guid, userid = userid, components = {},
        IsValid = function() return true end,
        HasTag = function(_, tag) return tag == "player" and userid ~= nil end,
        Transform = { GetWorldPosition = function() return 1, 0, 2 end },
        WatchWorldState = function() end,
    }, { __index = EntityScript })
end
local player = entity("winona", 2, "KU_PLAYER")
TheWorld = entity("forest", 1)
TheWorld.state = { cycles = 3 }
TheWorld.meta = { session_identifier = "SESSION" }
TheWorld.ismastersim = true
AllPlayers = { player }
local state = require("dst_server.state")
state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 1
state.requested_profile, state.telemetry_active = "timeline", true
local world_events = require("dst_server.world_events")
local extensions = require("prefabs/player_common_extensions")
world_events.install_players()
require("dst_server.player_events").install_lifecycle()

player.components.age = {GetAge = function() return 4800.25 end}
player:PushEvent("ms_respawnedfromghost")
assert(json.decode(outputs[#outputs]:sub(10)).data.age_seconds == 4800.25)
player.components.age.GetAge = function() error("mod age unavailable") end
player:PushEvent("ms_respawnedfromghost", { corpse = true, reviver = player })
assert(#outputs == 2)
assert(json.decode(outputs[#outputs]:sub(10)).data.age_seconds == nil)
player.components.age.GetAge = function() return 4800.25 end

-- Exercise the native vine rescue through the actual health restoration.
TUNING = { SANITY_BECOME_SANE_THRESH = .3, STARTING_TEMP = 20, RESURRECT_HEALTH = 50 }
STRINGS = { NAMES = { CHARLIE = "Charlie" } }
GetNewRezAnnouncementString = function() return "Charlie rescue" end
local announcements = 0
TheNet = { AnnounceResurrect = function() announcements = announcements + 1 end }
local noop = function() end
player.charlie_vinesave = true
player.components.inventory = { Show = noop }
player.components.burnable = { Extinguish = noop }
player.components.freezable = { Reset = noop }
player.components.grogginess = { ResetGrogginess = noop }
player.components.moisture = { ForceDry = noop }
player.components.temperature = { SetTemperature = noop }
player.components.debuffable = { Enable = noop }
player.components.sanity = { GetRealPercent = function() return 1 end }
player.components.hunger = { GetPercent = function() return 1 end }
player.components.health = {
    SetCurrentHealth = function(self, value) self.currenthealth = value end,
    ForceUpdateHUD = noop,
}
extensions.OnRespawnFromVineSave(player)
assert(not player.charlie_vinesave)
assert(player.components.health.currenthealth == 50 and announcements == 1)
assert(#outputs == 3, "a vine rescue emits one revival")
local rescue = json.decode(outputs[#outputs]:sub(10)).data
assert(rescue.method == "charlie" and rescue.age_seconds == 4800.25)
local failure = {}
player.components.inventory.Show = function() error(failure, 0) end
local ok, err = pcall(extensions.OnRespawnFromVineSave, player)
assert(not ok and err == failure and #outputs == 3)

io.write("ok\n")
