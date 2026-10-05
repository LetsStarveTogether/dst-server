local Actions = require("fieldkit/game/actions")
local M = {}

local function distance(a, b)
    return math.sqrt((a.x - b.x)^2 + (a.z - b.z)^2)
end

function M.capabilities(ctx)
    local player, G = ctx.player, ctx.G
    local locomotor = player.components.playercontroller.locomotor
    local caps = {}
    if locomotor then
        for key, value in pairs(locomotor.pathcaps or {}) do caps[key] = value end
        if locomotor.allow_platform_hopping ~= nil then caps.allowplatformhopping = locomotor.allow_platform_hopping or nil end
    else
        -- Prediction removes the client locomotor; the server keeps these player capabilities.
        caps.player, caps.ignorecreep, caps.allowplatformhopping = true, true, true
    end
    -- Weregoose changes collision physics without changing the native locomotor's pathcaps.
    -- Platform collisions describe the deck, so they cannot establish an ability to cross water.
    if not player:GetCurrentPlatform() and player.Physics
        and G.bit.band(player.Physics:GetCollisionMask(), G.COLLISION.LAND_OCEAN_LIMITS) == 0 then
        caps.allowocean = true
    end
    return caps
end

function M.new(ctx, goal, route)
    local G, player = ctx.G, ctx.player
    local map, finder = G.TheWorld.Map, G.TheWorld.Pathfinder
    local inventory = player.replica.inventory
    local boat = player:GetCurrentPlatform()
    local item = route and boat and inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS) or nil
    local steering = player:HasTag("steeringboat")
    local oar = item and item.HasActionComponent and item:HasActionComponent("oar")
    local rider = player.replica.rider
    -- Deck destinations use native walking and hopping; operating a helm explicitly selects sailing.
    local sailing = route and boat and (steering or oar) and boat:HasTag("boat")
        and not (rider and rider:IsRiding()) and not inventory:GetActiveItem()
        and map:IsOceanAtPoint(goal.x, 0, goal.z, steering)
    local caps = M.capabilities(ctx)
    local mode = sailing and (steering and "steer" or "row")
        or (caps.allowocean or inventory:GetActiveItem()) and "direct" or "walk"
    local self = { mode = mode, sailing = sailing == true, continuous = mode ~= "walk",
        reach = sailing and 1.5 or mode == "direct" and 1 or .35 }
    local ticket, next_row, last_position, last_at, velocity
    local radius = sailing and boat:GetPhysicsRadius(0) or 0
    self.radius = sailing and radius or player:GetPhysicsRadius(0)

    function self:capabilities()
        if self.sailing then return { allowocean = true, ignoreLand = true, ignorecreep = true } end
        return M.capabilities(ctx)
    end
    function self:valid()
        if self.sailing then
            return boat:IsValid() and player:GetCurrentPlatform() == boat
                and (mode == "steer" and player:HasTag("steeringboat")
                    or mode == "row" and inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS) == item and item:IsValid())
        end
        return true
    end
    function self:position() return (self.sailing and boat or player):GetPosition() end
    function self:capture(point)
        if not self.sailing then return G.DynamicPosition(G.Vector3(point.x, 0, point.z)) end
        -- Sailing destinations belong to the world, never to the boat carrying the navigator.
        local fixed = G.Vector3(point.x, 0, point.z)
        return { GetPosition = function() return fixed end }
    end
    function self:passable(point)
        if not self.sailing then return map:IsPassableAtPoint(point.x, 0, point.z, self:capabilities().allowocean) end
        if not map:IsOceanAtPoint(point.x, 0, point.z, true) then return false end
        for angle = 0, 7 do
            local theta = angle * math.pi / 4
            if not map:IsOceanAtPoint(point.x + math.cos(theta) * radius, 0,
                point.z + math.sin(theta) * radius, true) then return false end
        end
        return true
    end
    function self:clear(a, b)
        local capabilities = self:capabilities()
        if not finder:IsClear(a.x, 0, a.z, b.x, 0, b.z, capabilities) then return false end
        if self.sailing then
            if not self:passable(b) then return false end
            local length = distance(a, b)
            if length > .001 then
                local nx, nz = -(b.z - a.z) / length * (radius + .25), (b.x - a.x) / length * (radius + .25)
                for _, sign in ipairs({ -1, -.5, .5, 1 }) do
                    if not finder:IsClear(a.x + nx * sign, 0, a.z + nz * sign,
                        b.x + nx * sign, 0, b.z + nz * sign, capabilities) then return false end
                end
            end
            local reach = length / 2 + radius + G.TUNING.MAX_WALKABLE_PLATFORM_RADIUS
            for _, other in ipairs(G.TheSim:FindEntities((a.x + b.x) / 2, 0, (a.z + b.z) / 2,
                reach, { "boat" }, { "INLIMBO" })) do
                if other ~= boat and other:IsValid() then
                    local p = other:GetPosition()
                    local t = length > .001 and math.max(0, math.min(1,
                        ((p.x - a.x) * (b.x - a.x) + (p.z - a.z) * (b.z - a.z)) / (length * length))) or 0
                    local dx, dz = p.x - a.x - (b.x - a.x) * t, p.z - a.z - (b.z - a.z) * t
                    if dx * dx + dz * dz < (radius + other:GetPhysicsRadius(0) + .25)^2 then return false end
                end
            end
        end
        return true
    end

    function self:stop(previous, reason)
        last_position, last_at, velocity = nil, nil, nil
        if mode ~= "direct" then
            -- The hull may arrive while a row is still approaching the deck edge.
            Actions.stop(ctx, previous, mode == "row" and reason == "done" and "cancelled" or reason)
            return
        end
        if not ticket or ticket.closed then return end
        ticket:close()
        local controller, locomotor = player.components.playercontroller, ticket.locomotor
        if controller ~= ticket.controller or not player:IsValid() or player:GetBufferedAction()
            or controller.locomotor and controller.locomotor.bufferedaction then return end
        if locomotor and controller.locomotor == locomotor then locomotor:Stop() end
        if not controller.ismastersim then
            local remote = controller.remote_vector
            if locomotor or remote and remote.y == 1 and remote.x == ticket.dx and remote.z == ticket.dz then
                controller:RemoteStopWalking()
            end
        end
    end

    function self:move(point, now, previous)
        local controller = player.components.playercontroller
        if mode == "walk" then
            local next_ticket, why = Actions.walk(ctx, point, previous)
            return next_ticket or false, why
        end
        if not self:valid() then return false, ctx:text("航行装备或船只已改变", "Sailing equipment or platform changed") end
        local position = self:position()
        local dx, dz = point.x - position.x, point.z - position.z
        if self.sailing then
            if last_position and now > last_at then
                velocity = { x = (position.x - last_position.x) / (now - last_at),
                    z = (position.z - last_position.z) / (now - last_at) }
            end
            last_position, last_at = position, now
            -- Counter the observed drift as the hull approaches a turn or its destination.
            if velocity then dx, dz = dx - velocity.x * 2, dz - velocity.z * 2 end
        end
        local length = math.sqrt(dx * dx + dz * dz)
        if length < .001 then return previous end
        dx, dz = dx / length, dz / length
        if mode == "direct" then
            if ticket and not ticket.closed and (controller ~= ticket.controller or controller.locomotor ~= ticket.locomotor
                or player:GetBufferedAction() or controller.locomotor and controller.locomotor.bufferedaction) then
                return false, ctx:text("移动控制已改变，导航停止", "Movement control changed; navigation stopped")
            end
            if not ticket or ticket.closed then
                ticket = { controller = controller, locomotor = controller.locomotor, sent = true,
                    close = function(object) object.closed = true end }
                player:ClearBufferedAction()
            end
            ticket.dx, ticket.dz = dx, dz
            if controller.locomotor then
                controller.locomotor:RunInDirection(-math.atan2(dz, dx) * 180 / math.pi,
                    math.min(1, distance(position, point) / 2))
            else
                controller:RemoteDirectWalking(dx, dz)
            end
            return ticket
        elseif mode == "steer" then
            if controller.ismastersim then
                local user = player.components.steeringwheeluser
                if not user then return false, ctx:text("舵轮已不可用", "Steering wheel is no longer available") end
                user:SteerInDir(dx, dz)
            else G.SendRPCToServer(G.RPC.SteerBoat, dx, dz) end
            return nil
        end
        local actor = player:GetPosition()
        if controller.isclientcontrollerattached then
            -- Native controller rowing ignores the action point and rows radially from the actor.
            local rx, rz = actor.x - position.x, actor.z - position.z
            local radial = math.sqrt(rx * rx + rz * rz)
            if radial < radius - .5 or (rx * dx + rz * dz) / radial < .97 then
                if previous and previous.action.action ~= G.ACTIONS.WALKTO then self:stop(previous, "replaced") end
                local edge = { x = position.x + dx * (radius - .05), z = position.z + dz * (radius - .05) }
                local next_ticket, why = Actions.walk(ctx, edge, previous)
                return next_ticket or false, why
            end
        end
        if next_row and now < next_row or previous and not previous.closed and not previous.sent then return nil end
        local row_point = G.Vector3(actor.x + dx * (radius * 2 + 4), 0, actor.z + dz * (radius * 2 + 4))
        for _, action in ipairs(Actions.candidates(ctx, nil, "right", item, row_point)) do
            if action.action == G.ACTIONS.ROW or action.action == G.ACTIONS.ROW_CONTROLLER then
                if previous then self:stop(previous, "replaced") end
                local next_ticket, why = Actions.dispatch(ctx, action, "right")
                if next_ticket then next_row = now + .5 end
                return next_ticket or false, why
            end
        end
        return nil
    end
    return self
end

return M
