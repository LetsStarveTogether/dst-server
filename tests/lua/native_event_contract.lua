local lua_root = assert(arg[1], "driver Lua root is required")
local native_root = assert(arg[2], "native DST scripts root is required")
local case = assert(arg[3], "native event case is required")
local profile = assert(arg[5], "telemetry profile is required")
package.path = lua_root .. "/?.lua;" .. native_root .. "/?.lua;" .. package.path

-- Load the shipped game code, including its real JSON encoder and Class semantics.
require("class")
Entity = {}
require("entityscript")
require("stategraph")
require("vector3")
require("util")
json = require("json")
require("bufferedaction")
nolineprint = print
local native = dofile(assert(arg[4], "extracted native handlers are required"))

local function entity(prefab, guid, userid)
    local tags = { player = userid ~= nil }
    return setmetatable({
        prefab = prefab,
        GUID = guid,
        userid = userid,
        components = {},
        Transform = {
            GetWorldPosition = function() return 1, 0, 2 end,
            SetPosition = function() end,
        },
        GetPosition = function()
            return { x = 1, y = 0, z = 2, Get = function() return 1, 0, 2 end }
        end,
        HasTag = function(_, tag) return tags[tag] == true end,
        AddTag = function(_, tag) tags[tag] = true end,
        RemoveTag = function(_, tag) tags[tag] = nil end,
        IsValid = function() return true end,
        WatchWorldState = function() end,
    }, { __index = EntityScript })
end

local player = entity("wilson", 101, "KU_PLAYER")
local attacker = entity("wendy", 102, "KU_ATTACKER")
local target = entity("campfire", 201)
local twigs = entity("twigs", 301)
local flint = entity("flint", 302)

-- Queued or completed actions must never become causal evidence for native events.
player.bufferedaction = { _dst_action_seq = 991 }
attacker.bufferedaction = { _dst_action_seq = 992 }
TheWorld = entity("forest", 1)
TheWorld.ismastersim = true
TheWorld.state = { cycles = 3 }
TheWorld.meta = { session_identifier = "SESSION" }
TheNet = { GetSessionIdentifier = function() return "SESSION" end }
AllPlayers = { player, attacker }
GetTime = function() return 1 end
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
Shard_UpdateWorldState = function(...) return ... end
REMOTESHARDSTATE = { READY = 1 }
EQUIPSLOTS = { HANDS = "hands", BODY = "body" }
FALLINGREASON = { OCEAN = 1, VOID = 2 }

local driver = require("dst_server")
local health = driver.install({
    nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    generation = 1,
    profile = profile,
    actions = { "DEPLOY" },
})
assert(health.telemetry_status == (profile == "off" and "disabled" or "active"), tostring(health.last_error))

local cases = {}

local function position_action(mode)
    local alive, offset = true, 0
    local platform = {
        IsValid = function() return alive end,
        entity = {
            WorldToLocalSpace = function(_, x, y, z) return x - offset, y, z - offset end,
            LocalToWorldSpace = function(_, x, y, z) return x + offset, y, z + offset end,
        },
    }
    TheWorld.Map = { GetPlatformAtPoint = function()
        return mode ~= "ground" and platform or nil
    end }
    local calls, validations = 0, 0
    local current = BufferedAction(player, nil, {
        id = "DEPLOY", fn = function() calls = calls + 1; return true end,
    }, nil, mode ~= "absent" and Vector3(10, 0, 20) or nil)
    local position = current.pos
    local local_point = position ~= nil and position.local_pt or nil
    local initial_platform = position ~= nil and position.walkable_platform or nil
    alive, offset = mode ~= "removed", 100
    current.validfn = function() validations = validations + 1; return true end

    assert(current:Do() == alive, "telemetry must preserve native action validity")
    assert(calls == (alive and 1 or 0))
    assert(validations == (alive and 1 or 0), "capture must not call validfn")
    assert(current.pos == position)
    if position ~= nil then
        assert(position.local_pt == local_point and position.walkable_platform == initial_platform,
            "capture must not mutate DynamicPosition")
    end
end
cases.action_platform_removed = function() position_action("removed") end
cases.action_platform_moved = function() position_action("moved") end
cases.action_ground = function() position_action("ground") end
cases.action_without_position = function() position_action("absent") end

local function work(action, removed)
    local component = {
        inst = target,
        workleft = 1,
        action = action,
        onfinish = removed and function(inst)
            inst.Transform.GetWorldPosition = function() return nil, nil, nil end
        end or nil,
    }
    require("components/workable").WorkedBy_Internal(component, player, 1)
    assert(component.workleft == 0 and component.lastworker == player)
end
cases.work_without_action = function() work(nil, false) end
cases.work_with_action = function() work({ id = "CHOP" }, false) end
cases.work_removed_position = function() work({ id = "CHOP" }, true) end

cases.unequip_empty = function()
    local inventory = { inst = player, equipslots = {} }
    assert(require("components/inventory").Unequip(inventory, "hands") == nil)
end
cases.unequip_item = function()
    twigs.components.stackable = { StackSize = function() return 3 end }
    local inventory = { inst = player, equipslots = { hands = twigs } }
    assert(require("components/inventory").Unequip(inventory, "hands", true) == twigs)
    assert(inventory.equipslots.hands == nil)
end

local function migrate(portal_id, destination)
    local component = {
        inst = target,
        id = portal_id,
        linkedWorld = destination,
        FX_OVERRIDES = {},
    }
    local success, reason = require("components/worldmigrator").Activate(component, player)
    assert(success == (destination ~= nil))
    assert(reason == (destination == nil and "NODESTINATION" or nil))
end
cases.migrate_number = function() migrate(7, "2") end
cases.migrate_string = function() migrate("oceanwhirlbigportal", "2") end
cases.migrate_without_destination = function() migrate(7, nil) end

local function combat(damage, blocked, health)
    local component = {
        inst = player,
        ShouldRecoil = function(_, _, _, incoming) return blocked, incoming end,
        externaldamagetakenmultipliers = { Get = function() return 1 end },
    }
    if health then
        player.components.health = {
            IsDead = function() return false end,
            IsInvincible = function() return false end,
            DoDelta = function(_, amount)
                assert(amount == -10)
                return -7.5
            end,
        }
    end
    assert(require("components/combat").GetAttacked(component, attacker, damage) == not blocked)
end
cases.combat_without_damage = function() combat(nil, false, false) end
cases.combat_resolved_damage = function() combat(10, false, true) end
cases.combat_blocked = function() combat(10, true, false) end

local function wilson_stategraph()
    ModManager = {
        GetPostInitData = function() return {} end,
        GetPostInitFns = function() return {} end,
    }
    -- Keep the native handlers and stategraph machinery; omit animation/physics state bodies.
    local graph = StateGraph("native_incidents", {
        State({ name = "idle" }),
        State({ name = "sink", tags = { "drowning" } }),
        State({ name = "sink_fast", tags = { "drowning" } }),
        State({ name = "abyss_fall", tags = { "falling" } }),
        State({ name = "weregoose_takeoff" }),
    }, native.events, "idle")
    player.sg = StateGraphInstance(graph, player)
    SGManager:AddInstance(player.sg)
    player.sg:GoToState("idle")
    assert(driver.health().events_emitted == 0, "ordinary newstate must not be an incident")
end

local function drown(reason, rejected, boat, weregoose)
    wilson_stategraph()
    player.components.health = {
        IsDead = function() return false end,
        IsInvincible = function() return false end,
    }
    TheWorld.Map = {
        IsInMapBounds = function() return true end,
        IsVisualGroundAtPoint = function() return rejected or reason == nil end,
        IsOceanTileAtPoint = function() return reason == FALLINGREASON.OCEAN end,
        IsInvalidTileAtPoint = function() return reason == FALLINGREASON.VOID end,
    }
    local component = setmetatable({ inst = player, enabled = true }, {
        __index = require("components/drownable"),
    })
    player.components.drownable = component
    if weregoose then
        player:AddTag("weregoose")
        FindRandomPointOnShoreFromOcean = function() return 10, 0, 20 end
    end
    if rejected or boat then
        player:PushEvent(reason == FALLINGREASON.OCEAN and "onsink" or "onfallinvoid", { boat = boat })
    else
        assert(component:CheckDrownable() == (reason ~= nil))
    end
    assert(driver.health().events_emitted == 0, "a queued request is not an incident")
    player.sg:HandleEvents()
    local expected = (rejected or reason == nil) and "idle"
        or weregoose and "weregoose_takeoff"
        or reason == FALLINGREASON.OCEAN and (boat and "sink" or "sink_fast")
        or "abyss_fall"
    assert(player.sg.currentstate.name == expected)
end
cases.drown_ocean = function() drown(FALLINGREASON.OCEAN) end
cases.drown_void = function() drown(FALLINGREASON.VOID) end
cases.drown_safe = function() drown(nil) end
cases.drown_boat = function() drown(FALLINGREASON.OCEAN, false, target) end
cases.drown_ocean_rejected = function() drown(FALLINGREASON.OCEAN, true) end
cases.drown_void_rejected = function() drown(FALLINGREASON.VOID, true) end
cases.drown_weregoose = function() drown(FALLINGREASON.VOID, false, nil, true) end
cases.direct_sink = function()
    wilson_stategraph()
    player.sg:GoToState("sink_fast")
end
cases.direct_fall = function()
    wilson_stategraph()
    player.sg:GoToState("abyss_fall")
end

cases.eat_soul = function()
    local soul = entity("wortox_soul", 401)
    soul.components.soul = {}
    local removed = false
    soul.Remove = function() removed = true end
    assert(require("components/souleater").EatSoul({ inst = player }, soul))
    assert(removed)
end

local function spawn(mode)
    local noop = function() end
    TUNING = { EXTRA_STARTING_ITEMS = {}, SEASONAL_STARTING_ITEMS = {} }
    FRAMES = 1 / 30
    Ents = { [player.GUID] = player }
    SerializeUserSession = noop
    SpawnPrefab = function() return nil end
    TheWorld.state.isday = true
    TheWorld.state.season = "autumn"
    TheWorld.topology = {}
    TheWorld.components.playerspawner = require("components/playerspawner")(TheWorld)
    TheWorld:PushEvent("ms_setspawnmode", mode)
    player.components.skinner = {
        SetClothing = noop, SetSkinName = noop, SetSkinMode = noop, SetSkinOverrides = noop,
    }
    player.components.skilltreeupdater = { SetPlayerSkillSelection = noop }
    player.components.playercontroller = { Enable = noop }
    local teleports, scheduled = 0, 0
    player.Physics = {
        Teleport = function(_, x, y, z)
            assert(x == 0 and y == 0 and z == 0)
            teleports = teleports + 1
        end,
    }
    player.AnimState = { SetMultColour = noop }
    player.Hide = noop
    player.DoStaticTaskInTime = function() scheduled = scheduled + 1 end
    TheWorld:ListenForEvent("ms_newplayerspawned", function(_, inst)
        assert(inst == player and teleports == 0, "new spawn precedes positioning")
    end)
    player.OnNewSpawn = native.on_new_spawn
    native.spawn_new_player(player.GUID)
    assert(player.OnNewSpawn == nil and teleports == 1)
    assert(scheduled == (mode == "fixed" and 0 or 1))
end
cases.spawn_fixed = function() spawn("fixed") end
cases.spawn_scatter = function() spawn("scatter") end

local function pick(product, array, empty)
    player.components.inventory = {}
    SpawnPrefab = function(prefab)
        assert(prefab == "twigs")
        return not empty and twigs or nil
    end
    local component = {
        inst = target,
        product = product,
        numtoharvest = 1,
        use_lootdropper_for_product = array,
    }
    if array then
        target.components.lootdropper = {
            GenerateLoot = function() return empty and {} or { "twigs", "flint" } end,
            SpawnLootPrefab = function(_, prefab) return prefab == "twigs" and twigs or flint end,
        }
    end
    local loot = require("components/pickable").SpawnProductLoot(component, player)
    if empty then
        assert(loot == nil or #loot == 0)
    elseif array then
        assert(#loot == 2 and loot[1] == twigs and loot[2] == flint)
    else
        assert(loot == twigs)
    end
end
cases.pick_single = function() pick("twigs", false, false) end
cases.pick_stack = function()
    twigs.components.stackable = {
        StackSize = function() return 3 end,
        SetStackSize = function() error("native picker must preserve an existing stack") end,
    }
    pick("twigs", false, false)
end
cases.pick_array = function() pick(nil, true, false) end
cases.pick_empty_array = function() pick(nil, true, true) end
cases.pick_missing_product = function() pick(nil, false, true) end
cases.pick_failed_spawn = function() pick("twigs", false, true) end

assert(cases[case], "unknown native case: " .. case)()
print("NATIVE_HEALTH|" .. json.encode(driver.health()))
