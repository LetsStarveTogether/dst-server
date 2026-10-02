local root, scripts, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
require("class")
require("vector3")
require("util")
require("mathutil")
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
        RemoveTag = noop,
        HasTag = function(_, tag) return tag == "player" and userid ~= nil end,
        Transform = {GetWorldPosition = function() return 1, 0, 2 end, SetPosition = noop},
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

-- Run native container UI handlers and stack splitting/merging without the engine's UI registry.
package.loaded.containers = {}
local Container = require("components/container")
local Inventory = require("components/inventory")
local Stackable = require("components/stackable")
TUNING.STACK_SIZE_MEDITEM = 40
MAXUINT = 4294967295
BRANCH = "release"
GetGameModeProperty = function() return false end
local replica = setmetatable({}, {__index = function() return noop end})
local function item(prefab, size)
    local inst = entity(prefab)
    inst.replica = {stackable = replica}
    inst.components.inventoryitem = {
        cangoincontainer = true,
        OnPutInInventory = function(self, owner) self.owner = owner end,
        OnRemoved = function(self) self.owner = nil end,
        OnDropped = noop, InheritMoisture = noop, SetTemperature = noop,
        DiluteMoisture = noop, DiluteTemperature = noop,
        GetMoisture = function() return 0 end,
        IsWet = function() return false end,
        GetTemperature = function() return 20 end,
    }
    inst.components.stackable = Stackable(inst)
    inst.components.stackable:SetStackSize(size or 1)
    inst.Remove = function(self) self.removed = true end
    return inst
end
SpawnPrefab = item
local function chest(size)
    local inst = entity("treasurechest")
    inst.replica = {container = replica}
    inst.GetPosition = function() return Vector3(1, 0, 2) end
    local component = Container(inst)
    inst.components.container = component
    component:SetNumSlots(size or 4)
    component.openlist[player] = true
    return component
end
-- The engine-facing inventory ownership methods are irrelevant to the UI transfer decision.
local inventory = setmetatable({inst = player, itemslots = {}, equipslots = {}, isopen = true, isvisible = true},
    {__index = Inventory})
player.components.inventory = inventory
inventory.GiveActiveItem = function(self, value) self.activeitem = value end
inventory.SetActiveItem = inventory.GiveActiveItem
inventory.RemoveItemBySlot = function(self, slot)
    local value = self.itemslots[slot]
    self.itemslots[slot] = nil
    return value
end
inventory.RemoveItem = function(self, value, whole)
    if not whole and value.components.stackable:IsStack() then return value.components.stackable:Get(1) end
    if self.activeitem == value then self.activeitem = nil end
    return value
end
inventory.CanTakeItemInSlot = function() return true end
inventory.GiveItem = function(self, value, slot)
    slot = slot or 1
    if self.itemslots[slot] ~= nil then return false end
    self.itemslots[slot] = value
    return true
end
local history = profile == "history"
if history then require("dst_server.container_events").install() end
local transfer_event = "dst.player.container_transfer"
local function transfer(callback, holder, target, before_size, after_size)
    local before_count = count(transfer_event)
    callback()
    assert(count(transfer_event) == before_count + (history and 1 or 0), "one record per changed slot")
    if history then
        local rows = records(transfer_event)
        local row = rows[#rows]
        assert(row.player.userid == player.userid and row.holder.guid == holder.inst.GUID)
        assert(row.target.guid == target.GUID and row.slot == 1)
        assert((row.before and row.before.stack_size or 0) == before_size)
        assert((row.after and row.after.stack_size or 0) == after_size)
    end
end
local box = chest()
inventory.activeitem = item("log", 8)
transfer(function() box:PutOneOfActiveItemInSlot(1, player) end, box, player, 0, 1)
transfer(function() box:AddOneOfActiveItemToSlot(1, player) end, box, player, 1, 2)
transfer(function() box:AddAllOfActiveItemToSlot(1, player) end, box, player, 2, 8)
assert(inventory.activeitem == nil)
transfer(function() box:TakeActiveItemFromHalfOfSlot(1, player) end, box, player, 8, 4)
inventory.activeitem = nil
transfer(function() box:TakeActiveItemFromCountOfSlot(1, 2, player) end, box, player, 4, 2)
inventory.activeitem = nil
transfer(function() box:TakeActiveItemFromAllOfSlot(1, player) end, box, player, 2, 0)
-- Both native delegation directions must record once, including swaps of different items.
transfer(function() box:SwapActiveItemWithSlot(1, player) end, box, player, 0, 2)
inventory.activeitem = item("rocks", 3)
transfer(function() box:PutAllOfActiveItemInSlot(1, player) end, box, player, 2, 3)
transfer(function() box:SwapOneOfActiveItemWithSlot(1, player) end, box, player, 3, 1)

-- Moving between a player and containers bypasses BufferedAction entirely.
local destination = chest()
inventory.itemslots[1] = item("log", 10)
transfer(function() inventory:MoveItemFromHalfOfSlot(1, destination.inst) end, inventory, destination.inst, 10, 5)
transfer(function() inventory:MoveItemFromCountOfSlot(1, destination.inst, 2) end, inventory, destination.inst, 5, 3)
transfer(function() inventory:MoveItemFromAllOfSlot(1, destination.inst) end, inventory, destination.inst, 3, 0)
assert(destination.slots[1].components.stackable:StackSize() == 10)
box.slots = {}
transfer(function() destination:MoveItemFromHalfOfSlot(1, box.inst, player) end, destination, box.inst, 10, 5)
transfer(function() destination:MoveItemFromCountOfSlot(1, box.inst, 2, player) end, destination, box.inst, 5, 3)
transfer(function() destination:MoveItemFromAllOfSlot(1, box.inst, player) end, destination, box.inst, 3, 0)
inventory.itemslots = {}
transfer(function() box:MoveItemFromAllOfSlot(1, player, player) end, box, player, 10, 0)

destination = chest(1)
destination:GiveItem(item("log", 37), 1)
box:GiveItem(item("log", 10), 1)
transfer(function() box:MoveItemFromAllOfSlot(1, destination.inst, player) end, box, destination.inst, 10, 7)
assert(destination.slots[1].components.stackable:StackSize() == 40, "partial acceptance must retain the actual change")

local before_transfers = count(transfer_event)
box.slots = {}
box:GiveItem(item("log", 4), 1)
box:TakeActiveItemFromAllOfSlot(1, player) -- Occupied cursor: no change.
box:TakeActiveItemFromAllOfSlot(1, nil) -- No attributable player.
destination.numslots = 0
box:MoveItemFromAllOfSlot(1, destination.inst, player) -- Rejected, then restored to source.
assert(box.slots[1].components.stackable:StackSize() == 4)
box.slots[1].components.inventoryitem.islockedinslot = true
inventory.activeitem = nil
box:TakeActiveItemFromAllOfSlot(1, player)
assert(count(transfer_event) == before_transfers, "failed transfers and automatic GiveItem must be silent")
box.slots[1].components.inventoryitem.islockedinslot = false
local item_ref = values.item_ref
values.item_ref = function() error("snapshot failed") end
box:TakeActiveItemFromAllOfSlot(1, player)
values.item_ref = item_ref
assert(box.slots[1] == nil and inventory.activeitem ~= nil)
local native_error = {}
local get_active = inventory.GetActiveItem
inventory.GetActiveItem = function() error(native_error, 0) end
local succeeded, failure_value = pcall(box.PutAllOfActiveItemInSlot, box, 1, player)
inventory.GetActiveItem = get_active
assert(not succeeded and failure_value == native_error)
transfer(function() box:PutAllOfActiveItemInSlot(1, player) end, box, player, 0, 4)

-- Wormholes, temporary map exits and other local portals share the actual teleporter component.
local Teleporter = require("components/teleporter")
if enabled then capture.install_teleporters() end
local function portal(prefab, x)
    local inst = entity(prefab)
    inst.Transform.GetWorldPosition = function() return x, 0, 0 end
    inst.DoTaskInTime = function() return {Cancel = noop} end
    inst.components.teleporter = Teleporter(inst)
    inst.components.teleporter.offset = 0
    return inst
end
local entrance, exit, temporary = portal("wormhole", 0), portal("wormhole", 30), portal("tentacle_pillar_hole", 60)
local teleporter = entrance.components.teleporter
teleporter:Target(exit)
player.ScreenFade = noop
local position = 0
player.Transform.SetPosition = function(_, x) position = x end
TheWorld.Map = {IsOceanAtPoint = function() return false end}
local teleport_event = "dst.player.teleport_started"
assert(teleporter:Activate(player) and position == 30)
assert(teleporter:UseTemporaryExit(player, temporary) and position == 60)
assert(teleporter.targetTeleporterTemporary == nil)
teleporter:SetEnabled(false)
assert(not teleporter:Activate(player) and count(teleport_event) == (enabled and 2 or 0))
teleporter:SetEnabled(true)
assert(teleporter:Activate(drone))
assert(count(teleport_event) == (enabled and 2 or 0), "non-player teleports are not player history")
if enabled then
    local rows = records(teleport_event)
    assert(rows[1].player.userid == player.userid and rows[1].source.guid == entrance.GUID)
    assert(rows[1].target.guid == exit.GUID and rows[2].target.guid == temporary.GUID)
end
TheShard = {GetShardId = function() return "1" end}
Shard_IsWorldAvailable = function() return true end
teleporter:MigrationTarget("2", 5, 0, 6)
local migrations = 0
TheWorld:ListenForEvent("ms_playerdespawnandmigrate", function() migrations = migrations + 1 end)
assert(teleporter:Activate(player) and migrations == 1)
assert(count(teleport_event) == (enabled and 2 or 0), "cross-shard travel already has a migration event")
teleporter.migration_data = nil
teleporter.onActivate = function() error(native_error, 0) end
local before_error = #outputs
succeeded, failure_value = pcall(teleporter.Activate, teleporter, player)
assert(not succeeded and failure_value == native_error and #outputs == before_error)
teleporter.onActivate = nil
values.entity_ref = function() error("capture failed") end
assert(teleporter:Activate(player) and position == 30)
values.entity_ref = entity_ref
assert(state.errors == (enabled and (history and 4 or 3) or 0), tostring(state.errors))
for _, line in ipairs(outputs) do io.write(line, "\n") end
