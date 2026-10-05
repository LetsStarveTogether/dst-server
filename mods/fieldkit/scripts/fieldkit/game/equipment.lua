local Actions = require("fieldkit/game/actions")
local Inventory = require("fieldkit/game/inventory")
local M = {}

local function same(a, b)
    return a == b or a ~= nil and b ~= nil and a.GUID ~= nil and a.GUID == b.GUID
end

function M.new(ctx)
    local self, player = {}, ctx.player
    local inv, hands = player.replica.inventory, ctx.G.EQUIPSLOTS.HANDS
    local function source(item)
        for _, entry in ipairs(Inventory.items(ctx, true)) do
            if same(entry.item, item) then return entry end
        end
    end
    local function stored(item)
        local entry = item and item:IsValid() and source(item)
        return entry and entry.slot and Inventory.readable(ctx, entry.owner)
    end
    local function locked(item)
        return item and (item.replica.inventoryitem:IsLockedInSlot()
            or item.replica.equippable and item.replica.equippable:ShouldPreventUnequipping())
    end
    local function stable(owner)
        return Inventory.readable(ctx, owner) and not Inventory.busy(ctx, owner)
    end
    local function stored_count(prefab, skin, containers)
        for _, owner in ipairs(containers) do
            if not stored(owner.inst) or not stable(owner) then return end
        end
        local count = 0
        for _, entry in ipairs(Inventory.items(ctx, true, containers)) do
            if entry.slot and entry.item.prefab == prefab and entry.item.skinname == skin then
                local size = Inventory.stack_size(entry.item)
                if not stable(entry.owner) or size == nil then return end
                count = count + size
            end
        end
        return count
    end
    function self:busy()
        local pending = inv._fieldkit_pending_equipment
        if pending then
            if pending:state() == "pending" then return true end
            inv._fieldkit_pending_equipment = nil
        end
        return false
    end
    function self:ready(job)
        return job ~= nil and (ctx.executor.current == job or ctx.executor.finishing == job) and not self:busy()
    end
    local function request(job, item, remove, allow_cursor)
        if not self:ready(job) then return nil, "busy" end
        local before = inv:GetEquippedItem(hands)
        if not item or not item:IsValid() or not item.replica.equippable
            or item.replica.equippable:EquipSlot() ~= hands or locked(item) or locked(before)
            or inv:IsHeavyLifting() then return nil, "unavailable" end
        if not remove and same(before, item) then return { state = function() return "done" end } end
        if remove and not same(before, item) then return nil, "changed" end
        local entry = source(item)
        if not entry or not stable(inv) or not stable(entry.owner) then return nil, "busy" end
        local overflow = inv:GetOverflowContainer()
        if overflow and not stable(overflow) then return nil, "busy" end
        local active = inv:GetActiveItem()
        if active and (remove or not same(active, item)) or Inventory.stack_size(item) ~= 1 then return nil, "unavailable" end
        local replaced = remove and item or before
        local replaced_count = 0
        if replaced then replaced_count = Inventory.stack_size(replaced) end
        if replaced_count == nil then return nil, "busy" end
        local replaced_prefab, replaced_skin, expected_stored, to_cursor
        local containers = {}
        if replaced then
            if replaced.replica.stackable then
                -- GiveItem can merge old equipment into specialized pocket containers.
                -- Capture them before the request; unreadable contents cannot count as zero.
                for _, stored_entry in ipairs(Inventory.items(ctx, true)) do
                    local owner = stored_entry.item.replica.container
                    if stored_entry.slot and owner and owner.priorityfn and owner:CanBeOpened()
                        and owner:CanTakeItemInSlot(replaced) then
                        if not stable(owner) then return nil, "busy" end
                        containers[#containers + 1] = owner
                    end
                end
            end
            local capacity = Inventory.capacity(ctx, inv, replaced)
            if not remove and entry.slot and entry.owner:CanTakeItemInSlot(replaced, entry.slot) then
                local stack = replaced.replica.stackable
                capacity = capacity + (entry.owner:AcceptsStacks() and stack and stack:MaxSize() or 1)
            end
            if not inv:CanTakeItemInSlot(replaced) or capacity < replaced_count then
                if not (remove and allow_cursor and inv:GetNumSlots() > 0
                    and item.replica.inventoryitem:CanGoInContainer()) then return nil, "full" end
                to_cursor = true
            end
            if replaced.replica.stackable and not to_cursor then
                replaced_prefab, replaced_skin = replaced.prefab, replaced.skinname
                expected_stored = stored_count(replaced_prefab, replaced_skin, containers)
                if expected_stored == nil then return nil, "busy" end
                expected_stored = expected_stored + replaced_count
                    - (not remove and entry.slot and item.prefab == replaced_prefab and item.skinname == replaced_skin and 1 or 0)
            end
        end
        if player.components.playercontroller:IsBusy() then return nil, "busy" end
        local expected = not remove and item or nil
        local receipt = { deadline = ctx.G.GetTime() + 6, cursor = to_cursor }
        function receipt:state()
            if self.result then return self.result end
            if not Inventory.readable(ctx, inv) or not Inventory.readable(ctx, entry.owner) then self.result = "changed"
            elseif stable(inv) and stable(entry.owner) and (not overflow or stable(overflow)) then
                local current = inv:GetEquippedItem(hands)
                if same(current, expected) then
                    if replaced and not expected_stored and not replaced:IsValid() then self.result = "changed"
                    elseif (to_cursor and same(inv:GetActiveItem(), item)
                            or not to_cursor and (not replaced
                                or expected_stored and stored_count(replaced_prefab, replaced_skin, containers) == expected_stored
                                or not expected_stored and stored(replaced)))
                        and (not entry.slot or not same(entry.owner:GetItemInSlot(entry.slot), item)) then self.result = "done" end
                elseif current and not same(current, before) then self.result = "changed" end
            end
            if not self.result and ctx.G.GetTime() >= self.deadline then self.result = "timeout" end
            return self.result or "pending"
        end
        local sent = Actions.query(ctx, function()
            local actions = player.components.playeractionpicker:GetInventoryActions(item) or {}
            if not actions[1] or actions[1].action ~= (remove and ctx.G.ACTIONS.UNEQUIP or ctx.G.ACTIONS.EQUIP) then
                return false
            end
            -- Native inventory use asks its picker again. Both selections and dispatch own this intent.
            -- The inventory retains in-flight requests across cancellation and HUD replacement.
            inv._fieldkit_pending_equipment = receipt
            if to_cursor then inv:TakeActiveItemFromEquipSlot(hands)
            elseif same(active, item) then inv:EquipActionItem(item)
            else inv:UseItemFromInvTile(item) end
            return true
        end)
        if not sent then return nil, "unavailable" end
        return receipt
    end
    function self:equip(job, item) return request(job, item, false) end
    function self:unequip(job, item, allow_cursor) return request(job, item, true, allow_cursor) end
    return self
end

return M
