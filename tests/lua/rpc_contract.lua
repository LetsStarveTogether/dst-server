local lua_root = assert(arg[1], "Lua module root is required")
local test_root = assert(arg[2], "Lua test module root is required")
package.path = table.concat({ lua_root, test_root }, "/?.lua;") .. "/?.lua;" .. package.path

json = require("json_contract")
local observed = { world_events = {} }
local blacklist = {
    { userid = "KU_BLOCKED", netid = "Steam_BLOCKED" },
    { userid = "", netid = "Steam_ONLY" },
    { userid = "KU_KEEP", netid = "Steam_KEEP" },
}
local whitelist = { KU_TEST = true }

local function component(values)
    return setmetatable(values, {
        __index = function(_, name)
            return function() return 0 end
        end,
    })
end

local function item(prefab, guid)
    return {
        prefab = prefab,
        GUID = guid,
        GetSkinName = function() return "classic" end,
        components = {
            stackable = { StackSize = function() return 3 end },
            inventoryitem = component({ GetMoisturePercent = function() return 0.1 end }),
            finiteuses = component({ GetPercent = function() return 0.8 end }),
            perishable = component({ GetPercent = function() return 0.7 end }),
            fueled = component({ GetPercent = function() return 0.6 end }),
            armor = component({ GetPercent = function() return 0.5 end }),
            rechargeable = component({ GetPercent = function() return 0.4 end }),
        },
    }
end

local inventory_item = item("twigs", 100)
local equipped_item = item("axe", 101)
local active_item = item("torch", 102)
local overflow_item = item("rocks", 103)
local inventory = {
    itemslots = { [1] = inventory_item },
    equipslots = { hands = equipped_item },
    activeitem = active_item,
    GetOverflowContainer = function()
        return {
            inst = { prefab = "backpack", GUID = 104 },
            slots = { [2] = overflow_item },
        }
    end,
    GiveItem = function(_, value) observed.given_prefab = value.prefab end,
    Has = function(_, prefab, count)
        observed.remove_query = { prefab, count }
        return true, count
    end,
    ConsumeByName = function(_, prefab, count)
        observed.removed = { prefab, count }
    end,
}

local player = {
    prefab = "wilson",
    GUID = 42,
    userid = "KU_TEST",
    Transform = {
        GetWorldPosition = function() return 1, 0, 2 end,
        SetPosition = function() end,
    },
    Physics = {
        Teleport = function(_, x, y, z) observed.teleported = { x, y, z } end,
    },
    components = {
        age = component({
            GetAge = function() return 100 end,
            GetAgeInDays = function() return 2 end,
            GetDisplayAgeInDays = function() return 3 end,
        }),
        combat = { target = nil, GetWeapon = function() return nil end },
        rider = { GetMount = function() return nil end },
        leader = { followers = {} },
        skilltreeupdater = component({
            GetSkillXP = function() return 10 end,
            GetAvailableSkillPoints = function() return 2 end,
            GetActivatedSkills = function() return { wilson_torch_1 = true } end,
        }),
        health = component({
            currenthealth = 80,
            maxhealth = 100,
            GetPercent = function() return 0.8 end,
            IsDead = function() return false end,
            IsInvincible = function() return false end,
            ForceKill = function() observed.killed = true end,
            SetPercent = function(_, value) observed.health = value end,
        }),
        hunger = component({
            current = 50,
            max = 100,
            GetPercent = function() return 0.5 end,
            SetPercent = function(_, value) observed.hunger = value end,
        }),
        sanity = component({
            current = 60,
            max = 100,
            GetPercent = function() return 0.6 end,
            SetPercent = function(_, value) observed.sanity = value end,
        }),
        temperature = component({
            GetCurrent = function() return 25 end,
            GetMax = function() return 70 end,
            SetTemperature = function(_, value) observed.temperature = value end,
        }),
        moisture = component({
            GetMoisture = function() return 10 end,
            GetMaxMoisture = function() return 100 end,
            GetMoisturePercent = function() return 0.1 end,
            SetPercent = function(_, value) observed.moisture = value end,
        }),
        inventory = inventory,
    },
    HasTag = function(_, tag) return tag == "player" end,
    GetDisplayName = function() return "Wilson" end,
    IsValid = function() return true end,
    PushEvent = function(_, name) observed.player_event = name end,
    DoTaskInTime = function(value, _, callback) callback(value) end,
}

TheWorld = {
    ismastersim = true,
    ismastershard = true,
    meta = {
        session_identifier = "SESSION",
        build_version = "123456",
        saveversion = 5,
        generated_on_saveversion = 4,
        seed = 7,
        level_id = "SURVIVAL_TOGETHER",
    },
    state = {
        cycles = 10,
        time = 0.5,
        timeinphase = 0.25,
        phase = "day",
        isday = true,
        isdusk = false,
        isnight = false,
        moonphase = "full",
        iswaxingmoon = false,
        isfullmoon = true,
        isnewmoon = false,
        season = "autumn",
        isspring = false,
        issummer = false,
        isautumn = true,
        iswinter = false,
        elapseddaysinseason = 2,
        seasonprogress = 0.1,
        remainingdaysinseason = 18,
        springlength = 20,
        summerlength = 15,
        autumnlength = 20,
        winterlength = 15,
        temperature = 25,
        moisture = 10,
        moistureceil = 100,
        pop = 0.2,
        precipitationrate = 0,
        precipitation = "none",
        israining = false,
        issnowing = false,
        islunarhailing = false,
        isacidraining = false,
        issnowcovered = false,
        snowlevel = 0,
        lunarhaillevel = 0,
        lunarhailrate = 0,
        wetness = 10,
        iswet = false,
    },
    components = {
        worldstate = { GetWorldAge = function() return 1000 end },
    },
    HasTag = function() return false end,
    PushEvent = function(_, name, value) observed.world_events[name] = value end,
}
TheNet = {
    GetCurrentSnapshot = function() return 26 end,
    GetServerName = function() return "Test Room" end,
    GetServerDescription = function() return "Description" end,
    GetServerGameMode = function() return "survival" end,
    GetServerPlaystyle = function() return nil end,
    GetServerMaxPlayers = function() return 6 end,
    GetServerPVP = function() return false end,
    IsServerPaused = function() return true end,
    GetServerHasPassword = function() return true end,
    GetServerIsDedicated = function() return true end,
    IsOnlineMode = function() return true end,
    GetServerLANOnly = function() return false end,
    GetServerFriendsOnly = function() return false end,
    GetServerModsEnabled = function() return true end,
    GetServerClanID = function() return "123" end,
    GetServerClanOnly = function() return false end,
    Kick = function(_, userid) observed.kicked = userid end,
    Ban = function(_, userid) observed.banned = { userid } end,
    BanForTime = function(_, userid, seconds) observed.banned = { userid, seconds } end,
    GetBlacklist = function() return blacklist end,
    SetBlacklist = function(_, value)
        blacklist = value
        observed.blacklist = value
        observed.set_blacklist_calls = (observed.set_blacklist_calls or 0) + 1
    end,
    IsWhiteListed = function(_, userid) return whitelist[userid] == true end,
    AddToWhiteList = function(_, userid) whitelist[userid] = true end,
    RemoveFromWhiteList = function(_, userid) whitelist[userid] = nil end,
}
TheShard = { GetShardId = function() return 1 end }
BRANCH = "release"
APP_VERSION = "1.0"
ModManager = { GetEnabledModNames = function() return { "workshop-1" } end }
KnownModIndex = {
    GetModInfo = function() return { name = "Test Mod", version = "1.2.3" } end,
}
GetPlayerClientTable = function()
    return {
        {
            userid = "KU_TEST",
            name = "Wilson",
            prefab = "wilson",
            admin = true,
            moderator = false,
            netscore = 12,
        },
        {
            userid = "KU_LOADING",
            name = "Loading",
            admin = false,
            moderator = false,
        },
    }
end
LookupPlayerInstByUserID = function(userid)
    return userid == "KU_TEST" and player or nil
end
Shard_GetConnectedShards = function()
    return { [2] = { shard_name = "Caves", ready = true, tags = "cave" } }
end
Shard_IsWorldAvailable = function() return true end
PrefabExists = function(prefab)
    observed.prefab_exists = prefab
    return true
end
SpawnPrefab = function(prefab)
    observed.spawned = (observed.spawned or 0) + 1
    return item(prefab, 200 + observed.spawned)
end
SetServerPaused = function(paused) observed.paused = paused end
c_announce = function(message) observed.announcement = message end
c_save = function() observed.saved = true end
c_reset = function() observed.reset = true end
c_regenerateworld = function() observed.regenerated = true end
c_regenerateshard = function(erase) observed.regenerated_shard = erase end
c_rollback = function(count) observed.rollback = count end

local driver = require("dst_server")
driver.install({ nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV", profile = "off", actions = {} })

local calls = {
    { "health", {} },
    { "get_room", {} },
    { "get_world", {} },
    { "get_runtime", {} },
    { "get_mods", {} },
    { "get_shards", { current_name = "Master" } },
    { "get_players", {} },
    { "get_player", { userid = "KU_LOADING" } },
    { "get_player_inventory", { userid = "KU_TEST" } },
    { "announce", { message = "hello" } },
    { "save", {} },
    { "set_server_paused", { paused = true } },
    { "reset", {} },
    { "regenerate_world", {} },
    { "regenerate_shard", { preserve_settings = true } },
    { "rollback", { count = 1 } },
    { "kick_player", { userid = "KU_TEST" } },
    { "ban_player", { userid = "KU_TEST", seconds = 60 } },
    { "get_blocklist", {} },
    { "is_blocked", { userid = "Steam_BLOCKED" } },
    { "unban_player", { userid = "KU_BLOCKED" } },
    { "is_whitelisted", { userid = "KU_TEST" } },
    { "whitelist_player", { userid = "KU_FRIEND" } },
    { "unwhitelist_player", { userid = "KU_TEST" } },
    { "set_player_vitals", { userid = "KU_TEST", health = 0.5 } },
    { "kill_player", { userid = "KU_TEST" } },
    { "revive_player", { userid = "KU_TEST" } },
    { "despawn_player", { userid = "KU_TEST" } },
    { "migrate_player", { userid = "KU_TEST", shard_id = "2", portal_id = 1 } },
    { "teleport_player", { userid = "KU_TEST", x = 1, y = 0, z = 2 } },
    { "give_item", { userid = "KU_TEST", prefab = "twigs", count = 1 } },
    { "remove_item", { userid = "KU_TEST", prefab = "twigs", count = 1 } },
    { "execute_script", { source = "return {answer=42}" } },
}

for _, call in ipairs(calls) do
    print(call[1] .. "|" .. json.encode_compliant({
        ok = true,
        data = driver.call(call[1], call[2]),
    }))
end

assert(observed.announcement == "hello")
assert(observed.paused == true)
assert(observed.regenerated_shard == false)
assert(observed.rollback == 1)
assert(observed.kicked == "KU_TEST")
assert(observed.banned[1] == "KU_TEST" and observed.banned[2] == 60)
assert(#observed.blacklist == 2 and observed.blacklist[1].netid == "Steam_ONLY"
    and observed.blacklist[2].userid == "KU_KEEP")
assert(observed.set_blacklist_calls == 1)
assert(whitelist.KU_FRIEND == true and whitelist.KU_TEST == nil)
assert(driver.call("is_blocked", { userid = "KU_BLOCKED" }) == false)
assert(driver.call("unban_player", { userid = "KU_MISSING" }) == false)
assert(observed.set_blacklist_calls == 1)
local remaining = driver.call("get_blocklist", {})
assert(#remaining == 2 and remaining[1] == "KU_KEEP"
    and remaining[2] == "Steam_ONLY")
assert(driver.call("is_whitelisted", { userid = "KU_TEST" }) == false)
assert(observed.health == 0.5)
assert(observed.killed == true)
assert(observed.world_events.ms_playerdespawnanddelete == player)
local migration = observed.world_events.ms_playerdespawnandmigrate
assert(migration.player == player and migration.worldid == "2" and migration.portalid == 1)
assert(observed.teleported[1] == 1 and observed.teleported[2] == 0
    and observed.teleported[3] == 2)
assert(observed.given_prefab == "twigs")
assert(observed.remove_query[1] == "twigs" and observed.remove_query[2] == 1)
assert(observed.removed[1] == "twigs" and observed.removed[2] == 1)

local spawned = observed.spawned
assert(driver.call("give_item", {
    userid = "KU_TEST",
    prefab = "twigs",
    count = 64,
}) == 64)
assert(observed.spawned == spawned + 64)
local accepted = pcall(driver.call, "give_item", {
    userid = "KU_TEST",
    prefab = "twigs",
    count = 65,
})
assert(not accepted and observed.spawned == spawned + 64)
