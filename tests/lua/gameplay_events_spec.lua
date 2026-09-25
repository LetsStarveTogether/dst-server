local root, scripts, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
require("class")
require("vector3")
require("util")
Entity = {}
require("entityscript")

local outputs = {}
print = function() end
nolineprint = function(line) outputs[#outputs + 1] = line:sub(10) end
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local state = require("dst_server.state")
state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 4
state.requested_profile = profile
state.telemetry_active = profile ~= "off"
local enabled = profile ~= "off"
local function records(name)
    local result = {}
    for _, line in ipairs(outputs) do
        local record = json.decode(line)
        if record.event == name then result[#result + 1] = record.data end
    end
    return result
end
local function count(name) return #records(name) end
local noop = function() end
local next_guid = 0
Ents = {}
local function entity(prefab, userid)
    next_guid = next_guid + 1
    local inst = setmetatable({
        prefab = prefab, GUID = next_guid, userid = userid, components = {},
        IsValid = function() return true end,
        AddTag = noop,
        Transform = {GetWorldPosition = function() return 1, 0, 2 end},
        StartUpdatingComponent = function(self) self.updating = true end,
        StopUpdatingComponent = function(self) self.updating = false end,
    }, {__index = EntityScript})
    Ents[inst.GUID] = inst
    return inst
end
TheWorld = entity("world")
TheWorld.ismastersim = true
TheWorld.meta = {session_identifier = "SESSION"}
TheWorld.state = {cycles = 2}
local player = entity("wx78", "KU_PLAYER")
local drone = entity("wx78_drone_scout")
local MapDeliverable = require("components/mapdeliverable")
local update, stop, load, reset = MapDeliverable.OnUpdate, MapDeliverable.Stop,
    MapDeliverable.OnLoad, MapDeliverable.Reset_Internal
local capture = require("dst_server.gameplay_events")
if enabled then capture.install_deliveries() end
assert(MapDeliverable.OnUpdate == update and MapDeliverable.Stop == stop
    and MapDeliverable.OnLoad == load and MapDeliverable.Reset_Internal == reset)
local delivery = MapDeliverable(drone)
local destination = Vector3(20, 0, 30)
local delivery_event = "dst.world.map_delivery_started"

delivery:SetOnStartDeliveryFn(function() return false, "blocked" end)
local ok, reason = delivery:SendToPoint(destination, player)
assert(ok == false and reason == "blocked" and #outputs == 0)
delivery:SetOnStartDeliveryFn(nil)
assert(delivery:SendToPoint(destination, player))
assert(not delivery:SendToPoint(Vector3(40, 0, 50), player))
delivery:OnUpdate(2)
assert(drone.updating and delivery.t == 2 and count(delivery_event) == (enabled and 1 or 0))
delivery:OnUpdate(3)
assert(not drone.updating and delivery.t == nil)
delivery:Stop()
assert(count(delivery_event) == (enabled and 1 or 0))

assert(delivery:SendToPoint(destination, player))
delivery:Stop()
local saved = {x0 = 1, z0 = 2, x1 = 20, z1 = 30, t = 2, len = 5}
delivery:OnLoad(saved)
assert(delivery.t == 2 and drone.updating)
delivery:OnUpdate(3)
delivery:SetOnStartDeliveryFn(function() return false end)
delivery:OnLoad(saved)
assert(delivery.t == nil and count(delivery_event) == (enabled and 2 or 0))
delivery:SetOnStartDeliveryFn(nil)

if enabled then
    local rows = records(delivery_event)
    assert(#rows == 2)
    assert(rows[1].actor.userid == "KU_PLAYER" and rows[1].origin.x == 1)
    assert(rows[2].destination.x == 20 and rows[2].actor.userid == "KU_PLAYER")
end

-- Exercise the shipped vault prefab and EntityScript's actual event registration.
Asset = function() return {} end
Prefab = function(name, fn) return {name = name, fn = fn} end
ANIM_ORIENTATION = {OnGround = 1}
LAYER_BACKGROUND = 1
STRINGS = {NAMES = {VAULT_PILLAR_GUARD = "Guard"}}
TUNING = {VAULT_SHADOW_SUPPRESSION_MULT = 1}
TheNet = {IsDedicated = function() return true end}
POPULATING = false
-- Native progress only ORs distinct powers of two; DST supplies this engine API.
bit = {bor = function(a, b) return a + b end}
net_ushortint = function()
    return {set = function(self, value) self.current = value end}
end
CreateEntity = function()
    local inst = entity("vault_key_trial")
    inst.entity = {AddTransform = noop, AddAnimState = noop, AddSoundEmitter = noop,
        AddNetwork = noop, SetPristine = noop}
    inst.AnimState = {SetBank = noop, SetBuild = noop, PlayAnimation = noop, Hide = noop,
        SetOrientation = noop, SetLayer = noop, SetSortOrder = noop, SetFinalOffset = noop}
    inst.SoundEmitter = {PlaySound = noop, KillSound = noop, PlayingSound = function() return false end}
    inst.AddComponent = function(self, name) self.components[name] = require("components/" .. name)(self) end
    inst.DoTaskInTime = function() return {Cancel = noop} end
    inst.DoPeriodicTask = inst.DoTaskInTime
    return inst
end
local native_vault = require("prefabs/vault_key_trial")
Prefabs = {vault_key_trial = native_vault}
local function trial()
    local inst = native_vault.fn()
    local tracker = inst.components.entitytracker
    for i = 1, 2 do
        local socket = entity("vault_crawler_socket")
        socket.IsSocketed = function(self) return self.socketed == true end
        tracker:TrackEntity("socket" .. i, socket)
        local activator = entity("vault_key_activator_plate")
        activator.GotSpark = function(self) return self.spark == true end
        tracker:TrackEntity("activator" .. i, activator)
        local guard = entity("vault_pillar_guard")
        guard.components.damagetypebonus = {AddBonus = noop}
        guard.components.damagetyperesist = {AddResist = noop}
        tracker:TrackEntity("guard" .. i, guard)
    end
    return inst
end
local existing = trial()
existing:OnLoadPostPass({}, {})
local existing_socket = existing.components.entitytracker:GetEntity("socket1")
local progress_event = "dst.world.vault_trial_progress"
local bonus_event = "dst.world.vault_trial_guards_defeated"
local after_native = 0
existing:ListenForEvent("ms_vaultsocketed_changed", function()
    after_native = after_native + 1
    assert(count(progress_event) == (enabled and after_native or 0), "existing callback order changed")
end, existing_socket)
if enabled then capture.install_vault_trials() end
existing_socket.socketed = true
existing_socket:PushEvent("ms_vaultsocketed_changed")
existing_socket:PushEvent("ms_vaultsocketed_changed")
assert(after_native == 2, "repeat observations and other listeners must be preserved")

local spawned = trial()
assert(TheWorld.event_listeners == nil or TheWorld.event_listeners.entity_spawned == nil)
spawned:OnLoadPostPass({}, {})
local tracker = spawned.components.entitytracker
local activator = tracker:GetEntity("activator1")
activator.spark = true
activator:PushEvent("ms_vaultactivator_changed")
assert(count(progress_event) == (enabled and 4 or 0))
local first, last = tracker:GetEntity("guard1"), tracker:GetEntity("guard2")
first:PushEvent("death")
assert(first._vault_death_triggered and not first._vault_death_loot and count(bonus_event) == 0)
tracker:TrackEntity("pillar1", entity("vault_pillar_guard_dormant"))
last:PushEvent("death")
assert(last._vault_death_triggered and not last._vault_death_loot and count(bonus_event) == 0)
tracker:ForgetEntity("pillar1")
last:PushEvent("death")
assert(last._vault_death_loot and count(bonus_event) == (enabled and 1 or 0))
if enabled then
    local progress = records(progress_event)
    assert(progress[1].trigger == "socket" and progress[1].sockets == 1 and progress[1].sparks == 0)
    assert(progress[2].trigger == "socket" and progress[2].sockets == 1)
    assert(progress[3].trigger == "loaded" and progress[3].sockets == 0)
    assert(progress[4].trigger == "activator" and progress[4].sparks == 1)
    assert(records(bonus_event)[1].last_guard.guid == last.GUID)
end

-- Telemetry failures cannot interrupt the native methods, and native errors propagate unchanged.
local values = require("dst_server.values")
local entity_ref = values.entity_ref
values.entity_ref = function() error("capture failed") end
assert(delivery:SendToPoint(destination, player) and delivery.t == 0)
delivery:Stop()
tracker:GetEntity("socket1"):PushEvent("ms_vaultsocketed_changed")
values.entity_ref = entity_ref
local failure = {}
delivery:SetOnStartDeliveryFn(function() error(failure, 0) end)
local before = #outputs
local result, err = pcall(delivery.SendToPoint, delivery, destination, player)
assert(not result and err == failure and #outputs == before)
assert(state.errors == (enabled and 2 or 0), tostring(state.errors))
for _, line in ipairs(outputs) do io.write(line, "\n") end
