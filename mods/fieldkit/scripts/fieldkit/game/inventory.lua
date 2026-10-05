local M = {}

-- Only the owner's main inventory, equipped hands and accessible backpack are considered.
function M.items(ctx, include_locked, extra_containers)
    local inv, result, seen = ctx.player.replica.inventory, {}, {}
    if not inv then return result end
    local function add(item, owner, slot)
        if not item or not item:IsValid() then return end
        local identity = item.GUID or item
        local replica = item.replica and item.replica.inventoryitem
        if not seen[identity] and (include_locked or not (replica and replica.IsLockedInSlot and replica:IsLockedInSlot())) then
            seen[identity] = true
            result[#result + 1] = { item = item, owner = owner, slot = slot }
        end
    end
    add(inv:GetActiveItem(), inv)
    if ctx.G.EQUIPSLOTS and inv.GetEquippedItem then add(inv:GetEquippedItem(ctx.G.EQUIPSLOTS.HANDS), inv) end
    local function slots(owner)
        if owner and owner.GetNumSlots and owner.GetItemInSlot then
            for slot = 1, owner:GetNumSlots() do add(owner:GetItemInSlot(slot), owner, slot) end
        end
    end
    slots(inv)
    slots(inv.GetOverflowContainer and inv:GetOverflowContainer())
    for _, owner in ipairs(extra_containers or {}) do slots(owner) end
    return result
end

function M.stack_size(item)
    if not item or not item:IsValid() then return end
    local stack = item.replica and item.replica.stackable
    if stack and stack.GetPreviewStackSize and stack:GetPreviewStackSize() ~= nil then return end
    return stack and stack:StackSize() or 1
end

function M.readable(ctx, owner)
    return owner and owner:IsOpenedBy(ctx.player)
        and not (owner.IsReadOnlyContainer and owner:IsReadOnlyContainer())
end

local function native_busy(ctx, owner)
    if owner == ctx.player.replica.inventory then
        return not (ctx.player.components and ctx.player.components.inventory)
            and (not owner.classified or owner.classified:IsBusy())
    end
    return not owner or owner:IsBusy()
end

function M.busy(ctx, owner)
    local inv = ctx.player.replica.inventory
    local repair = inv and inv._fieldkit_pending_repair
    if repair then
        if repair:state() == "pending" then return true end
        inv._fieldkit_pending_repair = nil
    end
    local pending = inv and inv._fieldkit_pending_take
    if pending then
        if ctx.G.GetTime() < pending.deadline and pending.receipt:state() == "pending" then return true end
        inv._fieldkit_pending_take = nil
    end
    return native_busy(ctx, owner)
end

function M.slot(ctx, owner, slot)
    if not M.readable(ctx, owner) or type(slot) ~= "number" or slot ~= slot or slot ~= math.floor(slot)
        or slot < 1 or slot > owner:GetNumSlots() then return nil, "unavailable" end
    if M.busy(ctx, owner) then return nil, "busy" end
    local item = owner:GetItemInSlot(slot)
    local count = 0
    if item then count = M.stack_size(item) end
    if count == nil then return nil, "busy" end
    return { owner = owner, slot = slot, item = item, count = count }
end

function M.stackable(a, b)
    return a and b and a:IsValid() and b:IsValid() and a.replica.stackable
        and a.replica.stackable:CanStackWith(b) or false
end

function M.slot_limit(owner, item, existing)
    local stack = (existing or item).replica.stackable
    if not stack or not owner:AcceptsStacks() then return 1 end
    -- Existing stacks retain container upgrades; an incoming split starts at its ordinary limit.
    if not existing and stack.OriginalMaxSize then return stack:OriginalMaxSize() end
    return stack:MaxSize()
end

local function quantity(count)
    return type(count) == "number" and count == math.floor(count) and count >= 1 and count <= 65535
end

local function unlocked(item)
    return item and item.replica.inventoryitem and not item.replica.inventoryitem:IsLockedInSlot()
end

local function take_limit(item, count)
    local stack = item and item.replica.stackable
    return stack and stack.IsOverStacked and stack:IsOverStacked() and stack:OriginalMaxSize() or count
end

-- Native prediction is never a receipt. Both ends must reach the expected stable counts.
local function receipt(checks)
    return { state = function()
        local pending = false
        for _, check in ipairs(checks) do
            local count, changed = check.read()
            if changed then return "changed" end
            if count ~= check.after then pending = true end
        end
        return pending and "pending" or "done"
    end }
end

local function item_check(ctx, owner, slot, before, after, sample, identity, replacement)
    local prefab, skin = sample.prefab, sample.skinname
    return { after = after, read = function()
        if not M.readable(ctx, owner) then return nil, true end
        if native_busy(ctx, owner) then return end
        local item
        if slot then item = owner:GetItemInSlot(slot) else item = owner:GetActiveItem() end
        if not item then return 0 end
        local count = M.stack_size(item)
        if count == nil then return end
        if replacement and after == 0 and item.GUID ~= identity and item.prefab == replacement and count == 1 then
            return 0
        end
        local changed = item.prefab ~= prefab or item.skinname ~= skin
            or identity and item.GUID ~= identity or count > math.max(before, after)
            or count < math.min(before, after)
        return count, changed
    end }
end

function M.take(ctx, owner, slot, count)
    local entry, why = M.slot(ctx, owner, slot)
    local inv = ctx.player.replica.inventory
    if not entry then return nil, why end
    if not quantity(count) or count > take_limit(entry.item, entry.count) or not unlocked(entry.item)
        or not M.readable(ctx, inv) or inv:GetActiveItem() then return nil, "unavailable" end
    if M.busy(ctx, inv) then return nil, "busy" end
    local result = receipt({
        item_check(ctx, owner, slot, entry.count, entry.count - count, entry.item, entry.item.GUID),
        item_check(ctx, inv, nil, 0, count, entry.item),
    })
    if owner == inv then
        -- The current replica drops count, and its classified sends count in the container argument.
        local component = ctx.player.components and ctx.player.components.inventory
        if component then component:TakeActiveItemFromCountOfSlot(slot, count)
        else
            -- Bare RPCs have no native busy marker; cancellation or a rebuilt HUD must still wait for their receipt.
            inv._fieldkit_pending_take = { receipt = result, deadline = ctx.G.GetTime() + 6 }
            ctx.G.SendRPCToServer(ctx.G.RPC.TakeActiveItemFromCountOfSlot, slot, nil, count)
        end
    else owner:TakeActiveItemFromCountOfSlot(slot, count) end
    return result
end

function M.put(ctx, owner, slot, count)
    local entry, why = M.slot(ctx, owner, slot)
    local inv = ctx.player.replica.inventory
    if not entry then return nil, why end
    if M.busy(ctx, inv) then return nil, "busy" end
    local active = inv:GetActiveItem()
    local size = active and M.stack_size(active)
    if not size then return nil, "busy" end
    if not quantity(count) or count > size
        or not owner:CanTakeItemInSlot(active, slot)
        or entry.item and (not owner:AcceptsStacks() or not M.stackable(entry.item, active))
        or count > 1 and not owner:AcceptsStacks() then return nil, "unavailable" end
    local maximum = M.slot_limit(owner, active, entry.item)
    if entry.count + count > maximum then return nil, "full" end
    local merge_all = entry.item and count == math.min(size, maximum - entry.count)
    if count ~= 1 and count ~= size and not merge_all then return nil, "unavailable" end
    local result = receipt({
        item_check(ctx, inv, nil, size, size - count, active, active.GUID),
        item_check(ctx, owner, slot, entry.count, entry.count + count, active, entry.item and entry.item.GUID),
    })
    local method = entry.item and (merge_all and "AddAllOfActiveItemToSlot" or "AddOneOfActiveItemToSlot")
        or (count == size and "PutAllOfActiveItemInSlot" or "PutOneOfActiveItemInSlot")
    owner[method](owner, slot)
    return result
end

-- MoveItem can route into pocket containers or equipped stacks before ordinary slots.
function M.receivers(ctx, owner, item, source)
    local inv, result, seen = ctx.player.replica.inventory, {}, {}
    local function visit(target)
        if not target or target == source or seen[target] then return true end
        if not M.readable(ctx, target) then return false, "unavailable" end
        if M.busy(ctx, target) then return false, "busy" end
        seen[target] = true
        local specific = target.GetSpecificSlotForItem and target:GetSpecificSlotForItem(item)
        local pockets, from_pocket = {}, false
        for slot = 1, target:GetNumSlots() do
            local existing = target:GetItemInSlot(slot)
            if (not specific or slot == specific) and target:CanTakeItemInSlot(item, slot) then
                result[#result + 1] = { owner = target, slot = slot, item = existing }
            end
            local pocket = existing and existing.replica.container
            if pocket and pocket.priorityfn then
                from_pocket = from_pocket or pocket == source
                if M.readable(ctx, pocket) and pocket:ShouldPrioritizeContainer(item) then
                    pockets[#pockets + 1] = pocket
                end
            end
        end
        -- Native moves out of a pocket disable all specialized overflow on its parent.
        if not specific and not from_pocket then
            for _, pocket in ipairs(pockets) do
                local ok, why = visit(pocket)
                if not ok then return false, why end
            end
        end
        if target == inv then
            for slot, equipped in pairs(inv:GetEquips()) do
                local component = equipped.components and equipped.components.equippable
                if M.stackable(equipped, item) and (not component or component.equipstack) then
                    result[#result + 1] = { owner = inv, slot = slot, item = equipped, equipped = true }
                end
            end
            local overflow = inv:GetOverflowContainer()
            if overflow and overflow ~= source and overflow:CanTakeItemInSlot(item) then
                return visit(overflow)
            end
        end
        return true
    end
    if not owner or owner == source or not owner:CanTakeItemInSlot(item) then return nil, "unavailable" end
    local ok, why = visit(owner)
    if ok then return result end
    return nil, why
end

function M.capacity(ctx, owner, item, source)
    local receivers = M.receivers(ctx, owner, item, source)
    if not receivers then return 0 end
    local amount = 0
    for _, entry in ipairs(receivers) do
        local existing = entry.item
        if not existing then amount = amount + M.slot_limit(entry.owner, item)
        elseif existing ~= item and (not item.GUID or existing.GUID ~= item.GUID)
            and entry.owner:AcceptsStacks() and M.stackable(existing, item) then
            local size = M.stack_size(existing)
            if size == nil then return 0 end
            amount = amount + math.max(0, M.slot_limit(entry.owner, item, existing) - size)
        end
    end
    return amount
end

function M.move(ctx, entry, destination, count, options)
    local source, why = M.slot(ctx, entry.owner, entry.slot)
    local owner = destination and destination.replica and (destination.replica.container or destination.replica.inventory)
    if not source then return nil, why end
    if options ~= nil and type(options) ~= "table" then return nil, "unavailable" end
    local replacement = options and options.source_replacement
    if replacement and (type(replacement) ~= "string" or replacement == "" or source.count ~= count) then
        return nil, "unavailable"
    end
    if source.item ~= entry.item or not unlocked(source.item) or not quantity(count) or count > take_limit(source.item, source.count)
        or owner == source.owner or not M.readable(ctx, owner) or not owner:CanTakeItemInSlot(source.item) then
        return nil, "unavailable"
    end
    local receivers, reason = M.receivers(ctx, owner, source.item, source.owner)
    if not receivers then return nil, reason end
    local prefab, skin = source.item.prefab, source.item.skinname
    local function received()
        if not M.readable(ctx, owner) then return nil, true end
        local total, seen = 0, {}
        for _, target in ipairs(receivers) do
            if not M.readable(ctx, target.owner) then return nil, true end
            if M.busy(ctx, target.owner) then return end
            local item
            if target.equipped then item = target.owner:GetEquippedItem(target.slot)
            else item = target.owner:GetItemInSlot(target.slot) end
            if item and not seen[item.GUID or item] and item.prefab == prefab and item.skinname == skin then
                local size = M.stack_size(item)
                if not size then return end
                seen[item.GUID or item], total = true, total + size
            end
        end
        return total
    end
    local before = received()
    if before == nil then return nil, "busy" end
    local result = receipt({
        item_check(ctx, source.owner, source.slot, source.count, source.count - count, source.item, source.item.GUID, replacement),
        { read = received, after = before + count },
    })
    source.owner:MoveItemFromCountOfSlot(source.slot, destination, count)
    return result
end

function M.count(ctx, prefab)
    local count, seen = 0, {}
    local function add(item)
        if not item or not item:IsValid() or item.prefab ~= prefab then return true end
        local identity = item.GUID or item
        if seen[identity] then return true end
        local size = M.stack_size(item)
        if size == nil then return false end
        seen[identity], count = true, count + size
        return true
    end
    -- Effect receipts include locked slots and auto-equipped crafted products.
    for _, entry in ipairs(M.items(ctx, true)) do
        if not add(entry.item) then return end
    end
    local inv = ctx.player.replica.inventory
    if inv and inv.GetEquippedItem then
        for _, slot in pairs(ctx.G.EQUIPSLOTS or {}) do
            if not add(inv:GetEquippedItem(slot)) then return end
        end
    end
    return count
end

function M.refill(ctx, prefab)
    local inv = ctx.player.replica.inventory
    local active = inv:GetActiveItem()
    if active then return active:IsValid() and active.prefab == prefab end
    for _, entry in ipairs(M.items(ctx)) do
        if entry.item.prefab == prefab and entry.slot and entry.owner.TakeActiveItemFromAllOfSlot then
            entry.owner:TakeActiveItemFromAllOfSlot(entry.slot)
            return true
        end
    end
    return false
end

function M.equipped(ctx, item)
    local inv = ctx.player.replica.inventory
    local equipped = inv and ctx.G.EQUIPSLOTS and inv:GetEquippedItem(ctx.G.EQUIPSLOTS.HANDS)
    return equipped and item and (equipped == item or equipped.GUID ~= nil and equipped.GUID == item.GUID) or false
end

function M.usage(item)
    if not item or not item:IsValid() then return end
    local components, percent, resolution = item.components
    local component = components and (components.armor or components.finiteuses or components.fueled)
    if component then
        percent = component:GetPercent()
    else
        local replica = item.replica and item.replica.inventoryitem
        local classified = replica and replica.classified
        local encoded = classified and classified.percentused and classified.percentused:value()
        -- inventoryitem_classified encodes 0..100%; 255 means no known usage value.
        if type(encoded) == "number" and encoded >= 0 and encoded <= 100 then percent, resolution = encoded / 100, .01 end
    end
    if type(percent) == "number" and percent >= 0 and percent <= 1 then return percent, resolution end
end

function M.recipe(ctx, value)
    local name = type(value) == "table" and value.name or value
    local recipe = type(name) == "string" and ctx.G.GetValidRecipe and ctx.G.GetValidRecipe(name)
    local builder = ctx.player.replica.builder
    if not recipe or not builder then return nil, "unavailable" end
    local accessible = builder:KnowsRecipe(recipe) or builder.CanLearn and builder.GetTechTrees
        and ctx.G.CanPrototypeRecipe and builder:CanLearn(recipe.name)
        and ctx.G.CanPrototypeRecipe(recipe.level, builder:GetTechTrees())
    if not accessible then return nil, "unavailable" end
    if not builder:HasIngredients(recipe) then return nil, "ingredients" end
    return recipe
end

-- Select one real native craft; callers count only the requested final recipe.
function M.craft_step(ctx, value)
    local builder, inventory = ctx.player.replica.builder, ctx.player.replica.inventory
    if not builder or not inventory then return nil, "unavailable" end
    local visiting, remaining = {}, 64
    local function visit(request, depth)
        local name = type(request) == "table" and request.name or request
        if type(name) ~= "string" or not ctx.G.GetValidRecipe then return nil, "unavailable" end
        local recipe = ctx.G.GetValidRecipe(name)
        if not recipe then return nil, "unavailable" end
        -- ponytail: bound dependency traversal to 8 levels/64 visits; choose an intermediate
        -- explicitly for deeper modded chains instead of maintaining a reservation/DAG planner.
        if depth > 8 or remaining == 0 or visiting[recipe.name] then return nil, "ingredients" end
        remaining = remaining - 1
        local craftable, reason = M.recipe(ctx, recipe)
        if craftable or reason ~= "ingredients" then return craftable, reason end
        visiting[recipe.name] = true
        for _, ingredient in ipairs(recipe.ingredients or {}) do
            local amount = math.max(1, ctx.G.RoundBiasedUp(ingredient.amount * builder:IngredientMod()))
            if not inventory:Has(ingredient.type, amount, true) then
                local intermediate = ctx.G.GetValidRecipe(ingredient.type)
                if intermediate then
                    local _, access = M.recipe(ctx, intermediate)
                    if access == "unavailable" then
                        -- Native forwarding is for inaccessible base recipes, not cheaper alternatives.
                        local forwarded
                        for _, alternative in ipairs(intermediate.forward_ingredients or {}) do
                            local candidate = ctx.G.GetValidRecipe(alternative)
                            local _, unavailable = M.recipe(ctx, candidate)
                            if candidate and unavailable ~= "unavailable" then forwarded = candidate; break end
                        end
                        intermediate = forwarded
                    end
                    if intermediate and not intermediate.placer then
                        local next_recipe = visit(intermediate, depth + 1)
                        if next_recipe then visiting[recipe.name] = nil; return next_recipe end
                    end
                end
            end
        end
        visiting[recipe.name] = nil
        return nil, "ingredients"
    end
    return visit(value, 1)
end

return M
