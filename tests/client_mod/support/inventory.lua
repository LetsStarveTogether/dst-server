local T = require("tests.client_mod.support")
local M = {}
local env = { require = function() return {} end, Class = T.Class, math = setmetatable({}, { __index = math }) }
T.native("mathutil", env)
local NativeInventory = T.native("components/inventory", env)
local NativeContainer = T.native("components/container", env)
local NativeStackable = T.native("components/stackable", env)

function M.stackable(a, b)
    return a and b and a.replica.stackable and b.replica.stackable
        and a.replica.stackable:CanStackWith(b)
end

function M.new()
    local self = { now = 0, calls = 0, nextguid = 0, messages = {} }

    function self:item(prefab, count, fields)
        fields = fields or {}
        self.nextguid = self.nextguid + 1
        fields.GUID, fields.prefab, fields.count = fields.GUID or self.nextguid, prefab, count or 1
        local item = T.entity(fields)
        item.replica.inventoryitem = { IsLockedInSlot = function() return item.locked end }
        item.replica.stackable = {
            inst = item,
            StackSize = function() return item.count end,
            GetPreviewStackSize = function() return item.preview end,
            MaxSize = function() return item.maxsize or 20 end,
            OriginalMaxSize = function() return item.originalmaxsize or item.maxsize or 20 end,
            IsFull = function() return item.count >= (item.maxsize or 20) end,
            IsStack = function() return item.count > 1 end,
            IsOverStacked = function() return item.count > (item.originalmaxsize or item.maxsize or 20) end,
            CanStackWith = NativeStackable.CanStackWith,
            originalmaxsize = item.originalmaxsize,
        }
        local world = self
        function item.replica.stackable:Get(count)
            if item.count <= count then return item end
            item.count = item.count - count
            return world:item(item.prefab, count, { skinname = item.skinname, tags = item.tags,
                maxsize = item.originalmaxsize or item.maxsize })
        end
        function item.replica.stackable:Put(other)
            assert(M.stackable(item, other), "native merging requires compatible stacks")
            local moved = math.min(other.count, self:MaxSize() - item.count)
            item.count = item.count + moved
            if moved == other.count then other:Remove(); return nil end
            other.count = other.count - moved
            return other
        end
        item.components.stackable = item.replica.stackable
        item.components.inventoryitem = setmetatable({}, { __index = function(_, key)
            if key == "islockedinslot" then return item.locked end
        end })
        return item
    end

    function self:request(fn, owners)
        assert(not self.pending and not self.pending_owners, "only one native request may be in flight")
        self.calls, self.pending, self.pending_owners = self.calls + 1, fn, owners or {}
        for _, owner in ipairs(owners or {}) do owner.busy = true end
    end

    function self:deliver(keep_busy)
        local pending = self.pending
        self.pending = nil
        if pending then pending() end
        if not keep_busy then
            for _, owner in ipairs(self.pending_owners or {}) do owner.busy = false end
            self.pending_owners = nil
        end
    end

    function self:container(entity, size, fields)
        local box = fields or {}
        box.inst, box.size, box.slots = entity, size, box.slots or {}
        if box.open == nil then box.open = true end
        if box.stacks == nil then box.stacks = true end
        entity.replica.container, entity.components.container = box, box
        function box:GetNumSlots() return self.size end
        function box:GetItemInSlot(slot) return self.slots[slot] end
        function box:IsOpenedBy() return self.open end
        function box:IsReadOnlyContainer() return self.readonly end
        function box:IsBusy() return self.busy end
        function box:CanBeOpened() return not self.unavailable end
        function box:AcceptsStacks() return self.stacks end
        function box:CanTakeItemInSlot(item, slot)
            local existing = slot and self.slots[slot]
            return not self.reject and not self.readonly and not item.no_container
                and (not slot or slot >= 1 and slot <= self.size)
                and (not existing or not existing.locked or M.stackable(existing, item) and not existing.replica.stackable:IsFull())
                and (not self.accept or self:accept(item, slot))
        end
        function box:ShouldPrioritizeContainer(item) return self.priorityfn and self:priorityfn(item) or false end
        function box:IsEmpty() return next(self.slots) == nil end
        function box:GiveItem(item, slot)
            assert(not self.slots[slot], "native put requires an empty destination")
            self.slots[slot] = item
        end
        function box:RemoveItemBySlot(slot)
            local item = self.slots[slot]
            self.slots[slot] = nil
            return item
        end
        box.SwapActiveItemWithSlot = NativeContainer.SwapActiveItemWithSlot
        local world = self
        -- Run the game's slot handlers at delivery; the fixture only owns stacks and slots.
        for _, method in ipairs({ "TakeActiveItemFromCountOfSlot", "TakeActiveItemFromHalfOfSlot", "TakeActiveItemFromAllOfSlot",
            "PutAllOfActiveItemInSlot", "AddAllOfActiveItemToSlot", "PutOneOfActiveItemInSlot", "AddOneOfActiveItemToSlot" }) do
            box[method] = function(_, slot, count)
                world:request(function()
                    if world.reject then return end
                    box.readonlycontainer = box.readonly
                    local native = box == world.inv and NativeInventory or NativeContainer
                    local opener = { components = { inventory = world.inv } }
                    if method == "TakeActiveItemFromCountOfSlot" then native[method](box, slot, count, opener)
                    else native[method](box, slot, opener) end
                end, { box, world.inv })
            end
        end
        -- ponytail: flat transfers only; native receiver selection stays in the routing contracts.
        function box:MoveItemFromCountOfSlot(slot, destination, count)
            local target = destination.replica.inventory or destination.replica.container
            world:request(function()
                if world.reject then return end
                local item = self.slots[slot]
                local targets = { target }
                if target == world.inv and target.overflow and target.overflow ~= self then targets[#targets + 1] = target.overflow end
                for _, receiver in ipairs(targets) do
                    for index = 1, receiver.size do
                        local existing = receiver.slots[index]
                        if receiver:CanTakeItemInSlot(item, index) and (not existing or receiver.stacks
                            and M.stackable(existing, item) and existing.count + count <= (existing.maxsize or 20)) then
                            if existing then existing.count = existing.count + count
                            else receiver.slots[index] = world:item(item.prefab, count, { skinname = item.skinname, tags = item.tags }) end
                            item.count = item.count - count
                            if item.count == 0 then self.slots[slot] = nil end
                            if world.after_move then world.after_move(self, slot, item) end
                            return
                        end
                    end
                end
                error("planned native transfer must fit")
            end, { self, target })
        end
        return box
    end

    function self:inventory(player, size)
        local inv = self:container(player, size, { equips = {} })
        self.inv, player.replica.inventory, player.replica.container, player.components.container = inv, inv, nil, nil
        inv.classified = { IsBusy = function() return inv.busy end }
        function inv:GetActiveItem() return self.active end
        function inv:SetActiveItem(item) self.active = item end
        inv.GiveActiveItem = inv.SetActiveItem
        function inv:RemoveItem(item)
            assert(self.active == item, "native cursor put removes the active item")
            self.active = nil
            return item
        end
        function inv:GetOverflowContainer() return self.overflow end
        function inv:GetEquips() return self.equips end
        function inv:GetEquippedItem(slot) return self.equips[slot] end
        function inv:GetOpenContainers() return self.opencontainers or {} end
        return inv
    end
    return self
end

return M
