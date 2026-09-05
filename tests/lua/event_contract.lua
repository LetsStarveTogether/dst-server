local lua_root = assert(arg[1], "Lua module root is required")
local test_root = assert(arg[2], "Lua test module root is required")
local native_root = assert(arg[3], "Native Lua module root is required")
package.path = lua_root .. "/?.lua;" .. native_root .. "/?.lua;" .. package.path

json = require("json")
nolineprint = print
require("class")
require("vector3")
require("util")

local world_listeners = {}
local world_watchers = {}
local player_listeners = {}

local function entity(prefab, guid, userid, x, z, tags)
    return {
        prefab = prefab,
        GUID = guid,
        userid = userid,
        Transform = {
            GetWorldPosition = function() return x, 0, z end,
        },
        HasTag = function(_, tag)
            return tag == "player" and userid ~= nil or tags ~= nil and tags[tag] == true
        end,
    }
end

local function item(prefab, guid)
    local value = entity(prefab, guid, nil, 1, 2)
    value.GetSkinName = function() return "classic" end
    value.components = {
        stackable = { StackSize = function() return 3 end },
    }
    return value
end

local player = entity("wilson", 42, "KU_TEST", 1, 2)
player.ListenForEvent = function(_, name, callback)
    player_listeners[name] = callback
end

TheWorld = {
    ismastersim = true,
    Map = { GetPlatformAtPoint = function() end },
    meta = { session_identifier = "SESSION" },
    state = { cycles = 2 },
    ListenForEvent = function(_, name, callback)
        world_listeners[name] = callback
    end,
    WatchWorldState = function(_, name, callback)
        world_watchers[name] = callback
    end,
    HasTag = function() return false end,
}
AllPlayers = { player }
BufferedAction = {
    Do = function() return true, "worked" end,
}
Shard_UpdateWorldState = function(...) return ... end
REMOTESHARDSTATE = { READY = 1 }
GetTick = function() return 10 end
GetTimeReal = function() return 20 end

local driver = require("dst_server")
local health = driver.install({
    nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    generation = 1,
    profile = "history",
    actions = { "CHOP" },
})
assert(health.telemetry_status == "active")

local target = entity("hound", 44, nil, 3, 4)
local redirected = entity("wall", 45, nil, 5, 6)
local spear = item("spear", 43)
local loot = item("twigs", 46)
local action = {
    action = { id = "CHOP" },
    doer = player,
    target = target,
    initialtargetowner = redirected,
    invobject = spear,
    GetDynamicActionPoint = function()
        return DynamicPosition(Vector3(7, 0, 8))
    end,
    recipe = "axe",
    forced = true,
}
assert(BufferedAction.Do(action) == true)
player.bufferedaction = action

world_listeners.ms_playerjoined(TheWorld, player)
world_listeners.ms_playerleft(TheWorld, player)
world_listeners.ms_playerdisconnected(TheWorld, {
    player = player,
    wasExpected = true,
})
world_listeners.ms_playerdespawnandmigrate(TheWorld, {
    player = player,
    worldid = 2,
    portalid = 3,
    x = 9,
    y = 0,
    z = 10,
})
world_listeners.ms_newplayerspawned(TheWorld, player)
world_listeners.entity_death(TheWorld, {
    inst = player,
    cause = "hound",
    afflicter = player,
    corpsing = true,
})
world_listeners.master_shardbossdefeated(TheWorld, {
    bossprefab = "toadstool",
    shardid = 2,
})
world_listeners.lunarrift_opened(TheWorld)
world_listeners.shadowrift_opened(TheWorld)
world_listeners.ms_riftaddedtopool(TheWorld, { rift = target })
world_listeners.ms_riftremovedfrompool(TheWorld, { rift = target })
world_listeners.itemplanted(TheWorld, {
    doer = player,
    pos = { x = 11, y = 0, z = 12 },
})

for name, value in pairs({
    cycles = 3,
    phase = "day",
    season = "autumn",
    moonphase = "full",
    nightmarephase = "calm",
    israining = true,
    issnowing = false,
    isacidraining = true,
    islunarhailing = false,
}) do
    world_watchers[name](TheWorld, value)
end

Shard_UpdateWorldState("2", REMOTESHARDSTATE.READY, "cave,secondary", {}, "Caves")

player_listeners.ms_becameghost(player, { corpse = true })
player_listeners.ms_respawnedfromghost(player, {
    corpse = true,
    reviver = redirected,
})
player_listeners.newstate(player, { statename = "idle" })
player_listeners.newstate(player, { statename = "sink" })
player_listeners.newstate(player, { statename = "sink_fast" })
player_listeners.newstate(player, { statename = "abyss_fall" })

local combat = {
    damage = 12.5,
    weapon = spear,
    stimuli = "electric",
    spdamage = { planar = 1.25, shadow = 2 },
    target = target,
    attacker = target,
    damageresolved = 10,
    original_damage = 14,
    redirected = redirected,
}
player_listeners.onhitother(player, combat)
player_listeners.attacked(player, combat)
player_listeners.blocked(player, combat)

for _, name in ipairs({
    "startstarving",
    "stopstarving",
    "startfreezing",
    "stopfreezing",
    "startoverheating",
    "stopoverheating",
    "startfiredamage",
    "stopfiredamage",
    "startlunarburn",
    "stoplunarburn",
    "gosane",
    "goinsane",
    "goenlightened",
}) do
    player_listeners[name](player)
end

player_listeners.fishingcollect(player, { fish = loot })
player_listeners.fishcaught(player, { fish = loot })
player_listeners.onactivateskill_server(player, { skill = "wilson_torch_1" })
player_listeners.ondeactivateskill_server(player, { skill = "wilson_torch_1" })
player_listeners.houndwarning(player, 2)

local crafted = { item = loot, recipe = { name = "axe" }, skin = "classic" }
player_listeners.builditem(player, crafted)
player_listeners.buildstructure(player, crafted)
player_listeners.oneat(player, { food = loot, feeder = redirected })
player_listeners.oneatsoul(player, { soul = loot })
player_listeners.picksomething(player, { object = target, loot = { loot } })
player_listeners.harvestsomething(player, { object = target })
player_listeners.finishedwork(player, {
    target = target,
    action = { id = "CHOP" },
})
player_listeners.deployitem(player, { prefab = "pinecone" })
player_listeners.equip(player, { item = spear, eslot = "hands" })
player_listeners.unequip(player, { item = spear, eslot = "hands", slip = true })
player_listeners.dropitem(player, { item = spear })

health = driver.health()
assert(health.events_emitted == 60 and health.errors == 0)
