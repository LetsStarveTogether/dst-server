local T = require("tests.client_mod.support")
local Fixtures = require("tests.client_mod.support.inventory")
local Inventory = require("fieldkit/game/inventory")
local Storage = require("fieldkit/features/storage")
local Executor = require("fieldkit/core/executor")
local native_env = { require = function() return {} end, Class = T.Class, math = setmetatable({}, { __index = math }) }
T.native("mathutil", native_env)
local NativeInventory = T.native("components/inventory", native_env)

local function fixture()
    local state = Fixtures.new()
    local player = T.entity({ GUID = 100 })
    local inv = state:inventory(player, 4)
    local function owner(id)
        local value = state:container(T.entity({ GUID = id }), 4, { type = "chest" })
        local move = value.MoveItemFromCountOfSlot
        function value:MoveItemFromCountOfSlot(slot, destination, count)
            if destination ~= player or not state.native_routes then return move(self, slot, destination, count) end
            state:request(function()
                local source = self.slots[slot]
                local receiver = {
                    itemslots = inv.slots, equipslots = inv.equips, maxslots = inv.size,
                    GetOverflowContainer = function() return inv.overflow ~= self and inv.overflow or nil end,
                    GetSpecializedContainers = function()
                        local pockets = {}
                        for _, item in pairs(inv.slots) do
                            local pocket = item.replica.container
                            if pocket and pocket.priorityfn then
                                if pocket == self then return end
                                if pocket.open then pockets[#pockets + 1] = pocket end
                            end
                        end
                        return pockets
                    end,
                    CanTakeItemInSlot = function(_, item, slot) return inv:CanTakeItemInSlot(item, slot) end,
                }
                local index, selected = NativeInventory.GetNextAvailableSlot(receiver, source)
                assert(index, "native routing must accept the planned move")
                state.selected = selected
                local slots = selected.slots or selected
                if slots[index] then slots[index].count = slots[index].count + count
                else slots[index] = state:item(source.prefab, count, { skinname = source.skinname }) end
                source.count = source.count - count
                if source.count == 0 then self.slots[slot] = nil end
            end, { self, inv })
        end
        return value
    end
    state.owner = owner
    local chest = owner(200)
    state.open = { [chest.inst] = true }
    function inv:GetOpenContainers() return state.open end
    local ctx = { player = player, config = {}, G = { RPC = { TakeActiveItemFromCountOfSlot = 42 },
        GetTime = function() return state.now end } }
    function ctx:ready(mode) assert(mode == "inventory"); return true end
    function ctx:text(_, en) return en end
    function ctx:notify(value) state.messages[#state.messages + 1] = value end
    ctx.G.SendRPCToServer = function(rpc, slot, container, count)
        assert(rpc == 42 and container == nil and type(count) == "number", "native take RPC keeps nil container position")
        inv:TakeActiveItemFromCountOfSlot(slot, count)
    end
    ctx.executor = Executor.new(function() return state.now end, function() return true end,
        function(value) ctx:notify(value) end, error)
    ctx.storage = Storage.new(ctx)
    function state:tick() self.now = self.now + .1; ctx.executor:tick() end
    function state:finish()
        for _ = 1, 100 do
            self:tick()
            if not ctx.executor.current then return end
            self:deliver()
        end
        error("inventory operation did not terminate")
    end
    return ctx, state, inv, chest
end

local function raw_takes(ctx, inv)
    local requests = {}
    ctx.G.SendRPCToServer = function(rpc, slot, container, count)
        assert(rpc == ctx.G.RPC.TakeActiveItemFromCountOfSlot and container == nil)
        requests[#requests + 1] = function() NativeInventory.TakeActiveItemFromCountOfSlot(inv, slot, count) end
    end
    return requests
end

T.each("native cursor destination", { "inventory", "container" }, function(kind)
    T.each("put and add", {
        { name = "put one rejects singleton", method = "PutOne", active = 1, left = 1, stored = 0 },
        { name = "put one splits stack", method = "PutOne", active = 3, left = 2, stored = 1 },
        { name = "put all singleton", method = "PutAll", active = 1, left = 0, stored = 1 },
        { name = "put all stack", method = "PutAll", active = 3, left = 0, stored = 3 },
        { name = "put one occupied", method = "PutOne", active = 3, existing = 7, left = 3, stored = 7 },
        { name = "put all occupied matching stack", method = "PutAll", active = 3, existing = 7, left = 3, stored = 7 },
        { name = "add one rejects singleton", method = "AddOne", active = 1, existing = 7, left = 1, stored = 7 },
        { name = "add one splits stack", method = "AddOne", active = 3, existing = 7, left = 2, stored = 8 },
        { name = "add all singleton", method = "AddAll", active = 1, existing = 7, left = 0, stored = 8 },
        { name = "add all stack", method = "AddAll", active = 3, existing = 7, left = 0, stored = 10 },
        { name = "add all capacity remainder", method = "AddAll", active = 3, existing = 19, left = 2, stored = 20 },
        { name = "add one full", method = "AddOne", active = 3, existing = 20, left = 3, stored = 20 },
        { name = "add all full", method = "AddAll", active = 3, existing = 20, left = 3, stored = 20 },
        { name = "add one empty", method = "AddOne", active = 3, left = 3, stored = 0 },
        { name = "add all empty", method = "AddAll", active = 3, left = 3, stored = 0 },
        { name = "add one incompatible", method = "AddOne", active = 3, existing = 7, prefab = "stone", left = 3, stored = 7 },
        { name = "add all incompatible", method = "AddAll", active = 3, existing = 7, prefab = "stone", left = 3, stored = 7 },
        { name = "add all different skin", method = "AddAll", active = 3, existing = 7, skin = "winter", left = 3, stored = 7 },
        { name = "add one receiver rejects pairing", method = "AddOne", active = 3, existing = 7, callback = "receiver", left = 3, stored = 7 },
        { name = "add all receiver rejects pairing", method = "AddAll", active = 3, existing = 7, callback = "receiver", left = 3, stored = 7 },
        { name = "add one consults receiver only", method = "AddOne", active = 3, existing = 7, callback = "active", left = 2, stored = 8 },
        { name = "add all consults receiver only", method = "AddAll", active = 3, existing = 7, callback = "active", left = 0, stored = 10 },
        { name = "put one without stacking", method = "PutOne", active = 3, stacks = false, left = 2, stored = 1 },
        { name = "put all singleton without stacking", method = "PutAll", active = 1, stacks = false, left = 0, stored = 1 },
        { name = "put all stack without stacking", method = "PutAll", active = 3, stacks = false, left = 3, stored = 0 },
        { name = "add one without stacking", method = "AddOne", active = 3, existing = 7, stacks = false, left = 3, stored = 7 },
        { name = "add all without stacking", method = "AddAll", active = 3, existing = 7, stacks = false, left = 3, stored = 7 },
        { name = "put one rejected", method = "PutOne", active = 3, reject = true, left = 3, stored = 0 },
        { name = "put all rejected", method = "PutAll", active = 3, reject = true, left = 3, stored = 0 },
        { name = "add one rejected", method = "AddOne", active = 3, existing = 7, reject = true, left = 3, stored = 7 },
        { name = "add all rejected", method = "AddAll", active = 3, existing = 7, reject = true, left = 3, stored = 7 },
    }, function(row)
        local _, s, inv, chest = fixture()
        local owner = kind == "inventory" and inv or chest
        owner.stacks, owner.reject = row.stacks ~= false, row.reject
        inv.active = s:item("log", row.active)
        owner.slots[1] = row.existing and s:item(row.prefab or "log", row.existing, { skinname = row.skin }) or nil
        local active, existing = inv.active, owner.slots[1]
        if row.callback then
            local blocked = row.callback == "receiver" and existing or active
            local other = blocked == existing and active or existing
            blocked.stackable_CanStackWithFn = function(receiver, source)
                assert(receiver == blocked and source == other, "native pairing receives the owner and incoming entity")
                return false
            end
            assert(not Fixtures.stackable(blocked, other) and Fixtures.stackable(other, blocked),
                "the shared stack check follows the native receiving direction")
        end
        local method = row.method .. (row.method:sub(1, 3) == "Put" and "OfActiveItemInSlot" or "OfActiveItemToSlot")
        owner[method](owner, 1)
        assert(inv.active == active and active.count == row.active and owner.slots[1] == existing
            and (not existing or existing.count == row.existing), "requests cannot mutate slots before delivery")
        s:deliver(true)
        assert(inv.busy and owner.busy and s.calls == 1, "delivery and busy release are separate")
        assert((inv.active and inv.active.count or 0) == row.left and (owner.slots[1] and owner.slots[1].count or 0) == row.stored)
        assert(row.left + row.stored == row.active + (row.existing or 0), "native transfers conserve quantity")
        if row.left == row.active then
            assert(inv.active == active and owner.slots[1] == existing and not active.removed
                and (not existing or not existing.removed), "rejected native calls preserve both entities")
        end
        s:deliver()
        assert(not inv.busy and not owner.busy)
    end)
    T.case("occupied incompatible put all", function()
        local _, s, inv, chest = fixture()
        local owner = kind == "inventory" and inv or chest
        local active, existing = s:item("log", 3), s:item("stone", 7)
        inv.active, owner.slots[1] = active, existing
        owner:PutAllOfActiveItemInSlot(1); s:deliver()
        assert(inv.active == (kind == "container" and existing or active)
            and owner.slots[1] == (kind == "container" and active or existing),
            "native containers swap incompatible occupied slots; inventory PutAll requires an empty slot")
    end)
    T.each("take", {
        { name = "singleton", source = 1, count = 1, taken = 1 },
        { name = "partial stack", source = 8, count = 3, taken = 3 },
        { name = "whole stack", source = 8, count = 8, taken = 8 },
        { name = "clamp oversized request", source = 8, count = 99, taken = 8 },
        { name = "clamp zero request", source = 8, count = 0, taken = 1 },
        { name = "empty source", count = 1, taken = 0 },
        { name = "occupied cursor", source = 8, count = 3, active = 2, taken = 0 },
        { name = "locked whole stack", source = 8, count = 8, locked = true, taken = 0 },
        { name = "locked partial stack", source = 8, count = 3, locked = true, taken = 3 },
        { name = "overstack limit", source = 40, count = 99, originalmaxsize = 20, taken = 20 },
    }, function(row)
        local _, s, inv, chest = fixture()
        local owner = kind == "inventory" and inv or chest
        local item = row.source and s:item("log", row.source, { locked = row.locked,
            maxsize = row.originalmaxsize and math.huge or 20, originalmaxsize = row.originalmaxsize }) or nil
        inv.active, owner.slots[1] = row.active and s:item("stone", row.active) or nil, item
        local active = inv.active
        owner:TakeActiveItemFromCountOfSlot(1, row.count)
        assert(owner.slots[1] == item and inv.active == active and (not item or item.count == row.source))
        s:deliver()
        assert((inv.active and inv.active.count or 0) == (row.active or 0) + row.taken
            and (owner.slots[1] and owner.slots[1].count or 0) == (row.source or 0) - row.taken)
        if row.taken == 0 then
            assert(inv.active == active and owner.slots[1] == item, "rejected take preserves both entities")
        elseif row.taken == row.source then
            assert(inv.active == item, "taking the whole stack preserves its entity")
        else
            assert(owner.slots[1] == item and inv.active ~= item and inv.active.prevslot == 1
                and inv.active.prevcontainer == (kind == "container" and chest or nil), "split stacks retain their native source")
            assert(inv.active.replica.stackable:MaxSize() == 20, "splitting an overstack restores normal cursor capacity")
        end
    end)
end)

T.each("readonly container", { "PutOneOfActiveItemInSlot", "PutAllOfActiveItemInSlot", "AddOneOfActiveItemToSlot",
    "AddAllOfActiveItemToSlot", "TakeActiveItemFromCountOfSlot" }, function(method)
    local _, s, inv, chest = fixture()
    local taking, putting = method:sub(1, 4) == "Take", method:sub(1, 3) == "Put"
    inv.active, chest.slots[1] = not taking and s:item("log", 3) or nil, not putting and s:item("log", 7) or nil
    local active, item = inv.active, chest.slots[1]
    chest.readonly = true
    chest[method](chest, 1, 1); s:deliver()
    assert(inv.active == active and chest.slots[1] == item and (not active or active.count == 3)
        and (not item or item.count == 7), "readonly native calls cannot mutate either side")
end)

T.case("two-sided take and put receipts", function()
    local ctx, s, inv = fixture()
    inv.slots[1] = s:item("log", 8)
    local receipt = assert(Inventory.take(ctx, inv, 1, 1))
    assert(receipt:state() == "pending" and s.calls == 1)
    s:deliver(true)
    assert(receipt:state() == "pending", "delivered counts need the native busy release")
    s:deliver()
    inv.active.preview = 1
    assert(receipt:state() == "pending", "predicted cursor size is not receipt")
    inv.active.preview = nil
    assert(receipt:state() == "done" and inv.active.count == 1 and inv.slots[1].count == 7)
    inv.slots[1].preview = 6
    assert(Inventory.slot(ctx, inv, 1) == nil, "preview must not become an empty slot")
    inv.slots[1].preview = nil
    local put = assert(Inventory.put(ctx, inv, 1, 1))
    s:deliver(true)
    assert(put:state() == "pending", "a delivered merge still waits for the native busy release")
    s:deliver()
    assert(put:state() == "done" and inv.active == nil and inv.slots[1].count == 8,
        "last cursor item uses AddAll, not AddOne which requires stack > 1")
end)
T.case("host exact take", function()
    local ctx, s, inv = fixture()
    inv.slots[1] = s:item("log", 8)
    local host_count
    ctx.player.components.inventory = { TakeActiveItemFromCountOfSlot = function(_, slot, count) host_count = count; inv:TakeActiveItemFromCountOfSlot(slot, count) end }
    assert(Inventory.take(ctx, inv, 1, 3)); assert(host_count == 3, "host count reaches native component directly")
end)
T.case("native remote validator", function()
    local ctx, s, inv = fixture()
    local handlers
    local env = { require = function() return {} end, orderedPairs = function(values)
        if not handlers and values.TakeActiveItemFromCountOfSlot then handlers = values end
        return pairs(values)
    end }
    T.native("networkclientrpc", env)
    local server = { components = { inventory = { TakeActiveItemFromCountOfSlot = function(_, slot, count)
        assert(slot == 1 and count == 3)
        s.server_received = true
    end } } }
    env.TheNet = { SendRPCToServer = function(_, code, ...)
        handlers[code](server, ...)
    end }
    ctx.G.RPC, ctx.G.SendRPCToServer = env.RPC, env.SendRPCToServer
    inv.slots[1] = s:item("log", 8)
    assert(Inventory.take(ctx, inv, 1, 3) and s.server_received,
        "our remote take reaches the current native RPC validator with the correct argument positions")
end)
T.case("incremental wheel queue", function()
    local ctx, s, inv = fixture()
    inv.slots[1] = s:item("log", 8)
    assert(ctx.storage:scroll(inv, 1, 1))
    s:tick()
    assert(ctx.storage:scroll(inv, 1, 1), "same-slot wheel events queue while native prediction is busy")
    assert(not ctx.storage:scroll(inv, 2, 1), "unrelated busy input is explicitly rejected")
    s:finish()
    assert(inv.active.count == 2 and inv.slots[1].count == 6 and s.calls == 3, "incremental cursor take uses merge then exact native take")
    assert(ctx.storage:scroll(inv, 1, -1, 1)); s:finish()
    assert(inv.active.count == 1 and inv.slots[1].count == 7)
    assert(ctx.storage:scroll(inv, 2, -1)); s:finish()
    assert(inv.active == nil and inv.slots[2].count == 1)
end)
T.each("wheel receiving capacity", {
    { name = "ordinary partial stack", stored = 19, active = 3, count = 3, after = 20, left = 2 },
    { name = "ordinary full stack", stored = 20, active = 1, count = 1, after = 20, left = 1, rejected = true },
    { name = "larger receiving stack", stored = 25, maximum = 40, active = 3, count = 3, after = 28, left = 0 },
    { name = "infinite singleton", stored = 20, maximum = math.huge, active = 1, count = 1, after = 21, left = 0 },
    { name = "infinite partial cursor", stored = 40, maximum = math.huge, active = 5, count = 3, after = 43, left = 2 },
    { name = "infinite whole cursor", stored = 40, maximum = math.huge, active = 5, count = 20, after = 45, left = 0 },
    { name = "infinite withdrawal", stored = 40, maximum = math.huge, active = 19, count = 20,
        direction = 1, after = 39, left = 20 },
    { name = "full cursor withdrawal", stored = 40, maximum = math.huge, active = 20, count = 1,
        direction = 1, after = 40, left = 20, rejected = true },
}, function(row)
    local ctx, s, inv, chest = fixture()
    chest.slots[1] = s:item("log", row.stored, { maxsize = row.maximum or 20, originalmaxsize = 20 })
    inv.active = s:item("log", row.active)
    local ok = ctx.storage:scroll(chest, 1, row.direction or -1, row.count)
    assert(ok == not row.rejected, "wheel admission must use the receiving stack's capacity")
    if ok then
        s:tick()
        assert(chest.slots[1].count == row.stored and inv.active.count == row.active,
            "planning and dispatch cannot acknowledge undelivered quantities")
        s:finish()
    end
    assert(chest.slots[1].count == row.after and (inv.active and inv.active.count or 0) == row.left)
    assert(row.stored + row.active == row.after + row.left and #s.messages == 0)
    if row.rejected then assert(s.calls == 0 and not ctx.executor.current) end
end)
T.case("full source cursor swap", function()
    local ctx, s, inv = fixture()
    inv.slots[1], inv.active = s:item("log", 20), s:item("log", 5)
    assert(ctx.storage:scroll(inv, 1, 1)); s:finish()
    assert(inv.active.count == 6 and inv.slots[1].count == 19 and inv.slots[2] == nil and s.calls == 4,
        "full source uses temporary slot, never drops or illegally swaps stackable items")
    for slot = 2, 4 do inv.slots[slot] = s:item("stone", 20) end
    assert(not ctx.storage:scroll(inv, 1, 1) and s.calls == 4, "no safe temporary slot means no mutation")
end)
T.case("bidirectional transfer and source guards", function()
    local ctx, s, inv, chest = fixture()
    inv.slots[1] = s:item("log", 8)
    assert(ctx.storage:transfer(inv, 1, 3)); s:finish()
    assert(inv.slots[1].count == 5 and chest.slots[1].count == 3)
    assert(ctx.storage:transfer(chest, 1)); s:finish()
    assert(inv.slots[1].count == 8 and chest.slots[1] == nil)
    inv.slots[1].locked = true
    assert(not ctx.storage:transfer(inv, 1) and not ctx.storage:scroll(inv, 1, 1), "locked slots are never automatic sources")
    inv.slots[1].locked, chest.readonly = false, true
    assert(not ctx.storage:transfer(inv, 1), "read-only containers are never destinations")
end)
T.case("collect matching stacks", function()
    local ctx, s, inv = fixture()
    inv.slots[1], inv.slots[2], inv.slots[3] = s:item("log", 5), s:item("log", 6), s:item("stone", 7)
    assert(ctx.storage:collect(inv, 1)); s:finish()
    assert(inv.active.count == 11 and inv.slots[1] == nil and inv.slots[2] == nil and inv.slots[3].count == 7)
end)
T.case("cursor capacity and personal storage", function()
    local ctx, s, inv, chest = fixture()
    inv.active, inv.slots[2] = s:item("log", 12), s:item("log", 17)
    inv.overflow = s.owner(300)
    inv.overflow.slots[1] = s:item("log", 15)
    inv.overflow.slots[1].locked = true
    assert(ctx.storage:return_active()); s:finish()
    assert(inv.slots[2].count == 20 and inv.overflow.slots[1].count == 20 and inv.slots[1].count == 4
        and inv.active == nil and s.calls == 3 and chest.slots[1] == nil,
        "return merges both personal stores before using an empty slot, including locked receiving stacks")
    for slot = 1, inv:GetNumSlots() do inv.slots[slot] = s:item("stone", 20) end
    for slot = 1, inv.overflow:GetNumSlots() do inv.overflow.slots[slot] = s:item("stone", 20) end
    inv.active = s:item("log", 10)
    assert(not ctx.storage:return_active() and inv.active.count == 10 and s.calls == 3,
        "full personal storage never falls back to a ground drop or an open chest")
    inv.slots[2] = s:item("log", 17)
    assert(ctx.storage:return_active()); s:finish()
    assert(inv.slots[2].count == 20 and inv.active.count == 7 and s.calls == 4
        and s.messages[1]:find("Remaining items"), "partial capacity leaves the remainder on the cursor")
end)
T.case("occupied return destination", function()
    local ctx, s, inv = fixture()
    inv.active, inv.slots[1] = s:item("log", 9), s:item("log", 18)
    assert(ctx.storage:return_active()); s:tick(); s:deliver()
    inv.slots[2] = s:item("stone", 20)
    s:tick()
    assert(not ctx.executor.current and inv.active.count == 7 and inv.slots[2].prefab == "stone",
        "a newly occupied destination cancels without swapping or dropping the cursor item")
end)
T.case("native specialized receiver", function()
    local ctx, s, inv, chest = fixture()
    local pocket = s.owner(300)
    pocket.priorityfn = function(_, value) return value.prefab == "slingshotammo_rock" end
    pocket.inst.prefab = "slingshotammo_container"
    inv.slots[1], inv.size = pocket.inst, 1
    pocket.slots[1] = s:item("slingshotammo_rock", 15)
    pocket.slots[1].locked = true
    chest.slots[1] = s:item("slingshotammo_rock", 3)
    s.native_routes = true
    assert(ctx.storage:transfer(chest, 1), "full main inventory still accepts ammo into an open pocket")
    s:finish()
    assert(s.selected == pocket and pocket.slots[1].count == 18 and chest.slots[1] == nil and #s.messages == 0,
        "latest native priority route and receipt agree, including a locked receiving stack")
    pocket.open, chest.slots[1] = false, s:item("slingshotammo_rock", 1)
    assert(not ctx.storage:transfer(chest, 1) and s.calls == 1, "native manual moves exclude closed pockets")
    pocket.open, pocket.busy = true, true
    assert(not ctx.storage:transfer(chest, 1) and s.calls == 1, "unsynchronized receiving pocket is not usable capacity")
    pocket.busy = false
    local receipt = assert(Inventory.move(ctx, assert(Inventory.slot(ctx, chest, 1)), ctx.player, 1))
    s:deliver()
    pocket.slots[1].preview = 19
    assert(receipt:state() == "pending", "pocket prediction does not acknowledge the move")
    pocket.slots[1].preview = nil
    assert(receipt:state() == "done", "stable receiving pocket acknowledges source consumption")
end)
T.case("native equipped stack receiver", function()
    local ctx, s, inv, chest = fixture()
    inv.size, inv.slots[1] = 1, s:item("stone", 20)
    inv.equips.hands = s:item("blowdart_pipe", 15)
    inv.equips.hands.components.equippable = { equipstack = true }
    chest.slots[1] = s:item("blowdart_pipe", 4)
    s.native_routes = true
    assert(ctx.storage:transfer(chest, 1)); s:finish()
    assert(s.selected == inv.equips and inv.equips.hands.count == 19 and #s.messages == 0,
        "latest native equipped-stack route works with no ordinary inventory capacity")
end)
T.case("native pocket and backpack exclusions", function()
    local ctx, s, inv = fixture()
    local source, other = s.owner(300), s.owner(301)
    source.priorityfn, other.priorityfn = function() return true end, function() return true end
    inv.slots[1], inv.slots[2], inv.size = source.inst, other.inst, 2
    source.slots[1] = s:item("log", 3)
    assert(not ctx.storage:transfer(source, 1), "moving out of a pocket disables all sibling pockets, as native does")
    inv.size, s.native_routes = 3, true
    assert(ctx.storage:transfer(source, 1)); s:finish()
    assert(s.selected == inv.slots and inv.slots[3].count == 3 and source.slots[1] == nil and #s.messages == 0)
    inv.overflow, inv.size = s.owner(400), 2
    inv.overflow.slots[1] = s:item("stone", 2)
    source.priorityfn, other.priorityfn = nil, nil
    s.open = {}
    assert(not ctx.storage:transfer(inv.overflow, 1), "the source backpack never counts as receiving space")
end)
T.case("timeout and source replacement", function()
    local ctx, s, inv = fixture()
    inv.slots[1] = s:item("log", 8)
    assert(ctx.storage:scroll(inv, 1, 1)); s:tick()
    s.pending = nil; s:deliver()
    s.now = 7; ctx.executor:tick()
    assert(not ctx.executor.current and s.calls == 1 and #s.messages == 1, "unacknowledged operations time out without replay")
    assert(ctx.storage:scroll(inv, 1, 1)); s:tick(); s:deliver()
    inv.slots[1] = s:item("stone", 7)
    s:tick()
    assert(not ctx.executor.current and inv.active.count == 1 and #s.messages == 2, "manual inventory changes stop the sequence")
end)
T.each("cancelled raw take", {
    { name = "same slot and quantity", slot = 1, count = 1, prefab = "log" },
    { name = "same slot and larger quantity", slot = 1, count = 2, prefab = "log" },
    { name = "different slot and same prefab", slot = 2, count = 1, prefab = "log" },
    { name = "different slot and prefab", slot = 2, count = 1, prefab = "cutgrass" },
}, function(row)
    local ctx, s, inv, chest = fixture()
    local requests = raw_takes(ctx, inv)
    inv.slots[1], inv.slots[2] = s:item("log", 8), s:item(row.prefab, 8)
    assert(ctx.storage:scroll(inv, 1, 1)); s:tick()
    assert(#requests == 1 and not inv.classified:IsBusy(), "a bare RPC has no native prediction or busy marker")
    ctx.executor:cancel("manual")
    assert(not ctx.storage:scroll(inv, row.slot, 1, row.count), "an unsettled old RPC cannot admit a new inventory intent")
    assert(Inventory.busy(ctx, inv) and Inventory.busy(ctx, chest), "other inventory features see the same outstanding request")
    assert(not Inventory.take(ctx, inv, row.slot, row.count) and #requests == 1)
    requests[1]()
    local received = inv.active
    inv.active = nil
    assert(Inventory.busy(ctx, inv), "source consumption alone does not settle a cancelled take")
    inv.active, inv.slots[1].preview = received, 7
    assert(Inventory.busy(ctx, inv), "predicted counts do not settle a cancelled take")
    inv.slots[1].preview = nil
    assert(not Inventory.busy(ctx, inv) and not ctx.executor.current and #requests == 1,
        "the late receipt only releases the gate; it cannot resume the cancelled job")
    assert(ctx.storage:return_active()); s:finish()
    assert(ctx.storage:scroll(inv, row.slot, 1, row.count)); s:tick()
    assert(#requests == 2 and ctx.executor.current, "a fresh intent sends its own request")
    requests[2](); s:tick()
    assert(not ctx.executor.current and inv.active.prefab == row.prefab and inv.active.count == row.count
        and inv.slots[row.slot].count == 8 - row.count and #s.messages == 0)
end)
T.case("cancelled raw take survives context replacement and expires", function()
    local ctx, s, inv = fixture()
    local requests = raw_takes(ctx, inv)
    inv.slots[1] = s:item("log", 8)
    assert(ctx.storage:scroll(inv, 1, 1)); s:tick()
    ctx.executor:cancel("focus")
    local replacement = { player = ctx.player, G = ctx.G, config = ctx.config,
        text = ctx.text, ready = ctx.ready, executor = ctx.executor }
    replacement.storage = Storage.new(replacement)
    assert(not replacement.storage:scroll(inv, 1, 1), "recreating the HUD cannot discard an in-flight take")
    s.now = 6
    assert(Inventory.busy(replacement, inv), "the timeout starts at dispatch, not job cancellation")
    s.now = 6.2
    assert(not Inventory.busy(replacement, inv) and #requests == 1, "an unacknowledged request expires without replay")
    assert(replacement.storage:scroll(inv, 1, 1)); s:tick()
    assert(#requests == 2)
    ctx.executor:cancel("manual")
    inv.slots[1] = s:item("cutgrass", 8)
    assert(not Inventory.busy(replacement, inv), "a changed source releases an abandoned request")
end)
T.case("explicit salt replacement receipt", function()
    local ctx, s, inv, chest = fixture()
    chest.slots[1] = s:item("meat_dried", 1)
    local plain = assert(Inventory.move(ctx, Inventory.slot(ctx, chest, 1), ctx.player, 1))
    s:deliver()
    chest.slots[1] = s:item("saltrock", 1)
    assert(plain:state() == "changed", "ordinary inventory transfers reject all source replacements")
    chest.slots[1] = s:item("meat_dried", 1)
    local salted = assert(Inventory.move(ctx, Inventory.slot(ctx, chest, 1), ctx.player, 1, { source_replacement = "saltrock" }))
    s:deliver()
    chest.slots[1] = s:item("saltrock", 1)
    inv.slots[1].count = 1
    assert(salted:state() == "pending", "an expected salt replacement does not acknowledge a missing destination increase")
    inv.slots[1].count = 2
    assert(salted:state() == "done", "the explicit replacement and received original product acknowledge both ends")
    chest.slots[1].count = 2
    assert(salted:state() == "changed", "expected source replacement must contain exactly one item")
    chest.slots[1] = s:item("spoiled_food", 1)
    assert(salted:state() == "changed", "only the named replacement prefab is accepted")
    chest.slots[1] = s:item("meat_dried", 2)
    assert(not Inventory.move(ctx, Inventory.slot(ctx, chest, 1), ctx.player, 1, { source_replacement = "saltrock" }),
        "partial-stack transfers cannot opt into replacement")
end)
T.done()
