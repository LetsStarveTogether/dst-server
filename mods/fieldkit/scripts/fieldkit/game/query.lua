local M = {}

function M.visible(ctx, entity)
    return entity ~= nil and entity:IsValid() and entity.entity ~= nil
        and entity.entity:IsVisible() and not entity:HasTag("INLIMBO")
        and not entity:HasTag("NOCLICK") and entity.Transform ~= nil
        and ctx.G.CanEntitySeeTarget(ctx.player, entity)
end

function M.observed(ctx, entity, include_fx)
    if include_fx then
        if not entity or not entity:IsValid() or not entity.entity or not entity.entity:IsVisible()
            or not entity.Transform or entity:HasTag("INLIMBO") or entity:HasTag("CLASSIFIED")
            or not ctx.G.CanEntitySeeTarget(ctx.player, entity) then return false end
    elseif not M.visible(ctx, entity) then return false end
    local x, y = ctx.G.TheSim:GetScreenPos(entity.Transform:GetWorldPosition())
    local width, height = ctx.G.TheSim:GetScreenSize()
    return x ~= nil and y ~= nil and x >= 0 and y >= 0 and x <= width and y <= height
end

function M.nearby(ctx, radius, tags, notags, oneoftags, predicate, limit)
    local x, y, z = ctx.player.Transform:GetWorldPosition()
    local found = ctx.G.TheSim:FindEntities(x, y, z, radius, tags, notags or { "INLIMBO", "NOCLICK" }, oneoftags)
    local visible = {}
    for _, entity in ipairs(found) do
        if M.visible(ctx, entity) and (not predicate or predicate(entity)) then visible[#visible + 1] = entity end
    end
    table.sort(visible, function(a, b)
        local da, db = ctx.player:GetDistanceSqToInst(a), ctx.player:GetDistanceSqToInst(b)
        return da < db or (da == db and a.GUID < b.GUID)
    end)
    -- Apply the caller's predicate before the budget; unrelated nearby entities cannot crowd it out.
    limit = math.max(1, math.min(math.floor(limit or 128), 256))
    for i = #visible, limit + 1, -1 do visible[i] = nil end
    return visible
end

function M.ground_items(ctx, predicate)
    local function pickable(entity)
        local item = entity.replica and entity.replica.inventoryitem
        return item and item:CanBePickedUp(ctx.player) and not item:IsHeld()
            and (not predicate or predicate(entity))
    end
    return M.nearby(ctx, ctx.params.search_radius, { "_inventoryitem" }, nil, nil, pickable, 256)
end

return M
