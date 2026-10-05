local Parameters = require("fieldkit/core/parameters")
local Geometry = require("fieldkit/rules/geometry")
local NativePlacement = require("fieldkit/game/placement")
local Actions = require("fieldkit/game/actions")
local Scope = require("fieldkit/core/scope")
local M = {}
local sessions = setmetatable({}, { __mode = "k" })
local function valid(entity) return entity and entity:IsValid() end
local function vector(ctx, x, z) return ctx.G.Vector3(x, 0, z) end
local function platform_at(ctx, point) return ctx.G.TheWorld.Map:GetPlatformAtPoint(point.x, point.z) end
local function local_point(point, platform)
    if platform then
        local x, _, z = platform.entity:WorldToLocalSpace(point.x, 0, point.z)
        return { x = x, y = 0, z = z }
    end
    return { x = point.x, y = 0, z = point.z }
end
local function resolve(ctx, record)
    if not record or not Geometry.finite(record.x) or not Geometry.finite(record.z) then return end
    if record.platform then
        if not valid(record.platform) then return end
        local x, _, z = record.platform.entity:LocalToWorldSpace(record.x, 0, record.z)
        return vector(ctx, x, z), record.rotation and record.rotation + record.platform.Transform:GetRotation()
    end
    return vector(ctx, record.x, record.z), record.rotation
end
local function get_session(ctx)
    if sessions[ctx] then return sessions[ctx] end
    local session = { placers = {}, pending = {}, adapter = NativePlacement.new(ctx), enabled = ctx.config.grid ~= false,
        rotation = 0, previous_shape = "diamond" }
    sessions[ctx] = session
    ctx.scope:defer(function()
        for _, cleanup in pairs(session.pending) do cleanup() end
        for _, cleanup in pairs(session.placers) do cleanup() end
        sessions[ctx] = nil
    end)
    return session
end
local function active(ctx)
    if ctx.scope.closed then return false end
    return get_session(ctx).enabled and ctx:ready()
        and not (ctx.G.TheInput.IsControlPressed and ctx.G.TheInput:IsControlPressed(ctx.G.CONTROL_AXISALIGNEDPLACEMENT_TOGGLEMOD))
end
local function snapshot(ctx, point, item, recipe, kind, placer)
    local session = get_session(ctx)
    local platform = platform_at(ctx, point)
    local step, origin
    if kind ~= "drop" then step, origin = NativePlacement.lattice(ctx, placer, item) end
    if kind == "water" or kind == "fertilize" or kind == "terraform" then
        local x, _, z = ctx.G.TheWorld.Map:GetTileCenterPoint(0, 0, 0)
        step, origin = 4, { x = x, z = z }
    elseif kind == "till" or ctx.params.grid_smart_spacing and NativePlacement.farm_seed(item) then
        local count = ctx.params.farm_layout or 3
        local native = ctx.G.GetFarmTillSpacing and ctx.G.GetFarmTillSpacing() or 1.25
        step = math.max(4 / count, native + (count == 4 and .01 or .001))
        local x, _, z = ctx.G.TheWorld.Map:GetTileCenterPoint(0, 0, 0)
        local offset = count % 2 == 0 and step / 2 or 0
        -- A world origin keeps dense rows continuous for both pointer movement and area plans.
        origin = { x = x + offset, z = z + offset }
    end
    local fixed = step ~= nil
    if fixed then platform = nil end -- Turf and walls retain the native world lattice.
    if not step then
        step = ctx.params.grid_size
        if kind ~= "drop" and ctx.params.grid_smart_spacing then
            local inv = item and item.replica.inventoryitem
            local native = recipe and recipe.min_spacing or inv and inv:DeploySpacingRadius()
            -- Transform uses floats; a small margin keeps adjacent native-radius sites distinct.
            if Geometry.finite(native) and native > 0 then step = native + 0.001 end
        end
        origin = session.origin and session.origin.platform == platform and session.origin or nil
    end
    return { platform = platform, fixed = fixed, kind = kind,
        grid = Geometry.new({ shape = fixed and "square" or ctx.params.grid_shape, spacing = step,
            rotation = fixed and 0 or session.rotation, origin = origin }) }
end
local function snap(ctx, point, frame)
    if frame.platform and not valid(frame.platform) then return end
    local p = local_point(point, frame.platform)
    local x, z, col, row = Geometry.snap(frame.grid, p.x, p.z)
    local record = { x = x, y = 0, z = z, col = col, row = row, platform = frame.platform }
    return resolve(ctx, record), record
end
local function controller_override(placer, ctx)
    return placer.controllergroundoverridefn and ctx.G.TheInput:ControllerAttached()
end
function M.supported(placer, ctx)
    return placer.builder == ctx.player and active(ctx) and not placer.inst.parent
        and not controller_override(placer, ctx) and not NativePlacement.native_projection(placer)
end
local function owns_transform(placer, ctx)
    return not (placer.snap_to_tile or placer.snap_to_meters or placer.snaptogrid
        or placer.snap_to_half_tile or placer.snap_to_boat_edge or placer.inst.parent
        or controller_override(placer, ctx) or NativePlacement.native_projection(placer))
end
local guide_offsets = {}
local function offsets(hex)
    if guide_offsets[hex] then return guide_offsets[hex] end
    local entries, radius = {}, 4
    for row = -radius, radius do
        for col = -radius, radius do
            if not hex or math.max(math.abs(col), math.abs(row), math.abs(col + row)) <= radius then
                entries[#entries + 1] = { col = col, row = row, distance = col * col + row * row }
            end
        end
    end
    table.sort(entries, function(a, b)
        if a.distance ~= b.distance then return a.distance < b.distance end
        if a.row ~= b.row then return a.row < b.row end
        return a.col < b.col
    end)
    guide_offsets[hex] = entries
    return entries
end
local function create_guides(ctx)
    local guides, cursor = {}, 2
    local function hide() for _, entry in ipairs(guides) do if valid(entry.inst) then entry.inst:Hide() end end end
    local function remove()
        for _, entry in ipairs(guides) do if valid(entry.inst) then entry.inst:Remove() end end
        guides = {}
    end
    local function position(guide, point)
        if guide.draw_x ~= point.x or guide.draw_y ~= point.y or guide.draw_z ~= point.z then
            guide.inst.Transform:SetPosition(point:Get())
            guide.draw_x, guide.draw_y, guide.draw_z = point.x, point.y, point.z
        end
    end
    local function update(frame, centre, test)
        local budget = 12
        local _, record = snap(ctx, centre, frame)
        if not record then hide(); return end
        local entries = offsets(frame.grid.hex)
        -- Creation shares the test budget; a first preview cannot allocate the whole grid at once.
        for i = #guides + 1, math.min(#entries, #guides + budget) do
            local inst = ctx.G.SpawnPrefab("axisalignedplacement_outline")
            if inst then guides[i] = { inst = inst } end
        end
        local count = math.min(#guides, #entries)
        if count == 0 then return end
        for i, guide in ipairs(guides) do
            local offset = entries[i]
            if not offset then guide.inst:Hide()
            else
                local x, z = Geometry.point(frame.grid, record.col + offset.col, record.row + offset.row)
                local point = resolve(ctx, { x = x, z = z, platform = frame.platform })
                if guide.x ~= point.x or guide.z ~= point.z then guide.allowed, guide.projected = nil, nil end
                guide.x, guide.z, guide.point = point.x, point.z, point
                position(guide, guide.projected or point)
                local scale = math.min(frame.grid.spacing, 2)
                if guide.scale ~= scale then
                    guide.inst.AnimState:SetScale(scale, scale)
                    guide.scale = scale
                end
                guide.inst:Show()
            end
        end
        for n = 1, math.min(budget, count) do
            local index = n == 1 and 1 or (cursor - 2) % (count - 1) + 2
            if n > 1 then cursor = index + 1 end
            local guide = guides[index]
            local allowed, projected = test(guide.point)
            guide.allowed, guide.projected = allowed, projected
            position(guide, projected or guide.point)
        end
        for i, guide in ipairs(guides) do
            if entries[i] then
                local good, unknown = guide.allowed == true, guide.allowed == nil
                local style = unknown and -1 or good and 1 or 0
                if guide.style ~= style then
                    guide.inst.AnimState:PlayAnimation(good and "unit" or "unit_x")
                    guide.inst.AnimState:SetMultColour(unknown and 0.6 or good and 0.2 or 1,
                        unknown and 0.6 or good and 0.9 or 0.3, unknown and 0.6 or 0.2, i == 1 and 0.9 or 0.5)
                    guide.style = style
                end
            end
        end
    end
    return { update = update, hide = hide, remove = remove }
end
local function attach(placer, ctx)
    local session = get_session(ctx)
    if session.placers[placer] or placer.inst._fieldkit_probe then return end
    local scope = Scope.new(placer.inst, function(err) ctx:report(err) end)
    local guides = create_guides(ctx)
    local helper, helper_visible
    local function show_helper(show)
        local parent = placer.axisalignedhelpers and placer.axisalignedhelpers.parent
        if helper and (show or helper ~= parent) then
            if valid(helper) and helper_visible then helper:Show() end
            helper = nil
        end
        if not show and valid(parent) then
            if helper ~= parent then helper, helper_visible = parent, parent.entity:IsVisible() end
            parent:Hide()
        end
    end
    scope:wrap(placer, "IsAxisAlignedPlacement", function(original)
        return function(self, ...)
            if session.enabled and self.builder == ctx.player and ctx:ready() and NativePlacement.native_projection(self) then
                return false
            end
            if session.enabled and self.builder == ctx.player and ctx:ready() and owns_transform(self, ctx) then
                return active(ctx)
            end
            return original(self, ...)
        end
    end)
    scope:wrap(placer, "GetAxisAlignedPlacementTransform", function(original)
        return function(self, x, y, z, ignorescale, ...)
            if M.supported(self, ctx) and owns_transform(self, ctx) then
                local point = vector(ctx, x, z)
                local snapped = snap(ctx, point, snapshot(ctx, point, self.invobject, self.recipe, nil, self))
                if snapped then return snapped.x, y, snapped.z end
            end
            return original(self, x, y, z, ignorescale, ...)
        end
    end)
    scope:wrap(placer, "UpdateAxisAlignedHelpers", function(original)
        return function(self, ...)
            if M.supported(self, ctx) then show_helper(false); return end
            show_helper(true)
            return original(self, ...)
        end
    end)
    scope:wrap(placer, "OnUpdate", function(original)
        return function(self, ...)
            local function finish(...)
                if not scope.closed and M.supported(self, ctx) then
                    show_helper(false)
                    if not self.mouse_blocked then
                        local centre = NativePlacement.point(self)
                        guides.update(snapshot(ctx, centre, self.invobject, self.recipe, nil, self), centre,
                            function(point) return NativePlacement.evaluate(ctx, self, point) end)
                    else guides.hide() end
                else guides.hide(); show_helper(true) end
                return ...
            end
            return finish(original(self, ...))
        end
    end)
    scope:defer(function()
        guides.remove()
        show_helper(true)
        session.placers[placer] = nil
    end)
    local function cleanup() scope:close() end
    session.placers[placer] = cleanup
    scope:listen("onremove", cleanup)
end
function M.install(env, get_context)
    env.AddComponentPostInit("placer", function(placer)
        local ctx = get_context()
        if not ctx or ctx.scope.closed then return end
        local session = get_session(ctx)
        local scope = Scope.new(placer.inst, function(err) ctx:report(err) end)
        local function cleanup() scope:close() end
        scope:defer(function() session.pending[placer] = nil end)
        scope:wrap(placer, "SetBuilder", function(original)
            return function(self, builder, ...)
                local function finish(...)
                    local install = not scope.closed and get_context() == ctx and builder == ctx.player
                    cleanup()
                    if install then attach(self, ctx) end
                    return ...
                end
                return finish(original(self, builder, ...))
            end
        end)
        session.pending[placer] = cleanup
        scope:listen("onremove", cleanup)
    end)
end
function M.new(ctx)
    local session, self = get_session(ctx), {}
    function self:snapshot(point, item, recipe, kind)
        return session.adapter:with_probe(item, recipe, kind, function(placer)
            return snapshot(ctx, point, item, recipe, kind, placer)
        end)
    end
    function self:snap(point, item, recipe, kind, frame)
        return snap(ctx, point, frame or self:snapshot(point, item, recipe, kind))
    end
    function self:resolve(record) return resolve(ctx, record) end
    function self:validate(point, item, recipe, kind, orientation)
        return session.adapter:validate(point, item, recipe, kind, orientation)
    end
    function self:context()
        return NativePlacement.context(ctx, self.till_point, self.till_valid)
    end
    function self:points(a, b, limit, item, recipe, kind, orientation)
        return session.adapter:with_probe(item, recipe, kind, function(placer, validate)
            local corners = a.corners or { a, { x = b.x, z = a.z }, b, { x = a.x, z = b.z } }
            local frame = snapshot(ctx, corners[1], item, recipe, kind, placer)
            local platform, polygon = platform_at(ctx, corners[1]), {}
            if (kind == "water" or kind == "fertilize" or kind == "terraform") and platform then return nil, "ground_only" end
            for _, point in ipairs(corners) do
                if platform_at(ctx, point) ~= platform then return nil, "mixed_platforms" end
                polygon[#polygon + 1] = local_point(point, frame.platform)
            end
            local points, reason = Geometry.area(frame.grid, polygon, limit)
            if not points then return nil, reason end
            local out, seen = {}, {}
            for _, record in ipairs(points) do
                record.platform = frame.platform
                local world = resolve(ctx, record)
                if platform_at(ctx, world) ~= platform then return nil, "mixed_platforms" end
                local _, projected, rotation = validate(world, orientation)
                projected = projected or world
                if platform_at(ctx, projected) ~= platform then return nil, "mixed_platforms" end
                -- Fixed world lattices also retain platform ownership after sampling.
                -- Native wall/tile projection is applied again by validate at execution time.
                local adjusted = local_point(projected, platform)
                record.x, record.z, record.rotation, record.platform = adjusted.x, adjusted.z,
                    rotation and rotation - (platform and platform.Transform:GetRotation() or 0), platform
                local key = string.format("%.5f:%.5f", record.x, record.z)
                if not seen[key] then seen[key] = true; out[#out + 1] = record end
            end
            return out, frame
        end)
    end
    function self:set_origin(point)
        if not point then return false end
        local platform = platform_at(ctx, point)
        session.origin = local_point(point, platform)
        session.origin.platform = platform
        return true
    end
    function self:reset_origin() session.origin = nil end
    function self:rotate(degrees) session.rotation = (session.rotation + (degrees or 15)) % 360 end
    function self:cycle()
        session.previous_shape = ctx.params.grid_shape
        Parameters.cycle(ctx.params, "grid_shape")
        return ctx.params.grid_shape
    end
    function self:swap_shape()
        local shape = ctx.params.grid_shape
        Parameters.set(ctx.params, "grid_shape", session.previous_shape)
        session.previous_shape = shape
        return ctx.params.grid_shape
    end
    function self:toggle() session.enabled = not session.enabled; return session.enabled end
    function self:status()
        local current = self:context()
        if not current then return "" end
        local placer = NativePlacement.current(ctx)
        if placer and NativePlacement.native_projection(placer) then
            return ctx:text("网格：原生定位", "Grid: native placement")
        end
        if not active(ctx) or placer and not M.supported(placer, ctx) then
            return ctx:text("网格：自由放置", "Grid: free placement")
        end
        local frame = snapshot(ctx, current.point, current.item, current.recipe, current.kind, placer)
        return string.format("%s %s %.3g · %d° · %s", ctx:text("网格", "Grid"), frame.grid.shape,
            frame.grid.spacing, frame.fixed and 0 or session.rotation,
            current.valid and ctx:text("可放置", "valid") or ctx:text("不可放置", "invalid"))
    end
    local controller = ctx.player.components.playercontroller
    local guides = create_guides(ctx)
    ctx.scope:defer(guides.remove)
    local function till_point()
        if not active(ctx) or controller.placer_recipe or controller.deployplacer or controller:IsAOETargeting()
            or NativePlacement.current(ctx) then return end
        local inv = ctx.player.replica and ctx.player.replica.inventory
        local item = inv and inv:GetEquippedItem(ctx.G.EQUIPSLOTS.HANDS)
        if not item or not item:HasActionComponent("farmtiller") or inv:GetActiveItem() then return end
        local point = ctx.G.TheInput:GetWorldPosition()
        if ctx.G.TheInput:ControllerAttached() then
            local x, _, z = ctx.player.entity:LocalToWorldSpace(1, 0, 0)
            point = vector(ctx, x, z)
        end
        return self:snap(point, nil, nil, "till"), item
    end
    if ctx.player.DoPeriodicTask then
        ctx.scope:periodic(0.1, function()
            self.till_point = till_point()
            if self.till_point then
                self.till_valid = self:validate(self.till_point, nil, nil, "till")
                guides.update(self:snapshot(self.till_point, nil, nil, "till"), self.till_point,
                    function(point) return self:validate(point, nil, nil, "till") end)
            else guides.hide() end
        end)
    end
    local till_ticket
    ctx.scope:defer(function() if till_ticket then till_ticket:close() end end)
    local function do_till(point, item)
        if not point or not self:validate(point, nil, nil, "till") then return false end
        local picker = ctx.player.components.playeractionpicker
        for _, action in ipairs(picker and picker:GetPointActions(point, item, true) or {}) do
            if action.action == ctx.G.ACTIONS.TILL then
                if till_ticket then till_ticket:close() end
                till_ticket = Actions.dispatch(ctx, action, "right")
                return till_ticket ~= nil
            end
        end
        return false
    end
    if controller and controller.OnRightClick then
        ctx.scope:wrap(controller, "OnRightClick", function(original)
            return function(pc, down, ...)
                local point, item
                if down and pc:UsingMouse() and ctx.G.TheInput:GetHUDEntityUnderMouse() == nil then
                    point, item = till_point()
                end
                local native = point and pc:GetRightMouseAction()
                if point and (not native or native.action == ctx.G.ACTIONS.TILL) then
                    do_till(point, item)
                    return
                end
                return original(pc, down, ...)
            end
        end)
    end
    if controller and controller.DoControllerAltActionButton then
        ctx.scope:wrap(controller, "DoControllerAltActionButton", function(original)
            return function(pc, ...)
                local point, item = till_point()
                local _, native = pc:GetGroundUseAction()
                if point and native and native.action == ctx.G.ACTIONS.TILL then
                    do_till(point, item)
                    return
                end
                return original(pc, ...)
            end
        end)
    end
    return self
end
return M
