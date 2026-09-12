local root, scripts, native, profile = arg[1], arg[2], arg[3], arg[4]
package.path = root .. "/src/dst_server/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
json = require("json")
require("class")
Entity = {}
require("entityscript")
dofile(native)

local outputs = {}
nolineprint = function(line) outputs[#outputs + 1] = line end
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local function entity(prefab, guid, userid)
    return setmetatable({
        prefab = prefab, GUID = guid, userid = userid, components = {},
        IsValid = function() return true end,
        HasTag = function() return false end,
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
TheMixer = { PushMix = function() end, DeleteMix = function() end }
TheHaptics = { PauseEffects = function() end }
local state = require("dst_server.state")
state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 1
state.requested_profile = profile
state.telemetry_active = profile ~= "off"
local world_events = require("dst_server.world_events")
local extensions = require("prefabs/player_common_extensions")
local original = extensions.OnRespawnFromVineSave
world_events.install_players()
if profile == "off" then
    assert(extensions.OnRespawnFromVineSave == original)
    return
end
world_events.install_world()

player:PushEvent("ms_respawnedfromghost")
player:PushEvent("ms_respawnedfromghost", { corpse = true, reviver = player })
assert(#outputs == 2)

-- Execute the shipped rescue function: a restored body, not a rescue request.
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
local result = require("dst_server.telemetry").pack(extensions.OnRespawnFromVineSave(player))
assert(result.n == 0 and not player.charlie_vinesave)
assert(player.components.health.currenthealth == 50 and announcements == 1)
assert(#outputs == 3, "a vine rescue emits one revival")
local failure = {}
player.components.inventory.Show = function() error(failure, 0) end
local ok, err = pcall(extensions.OnRespawnFromVineSave, player)
assert(not ok and err == failure and #outputs == 3)

-- The native server callback publishes its flags; the simulation callback is distinct.
OnServerPauseDirty(true, false, false, "玩家")
OnServerPauseDirty(false, true, false, nil)
OnServerPauseDirty(false, false, true, nil)
OnServerPauseDirty(false, false, false, nil)
local unpaused = 0
TheWorld:ListenForEvent("ms_simunpaused", function() unpaused = unpaused + 1 end)
OnSimPaused()
OnSimUnpaused()
assert(#outputs == 9 and unpaused == 1, "native simulation effects run once")
local push = TheWorld.PushEvent
TheWorld.PushEvent = function() error(failure, 0) end
ok, err = pcall(OnSimUnpaused)
assert(not ok and err == failure and #outputs == 9)
TheWorld.PushEvent = push

-- Telemetry failure cannot change the native callback's return values or errors.
local emit = require("dst_server.telemetry").emit
require("dst_server.telemetry").emit = function() error("capture failed") end
OnSimUnpaused()
assert(unpaused == 2 and #outputs == 10)
require("dst_server.telemetry").emit = emit
for _, line in ipairs(outputs) do print(line) end

local values = require("dst_server.values")
for _, data in ipairs({ {}, { from_doattack = false }, { from_doattack = true } }) do
    print("COMBAT|" .. json.encode(values.combat_data(player, data, player)))
end
