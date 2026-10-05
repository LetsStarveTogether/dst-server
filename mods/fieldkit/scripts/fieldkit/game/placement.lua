-- Native placement projection and validation. Geometry and presentation live above this boundary.
local M = {}
local function valid(entity) return entity and entity:IsValid() end
local function vector(ctx, x, z) return ctx.G.Vector3(x, 0, z) end
local function platform_at(ctx, point) return ctx.G.TheWorld.Map:GetPlatformAtPoint(point.x, point.z) end
function M.current(ctx)
    local controller = ctx.player.components.playercontroller
    local inst = controller and (controller.placer or controller.deployplacer)
    return valid(inst) and inst.components.placer or nil
end
function M.selected_recipe(ctx)
    local controller = ctx.player.components.playercontroller
    if not controller then return end
    if controller.placer_recipe then return controller.placer_recipe, controller.placer_recipe_skin end
    -- Native HUD focus removes the visual placer but retains the player's recipe selection.
    local cached = controller.placer_cached
    if cached then return cached[1], cached[2] end
end
function M.lattice(ctx, placer, item)
    local inv = item and item.replica.inventoryitem
    local mode = inv and inv:GetDeployMode()
    local modes = ctx.G.DEPLOYMODE or {}
    if placer and placer.snap_to_tile or mode and mode == modes.TURF then
        local x, _, z = ctx.G.TheWorld.Map:GetTileCenterPoint(0, 0, 0)
        return 4, { x = x, z = z }
    elseif placer and placer.snap_to_meters or mode and mode == modes.WALL then
        return 1, { x = 0.5, z = 0.5 }
    elseif placer and placer.snaptogrid then
        return 1, { x = 0, z = 0 }
    elseif placer and placer.snap_to_half_tile then
        local x, _, z = require("prefabs/trap_fumarole_util").GetTrapCenterPoint(0, 0, 0)
        return 2, { x = x, z = z }
    end
end
function M.point(placer)
    local point = placer.override_build_point_fn and placer.override_build_point_fn(placer.inst)
    return point or placer.inst:GetPosition()
end
function M.farm_seed(item)
    return item and item.HasActionComponent and item:HasActionComponent("farmplantable")
        and item:HasActionComponent("deployable") or false
end
function M.native_projection(placer)
    return placer.onupdatetransform ~= nil and (placer.override_build_point_fn ~= nil
        or placer.oncanbuild ~= nil and placer.oncannotbuild ~= nil)
end
-- Native custom tests often consume state derived by onupdatetransform; run both together.
function M.evaluate(ctx, placer, point, orientation)
    local inst, G = placer.inst, ctx.G
    local old, rotation = inst:GetPosition(), inst.Transform:GetRotation()
    local ok, allowed, blocked, result, result_rotation = pcall(function()
        if placer.snap_to_tile then
            point = G.Vector3(G.TheWorld.Map:GetTileCenterPoint(point.x, 0, point.z))
        elseif placer.snap_to_meters then
            point = vector(ctx, math.floor(point.x) + 0.5, math.floor(point.z) + 0.5)
        elseif placer.snaptogrid then
            point = vector(ctx, math.floor(point.x + 0.5), math.floor(point.z + 0.5))
        elseif placer.snap_to_half_tile then
            point = G.Vector3(require("prefabs/trap_fumarole_util").GetTrapCenterPoint(point.x, 0, point.z))
        end
        inst.Transform:SetPosition(point.x, 0, point.z)
        if orientation ~= nil then inst.Transform:SetRotation(orientation) end
        local rotated_from_boat = false
        if placer.snap_to_boat_edge then
            local boats = G.TheSim:FindEntities(point.x, 0, point.z, G.TUNING.MAX_WALKABLE_PLATFORM_RADIUS, placer.BOAT_MUST_TAGS)
            local boat = G.GetClosest(inst, boats)
            if not boat then return false, false, point, rotation end
            G.SnapToBoatEdge(inst, boat, point)
        end
        if placer.rotate_from_boat_center then
            local boat = platform_at(ctx, point)
            if boat then
                inst.Transform:SetRotation(-G.GetAngleFromBoat(boat, point.x, point.z) / G.DEGREES)
                rotated_from_boat = true
            end
        end
        if orientation == nil and placer.fixedcameraoffset ~= nil and not rotated_from_boat and G.TheCamera then
            local angle = placer.fixedcameraoffset - G.TheCamera:GetHeading() + (placer.rotationoffset or 0)
            if placer.fixedcameraoffset_nearestangle then
                angle = math.floor(angle / placer.fixedcameraoffset_nearestangle + 0.5) * placer.fixedcameraoffset_nearestangle
            end
            inst.Transform:SetRotation(angle)
        end
        if placer.onupdatetransform then placer.onupdatetransform(inst) end
        local can, mouse = placer:TestCanBuild()
        return can, mouse, M.point(placer), inst.Transform:GetRotation()
    end)
    inst.Transform:SetPosition(old:Get())
    inst.Transform:SetRotation(rotation)
    if placer.onupdatetransform then placer.onupdatetransform(inst) end
    if not ok then error(allowed) end
    return allowed == true and not blocked, result, result_rotation
end

function M.context(ctx, till_point, till_valid)
    if ctx.scope.closed then return end
    local placer = M.current(ctx)
    if placer then
        local point = M.point(placer)
        local platform = platform_at(ctx, point)
        return {
            point = vector(ctx, point.x, point.z), rotation = placer.inst.Transform:GetRotation(),
            platform = platform, platform_rotation = platform and platform.Transform:GetRotation(),
            valid = placer.can_build == true and not placer.mouse_blocked,
            recipe = placer.recipe, item = placer.invobject, kind = placer.recipe and "build" or "deploy",
        }
    elseif till_point then
        return { point = vector(ctx, till_point.x, till_point.z), rotation = 0, valid = till_valid == true, kind = "till" }
    end
end

function M.new(ctx)
    local self = {}
    local function create(item, recipe, kind, capture)
        if kind == "till" or kind == "water" or kind == "fertilize" or kind == "terraform" or kind == "drop" then return end
        local existing = M.current(ctx)
        local inv = item and item.replica.inventoryitem
        local name = recipe and recipe.placer
            or inv and inv:IsDeployable(ctx.player) and inv:GetDeployPlacerName()
        if not name or not ctx.G.Prefabs[name] then return end
        local inst = ctx.G.SpawnPrefab(name)
        capture(inst)
        if not inst or not inst.components.placer then return end
        inst._fieldkit_probe = true
        local placer = inst.components.placer
        -- Validation has no ownership of the visible placer or inventory cursor.
        placer.hide_inv_icon = false
        -- SetBuilder also starts mouse-driven updates; validation only needs its data and prefab callback.
        placer.builder, placer.recipe, placer.invobject = ctx.player, recipe, item
        if placer.onbuilderset then placer.onbuilderset(inst) end
        -- Keep the chosen orientation, but never inherit the visible placer's mouse/HUD test.
        if existing and existing.invobject == item and existing.recipe == recipe then
            inst.Transform:SetRotation(existing.inst.Transform:GetRotation())
            placer.rotationoffset = existing.rotationoffset
            placer.fixedcameraoffset, placer.fixedcameraoffset_nearestangle = existing.fixedcameraoffset, existing.fixedcameraoffset_nearestangle
        end
        placer.testfn = function(point, rotation)
            if recipe then return ctx.player.replica.builder:CanBuildAtPoint(point, recipe, rotation) end
            return item.replica.inventoryitem:CanDeploy(point, nil, ctx.player, rotation)
        end
        inst:Hide()
        for _, linked in ipairs(placer.linked or {}) do linked:Hide() end
        return placer
    end

    local function validate(point, item, recipe, kind, placer, orientation)
        if not point then return false end
        if kind == "till" then
            if not ctx.G.TheWorld.Map:CanTillSoilAtPoint(point.x, 0, point.z) then return false end
            -- CanTill ignores soil; FarmTiller then breaks it within this radius.
            local spacing = ctx.G.GetFarmTillSpacing and ctx.G.GetFarmTillSpacing() or 1.25
            for _, soil in ipairs(ctx.G.TheSim:FindEntities(point.x, 0, point.z, spacing, { "soil" }, { "INLIMBO" })) do
                if soil:IsValid() then return false end
            end
            return true
        end
        if kind == "terraform" then
            local map = ctx.G.TheWorld.Map
            return not map:GetPlatformAtPoint(point.x, point.z) and map:CanTerraformAtPoint(point.x, 0, point.z)
        end
        if kind == "drop" then
            local map = ctx.G.TheWorld.Map
            return map:IsPassableAtPoint(point.x, 0, point.z) and not map:IsGroundTargetBlocked(point)
        end
        if kind == "water" or kind == "fertilize" then
            local map = ctx.G.TheWorld.Map
            if map:GetPlatformAtPoint(point.x, point.z) then return false end
            if kind == "water" then return map:GetTileAtPoint(point.x, 0, point.z) == ctx.G.WORLD_TILES.FARMING_SOIL end
            return item and item.replica.inventoryitem and item.replica.inventoryitem:CanDeploy(point, nil, ctx.player, 0) or false
        end
        if placer then return M.evaluate(ctx, placer, point, orientation) end
        if recipe then return false end
        return item and item.replica.inventoryitem and item.replica.inventoryitem:CanDeploy(point, nil, ctx.player, orientation or 0) or false
    end
    function self:with_probe(item, recipe, kind, fn)
        local inst
        local function run()
            local placer = create(item, recipe, kind, function(value) inst = value end)
            return fn(placer, function(point, orientation) return validate(point, item, recipe, kind, placer, orientation) end)
        end
        local function finish(ok, ...)
            -- Native placers own auxiliary entities that need not be linked or parented.
            -- Their removal hooks release those visuals before the next render frame.
            if valid(inst) then inst:Remove() end
            if not ok then error((...)) end
            return ...
        end
        return finish(pcall(run))
    end
    function self:validate(point, item, recipe, kind, orientation)
        return self:with_probe(item, recipe, kind, function(_, test) return test(point, orientation) end)
    end
    return self
end

return M
