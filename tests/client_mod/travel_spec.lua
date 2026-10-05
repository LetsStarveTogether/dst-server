local T = require("tests.client_mod.support")
local Actions = require("fieldkit/game/actions")
local Travel = require("fieldkit/features/travel")
local Route = require("fieldkit/rules/route")

T.case("route_coordinate_contract", function()
    local grid, start, finish = { size = 4, x = 2, z = 2 }, { x = 0, z = 0 }, { x = 240, z = 0 }
    local known = function() return true end
    local path = assert(Route.plan(start, finish, nil, grid, known, known))
    assert(#path == 10 and path[1].x == 24 and path[10].x == 240)
    assert(path[10] ~= finish and getmetatable(path[1]) == nil and path[1].GetPosition == nil,
        "coordinate planning returns fresh plain points; the adapter owns platform capture")
    local result, reason = Route.plan(start, finish, nil, grid, function() return false end,
        function() error("a blocked segment must stop before visibility checks") end)
    assert(result == nil and reason == "blocked_path")
    result, reason = Route.plan(start, finish, { { x = 0 / 0, z = 0 } }, grid, known, known)
    assert(result == nil and reason == "invalid_path")
    result, reason = Route.plan(start, { x = 100000, z = 0 }, nil, grid, known, known)
    assert(result == nil and reason == "complex_path", "visibility traversal has a finite budget")
    local steps = {}
    for i = 1, 2048 do steps[i] = { x = i, z = 0 } end
    result, reason = Route.plan(start, finish, steps, grid, known, known)
    assert(result == nil and reason == "complex_path", "native waypoint input has a finite budget")
    for _, sign in ipairs({ -1, 1 }) do
        local touched = {}
        assert(Route.plan({ x = 2, z = 2 }, { x = 2 + 8 * sign, z = 2 + 8 * sign }, nil, grid, known,
            function(point) touched[point.x .. ":" .. point.z] = true; return true end))
        assert(touched[(2 + 4 * sign) .. ":2"] and touched["2:" .. (2 + 4 * sign)],
            "both adjacent cells are checked at diagonal crossings in either direction")
    end
end)

local function physics(radius, mass, group, mask)
    return {
        IsActive = function() return true end,
        GetRadius = function() return radius or .5 end,
        GetMass = function() return mass or 75 end,
        GetCollisionGroup = function() return group or 1 end,
        GetCollisionMask = function() return mask or 131 end,
    }
end

local function target(x, z, tags, body)
    return T.entity({ x = x, z = z or 0, visible = true, tags = tags or { player = true }, Physics = body or physics() })
end

local function context(mode)
    local player, rpc = T.entity({ Physics = physics() }), {}
    local point = player
    function player:CanSeePointOnMiniMap() return true end
    local controller = { ismastersim = mode == "host", actions = {} }
    if mode ~= "remote" then
        controller.locomotor = {
            Stop = function(self) self.stopped = true end,
            Clear = function(self) self.bufferedaction = nil end,
        }
    end
    function controller:IsEnabled() return true end
    function controller:IsBusy() return false end
    function controller:GetPlatformRelativePosition(x, z) return "boat", x - 2, z - 3 end
    function controller:DoAction(action)
        self.actions[#self.actions + 1] = action
        player.bufferedaction = action
        if self.locomotor then self.locomotor.bufferedaction = action
        else action.non_preview_cb() end
    end
    function controller:ClearActionHold() self.cleared = true end
    function controller:RemoteStopWalking() self.stopped = true end
    player.components.playercontroller = controller
    local G = {
        EQUIPSLOTS = { HANDS = "hands" },
        COLLISION = { LAND_OCEAN_LIMITS = 128 },
        RPC = { LeftClick = "left", RightClick = "right", SetMovementPredictionEnabled = "prediction" },
        ACTIONS = { WALKTO = { id = "WALKTO", code = 1 }, EQUIP = { id = "EQUIP" }, UNEQUIP = { id = "UNEQUIP" } },
        TILE_SCALE = 4,
        Vector3 = T.Vector3,
        CanEntitySeePoint = function() return true end,
        CanEntitySeeTarget = function(_, target) return target.visible ~= false end,
        TheWorld = {
            Map = { IsPassableAtPoint = function() return true end, GetPlatformAtPoint = function() end,
                GetTileCenterPoint = function(_, x, _, z) return math.floor(x / 4) * 4 + 2, 0, math.floor(z / 4) * 4 + 2 end },
            Pathfinder = { IsClear = function() return true end },
        },
        bit = { band = function(a, b)
            local result, bit_value = 0, 1
            while a > 0 and b > 0 do
                if a % 2 == 1 and b % 2 == 1 then result = result + bit_value end
                a, b, bit_value = math.floor(a / 2), math.floor(b / 2), bit_value * 2
            end
            return result
        end },
        SendRPCToServer = function(...) rpc[#rpc + 1] = { ... } end,
    }
    local position_mt = { __eq = function(a, b)
        return a.walkable_platform == b.walkable_platform and a.x == b.x and a.z == b.z
    end }
    function G.DynamicPosition(position)
        local boat = G.TheWorld.Map:GetPlatformAtPoint(position.x, position.z)
        local x, z = position.x - (boat and boat.x or 0), position.z - (boat and boat.z or 0)
        return setmetatable({ walkable_platform = boat, x = x, z = z, GetPosition = function()
            if boat and not boat:IsValid() then return nil end
            return { x = x + (boat and boat.x or 0), z = z + (boat and boat.z or 0) }
        end }, position_mt)
    end
    function G.BufferedAction(doer, target, action, item, position)
        local captured = position and G.DynamicPosition(position)
        return {
            doer = doer, target = target, action = action, invobject = item, rotation = 0,
            pos = captured,
            IsValid = function() return true end,
            GetActionPoint = function() return captured and captured:GetPosition() end,
            AddSuccessAction = function(self, fn) self.success = fn end,
            AddFailAction = function(self, fn) self.failure = fn end,
        }
    end
    local ctx = { G = G, player = player, now = 0, config = { travel = true }, params = require("fieldkit/core/parameters").new() }
    G.GetTime = function() return ctx.now end
    -- These cases exercise the native navigation adapter; weighted planning has its own cases below.
    ctx.params.travel_fast, ctx.params.travel_avoid = false, false
    ctx.params.follow_distance = 3
    function ctx:text(_, en) return en end
    function ctx:notify(message) self.message = message end
    function ctx:ready() return true end
    ctx.executor = { start = function(object, job) ctx.job, object.current = job, job; return true end }
    local inv = { slots = {}, calls = 0 }
    player.replica.inventory = inv
    inv.classified = { IsBusy = function() return inv.busy end }
    function inv:IsOpenedBy() return true end
    function inv:GetActiveItem() return self.active end
    function inv:GetEquippedItem() return self.hands end
    function inv:GetEquips() return self.hands and { hands = self.hands } or {} end
    function inv:GetNumSlots() return 4 end
    function inv:GetItemInSlot(slot) return self.slots[slot] end
    function inv:GetOverflowContainer() return self.backpack end
    function inv:IsHeavyLifting() return self.heavy end
    function inv:CanTakeItemInSlot() return true end
    function inv:AcceptsStacks() return true end
    function inv:UseItemFromInvTile(item)
        assert(ctx._queue_query and not ctx.equip_pending)
        self.calls, ctx.equip_pending, self.busy = self.calls + 1, item, true
    end
    player.components.playeractionpicker = { GetInventoryActions = function(_, item)
        return { { action = inv.hands == item and G.ACTIONS.UNEQUIP or G.ACTIONS.EQUIP } }
    end }
    function ctx:item(prefab, speed)
        local item = { prefab = prefab, replica = {}, tags = {} }
        function item:IsValid() return not self.removed end
        function item:HasTag(tag) return self.tags[tag] == true end
        item.replica.inventoryitem = { GetWalkSpeedMult = function() return speed or 1 end,
            IsLockedInSlot = function() return item.locked end }
        item.replica.equippable = { EquipSlot = function() return "hands" end,
            ShouldPreventUnequipping = function() return item.prevent end, IsRestricted = function() return item.restricted end }
        return item
    end
    function ctx:deliver_equip()
        local item, old = self.equip_pending, inv.hands
        self.equip_pending, inv.busy = nil, false
        if not item then return end
        if old == item then inv.hands = nil
        else
            for slot, value in pairs(inv.slots) do if value == item then inv.slots[slot] = nil end end
            inv.hands = item
        end
        if old then for slot = 1, inv:GetNumSlots() do if not inv.slots[slot] then inv.slots[slot] = old; break end end end
    end
    function ctx:step(now) self.now = now; return self.job:step(now) end
    ctx.equipment = require("fieldkit/game/equipment").new(ctx)
    return ctx, point, rpc
end

T.case("navigation_admission", function()
    local ctx = context()
    local travel = Travel.new(ctx)
    local ok, reason = travel:add_waypoint({ x = 10, z = 0 })
    assert(ok and reason == "Waypoint added: 1" and ctx.message == nil)
    ctx.executor.start = function() return false, "Executor admission rejected" end
    ok, reason = travel:navigate()
    assert(not ok and reason == "Executor admission rejected" and ctx.message == nil)
    assert(#travel:route_snapshot().points == 1, "rejected navigation keeps the recorded waypoints")
    ctx.ready = function() return false, "Player is unavailable" end
    ok, reason = travel:walk({ x = 10, z = 0 })
    assert(not ok and reason == "Player is unavailable" and ctx.message == nil)
end)

T.each("native_player_navigation_capabilities", { "host", "predict", "remote" }, function(mode)
    local ctx = context(mode)
    local controller = ctx.player.components.playercontroller
    local locomotor = controller.locomotor or {}
    for _, method in ipairs({ "SetSlowMultiplier", "SetFasterOnCreep", "SetTriggersCreep", "EnableHopDelay" }) do
        locomotor[method] = function() end
    end
    function locomotor:SetAllowPlatformHopping(value) self.allow_platform_hopping = value end
    local configure = T.eval(assert(T.source("prefabs/player_common_extensions")
        :match("local function ConfigurePlayerLocomotor%b().-\nend"))
        .. "\nreturn ConfigurePlayerLocomotor", { TUNING = { WILSON_WALK_SPEED = 4, WILSON_RUN_SPEED = 6 } })
    ctx.player.components.locomotor = locomotor
    configure(ctx.player)
    if mode == "remote" then ctx.player.components.locomotor = nil end
    local checked = 0
    function ctx.G.TheWorld.Pathfinder:IsClear(_, _, _, _, _, _, caps)
        assert(caps.player == locomotor.pathcaps.player and caps.ignorecreep == locomotor.pathcaps.ignorecreep
            and caps.allowplatformhopping == locomotor.allow_platform_hopping,
            "navigation must preserve native player capabilities when prediction removes the local locomotor")
        checked = checked + 1
        return true
    end
    assert(Travel.new(ctx):walk({ x = 12, z = 0 }))
    assert(ctx.job:step(0) == "running" and checked > 0 and #controller.actions == 1)
    ctx.job:cancel("cancelled")
end)

T.each("native_dispatch", { "host", "predict", "remote" }, function(mode)
    local ctx, _, rpc = context(mode)
    local ticket = assert(Actions.walk(ctx, { x = 10, z = 12 }))
    assert(#rpc == (mode == "remote" and 1 or 0))
    if mode == "remote" then
        assert(rpc[1][3] == 8 and rpc[1][4] == 9 and rpc[1][6] == true)
        assert(rpc[1][10] == "boat" and rpc[1][11] == true)
    end
    local sent = #rpc
    Actions.stop(ctx, ticket, "manual")
    assert(#rpc == sent and T.listener_count(ctx.player) == 0)
    assert(ctx.player:GetBufferedAction() == nil, "the exact owned buffer can be cleared")
    if ticket.action.non_preview_cb then ticket.action.non_preview_cb() end
    assert(#rpc == sent, "closed delayed callback must not send")
end)

T.each("newer_action_ownership", { "host", "predict", "remote" }, function(mode)
    local ctx, _, rpc = context(mode)
    local ticket = assert(Actions.walk(ctx, { x = 10, z = 0 }))
    local user_action = {}
    ctx.player.bufferedaction = user_action
    local locomotor = ctx.player.components.playercontroller.locomotor
    if locomotor then locomotor.bufferedaction = user_action end
    local sent = #rpc
    Actions.stop(ctx, ticket, "manual")
    assert(ctx.player:GetBufferedAction() == user_action and #rpc == sent)
    if locomotor then assert(locomotor.bufferedaction == user_action) end
end)

T.each("split_buffer_ownership", { "player", "locomotor" }, function(slot)
    local ctx, _, rpc = context("predict")
    local ticket = assert(Actions.walk(ctx, { x = 10, z = 0 }))
    local newer = {}
    local locomotor = ctx.player.components.playercontroller.locomotor
    if slot == "player" then ctx.player.bufferedaction = newer else locomotor.bufferedaction = newer end
    Actions.stop(ctx, ticket, "manual")
    if slot == "player" then
        assert(ctx.player.bufferedaction == newer and locomotor.bufferedaction == nil)
    else
        assert(ctx.player.bufferedaction == nil and locomotor.bufferedaction == newer)
    end
    assert(#rpc == 0)
end)

T.case("remote_cancel_resets_server_route", function()
    local ctx, _, rpc = context("remote")
    local route = { bufferedaction = {}, Stop = function(self) self.stopped = true end,
        Clear = function(self) self.bufferedaction = nil end, SetAllowPlatformHopping = function() end }
    local server = { ismastersim = true, remote_predicting = false, locomotor = route, ResetRemoteController = function() end }
    local reset = T.eval(assert(T.source("components/playercontroller")
        :match("function PlayerController:OnRemoteToggleMovementPrediction%b().-\nend"))
        .. "\nreturn PlayerController.OnRemoteToggleMovementPrediction", { PlayerController = {} })
    local send = ctx.G.SendRPCToServer
    ctx.G.SendRPCToServer = function(kind, value, ...)
        send(kind, value, ...)
        if kind == ctx.G.RPC.SetMovementPredictionEnabled then reset(server, value) end
    end
    local ticket = assert(Actions.walk(ctx, { x = 10, z = 0 }))
    Actions.stop(ctx, ticket, "cancelled")
    assert(#rpc == 2 and route.stopped and route.bufferedaction == nil and not server.remote_predicting,
        "cancelling clears the server route through its existing movement mode")
end)

T.case("predictive_ticket_receipts", function()
    local ctx, _, rpc = context("predict")
    local action = ctx.G.BufferedAction(ctx.player, nil, { id = "PICK", code = 5 }, nil, { x = 8, z = 10 })
    local ticket = assert(Actions.dispatch(ctx, action, "right"))
    assert(#rpc == 0 and not ticket.sent)
    action.preview_cb()
    assert(#rpc == 1 and rpc[1][1] == "right" and rpc[1][7] == true)
    assert(rpc[1][11] == "boat" and rpc[1][12] == true)
    assert(not ticket.succeeded)
    ctx.player:PushEvent("performaction", { action = {} }); ctx.player:PushEvent("actionfailed", { action = {} })
    assert(not ticket.attempted and not ticket.failed, "foreign action events cannot mutate a movement ticket")
    ctx.player:PushEvent("performaction", { action = action })
    assert(ticket.attempted and not ticket.succeeded)
    ticket:close()
    assert(T.listener_count(ctx.player) == 0)
    ctx.player.components.playercontroller.DoAction = function() error("native rejection") end
    assert(Actions.dispatch(ctx, action) == nil and T.listener_count(ctx.player) == 0)
end)

T.case("walk_progress_timeout", function()
    local ctx, position = context("host")
    assert(Travel.new(ctx):walk({ x = 10, z = 0 }))
    assert(ctx.job.input.mode == "world")
    assert(ctx.job:step(0) == "running")
    assert(ctx.job:step(9) == "failed", "stalled route must stop")
    ctx.job:cancel("failed")
    assert(ctx.player:GetBufferedAction() == nil)
    assert(Travel.new(ctx):walk({ x = 10, z = 0 }))
    ctx.job:step(10)
    position.x = 10
    assert(ctx.job:step(11) == "done")
    assert(not Travel.new(ctx):walk({ x = math.huge, z = 0 }))
end)

T.case("hidden_leader_stops", function()
    local ctx = context("host")
    local leader = target(10)
    assert(Travel.new(ctx):follow(leader))
    assert(ctx.job.timeout == 600 and ctx.job:step(0) == "running")
    leader.visible = false
    assert(ctx.job:step(1) == "failed")
end)

T.case("busy_animation_deadline", function()
    local ctx = context("host")
    local controller = ctx.player.components.playercontroller
    controller.IsBusy = function(self) return self.busy end
    controller.busy = true
    assert(Travel.new(ctx):follow(target(10)))
    assert(ctx.job:step(0) == "running" and #controller.actions == 0)
    controller.busy = false
    assert(ctx.job:step(.5) == "running" and #controller.actions == 1,
        "finishing an earlier native animation delays movement without failing the new task")
    controller.busy = true
    assert(ctx.job:step(9) == "failed", "waiting for native readiness remains bounded")
end)

T.case("follow_collision_gap_and_hysteresis", function()
    local ctx, position = context("host")
    ctx.params.follow_distance, ctx.params.follow_keep_distance = 1.5, true
    local leader = target(10, 0, { locomotor = true }, physics(2))
    leader.GetPhysicsRadius = function() return 100 end
    local travel = Travel.new(ctx)
    assert(travel:can_follow(leader) and not travel:can_follow(target(3, 0, {})))
    assert(travel:follow(leader, "direct"))
    ctx.params.follow_distance, ctx.params.follow_keep_distance, ctx.params.search_radius = 3.5, false, 1
    assert(ctx.job:step(0) == "running")
    assert(ctx.player.bufferedaction:GetActionPoint().x == 6,
        "running follow retains its original 1.5 gap, search radius and retreat permission")
    position.x = 5.8
    ctx.job:step(1)
    assert(ctx.player.bufferedaction == nil, "native fast movement may arrive .2 before the selected point")
    leader.x = 10.2
    ctx.job:step(2)
    assert(ctx.player.bufferedaction == nil, "small target movements stay inside wait hysteresis")
    leader.x = 7
    ctx.job:step(3)
    assert(ctx.player.bufferedaction:GetActionPoint().x == 3, "keep-distance retreats from an approaching target")
end)

T.case("rejected_replacement_ownership", function()
    local ctx = context("host")
    local first = assert(Actions.walk(ctx, { x = 10, z = 0 }))
    assert(Actions.walk(ctx, { x = 10, z = 0 }, first) == first)
    assert(#ctx.player.components.playercontroller.actions == 1)
    ctx.player.components.playercontroller.DoAction = function() end
    assert(Actions.walk(ctx, { x = 11, z = 0 }, first) == nil)
    assert(not first.closed)
    Actions.stop(ctx, first, "cancelled")
    assert(ctx.player:GetBufferedAction() == nil)
end)

T.case("failed_follow_replacement", function()
    local ctx = context("host")
    local leader = target(10)
    assert(Travel.new(ctx):follow(leader, "direct"))
    ctx.job:step(0)
    local original = ctx.player.bufferedaction
    ctx.player.components.playercontroller.DoAction = function() end
    leader.x = 11
    assert(ctx.job:step(.5) == "failed")
    assert(ctx.player.bufferedaction == original)
    ctx.job:cancel("failed")
    assert(ctx.player.bufferedaction == nil)
end)

T.case("trail_turns", function()
    local ctx, position = context("host")
    local leader = target(6)
    assert(Travel.new(ctx):follow(leader))
    ctx.job:step(0)
    leader.z = 5
    ctx.job:step(.5)
    leader.x = 12
    ctx.job:step(1)
    assert(ctx.player.bufferedaction:GetActionPoint().x == 6 and ctx.player.bufferedaction:GetActionPoint().z == 0)
    position.x = 6
    ctx.job:step(1.5)
    assert(ctx.player.bufferedaction:GetActionPoint().x == 6 and ctx.player.bufferedaction:GetActionPoint().z == 5)
    position.z = 5
    ctx.job:step(2)
    assert(ctx.player.bufferedaction:GetActionPoint().x == 12)
end)

T.case("trail_arrival_throttle", function()
    local ctx, position = context("host")
    local leader = target(8)
    assert(Travel.new(ctx):follow(leader))
    for i = 0, 200 do
        leader.x = 8 + i * .6
        assert(ctx.job:step(i * .1) == "running", "a 6m/s leader must remain within follow range")
        local action = ctx.player.bufferedaction
        if action then
            local goal = action:GetActionPoint()
            position.x = math.min(goal.x, position.x + .6)
            if math.abs(position.x - goal.x) <= .15 then
                action.success()
                ctx.player.bufferedaction = nil
                ctx.player.components.playercontroller.locomotor.bufferedaction = nil
            end
        end
        assert(leader.x - position.x <= 8.6, "waypoint stops must not reduce sustained follow speed")
    end
end)

T.case("moving_leader_progress", function()
    local ctx, position = context("host")
    assert(Travel.new(ctx):walk({ x = 10, z = 0 }))
    ctx.job:step(0)
    position.x, position.z = 10, 10
    assert(ctx.job:step(4) == "running")
    position.x, position.z = 20, 0
    assert(ctx.job:step(8) == "failed")
    position.x, position.z = 0, 0
    local leader = target(10)
    assert(Travel.new(ctx):follow(leader, "direct"))
    ctx.job:step(10)
    leader.x, position.x = 14, 4
    assert(ctx.job:step(18) == "running", "advancing toward the previous goal counts despite an unchanged gap")
    ctx.job:step(19)
    assert(ctx.job:step(26) == "failed", "a stationary follower still times out")
end)

T.case("circling_is_not_progress", function()
    local ctx, position = context("host")
    position.x = 10
    local leader = target(0)
    assert(Travel.new(ctx):follow(leader, "direct"))
    ctx.job:step(0)
    position.x, position.z = 0, 10
    assert(ctx.job:step(4) == "running")
    position.x, position.z = -10, 0
    assert(ctx.job:step(8) == "failed", "circling the target is not progress toward the recorded goal")
end)

T.case("boat_drift_is_not_progress", function()
    local ctx, position = context("host")
    local boat = { x = 0, z = 0, IsValid = function() return true end }
    ctx.G.TheWorld.Map.GetPlatformAtPoint = function() return boat end
    ctx.player.platform = boat
    local leader = target(10)
    leader.platform = boat
    assert(Travel.new(ctx):follow(leader, "direct"))
    ctx.job:step(0)
    boat.x, position.x, leader.x = 5, 5, 15
    assert(ctx.job:step(8) == "failed", "same-boat drift does not count as advancing toward the previous goal")
end)

T.each("trail_limits", { "wall", "teleport", "points", "length" }, function(kind)
    local ctx = context("host")
    ctx.params.search_radius = 80
    local leader = target(12)
    assert(Travel.new(ctx):follow(leader))
    ctx.job:step(0)
    if kind == "wall" then
        ctx.G.TheWorld.Pathfinder.IsClear = function() return false end
        leader.x = 13
        assert(ctx.job:step(.1) == "failed")
    elseif kind == "teleport" then
        leader.x = 21
        assert(ctx.job:step(.1) == "failed")
    else
        local failed, count = false, kind == "points" and 129 or 99
        for i = 1, count do
            leader.z = i % 2 == 0 and 0 or (kind == "points" and .5 or 1)
            if ctx.job:step(i / 100) == "failed" then failed = true; break end
        end
        assert(failed, kind .. " bound was not enforced")
    end
end)

T.case("moving_platform_coordinates", function()
    local ctx, position = context("host")
    local boat = { x = 0, z = 0, valid = true, IsValid = function(self) return self.valid end }
    ctx.G.TheWorld.Map.GetPlatformAtPoint = function() return boat end
    ctx.player.platform = boat
    assert(Travel.new(ctx):walk({ x = 10, z = 0 }))
    ctx.job:step(0)
    boat.x, position.x = 4, 4
    assert(ctx.job:step(8) == "failed", "being carried by the same boat is not progress")
    assert(Travel.new(ctx):walk({ x = 14, z = 0 }))
    ctx.job:step(10)
    boat.valid = false
    assert(ctx.job:step(11) == "failed")
end)

T.case("recorded_platform_landing", function()
    local ctx, position = context("host")
    local boat = { x = 0, z = 0, IsValid = function() return true end }
    ctx.G.TheWorld.Map.GetPlatformAtPoint = function(_, x) return x >= 9 and boat or nil end
    ctx.G.TheWorld.Map.IsPassableAtPoint = function(_, x) return x <= 6 or x >= 9 end
    local leader = target(6)
    assert(Travel.new(ctx):follow(leader))
    ctx.job:step(0)
    leader.x, leader.tags.boathopping = 8, true
    ctx.job:step(.5)
    assert(ctx.player.bufferedaction:GetActionPoint().x == 6)
    leader.x, leader.tags.boathopping, leader.platform = 10, nil, boat
    ctx.job:step(1)
    position.x = 6
    ctx.job:step(1.5)
    assert(ctx.player.bufferedaction:GetActionPoint().x == 10)
end)

T.case("follow_stays_on_platform", function()
    local ctx = context("host")
    local boat = { x = 0, z = 0, IsValid = function() return true end }
    ctx.G.TheWorld.Map.GetPlatformAtPoint = function(_, x) return x >= 9 and boat or nil end
    local leader = target(10)
    leader.platform = boat
    assert(Travel.new(ctx):follow(leader, "direct"))
    assert(ctx.job:step(0) == "running")
    assert(ctx.player.bufferedaction:GetActionPoint().x >= 9, "the goal must be on the leader's boat")
end)

T.case("follow_avoids_wall", function()
    local ctx = context("host")
    ctx.G.TheWorld.Pathfinder.IsClear = function(_, _, _, _, x) return x >= 9 end
    assert(Travel.new(ctx):follow(target(10), "direct"))
    assert(ctx.job:step(0) == "running")
    assert(ctx.player.bufferedaction:GetActionPoint().x >= 9, "the first terrain-passable point is behind a wall")
end)

T.case("push_uses_native_walk", function()
    local ctx, position = context("host")
    local travel, creature = Travel.new(ctx), target(2, 0, { locomotor = true })
    assert(travel:can_push(creature) and travel:push(creature))
    assert(ctx.job.timeout == 30 and ctx.job:step(0) == "running")
    assert(ctx.player.bufferedaction.target == nil and ctx.player.bufferedaction:GetActionPoint().x == 2)
    position.x = 1
    ctx.job:step(1)
    creature.x, position.x = 2.5, 1.5
    assert(ctx.job:step(8) == "running", "observed displacement at contact resets the push watchdog")
    assert(ctx.job:step(16) == "failed", "contact alone does not prove a successful push")
    assert(not travel:can_push(target(2, 0, {}, physics(.5, 0))))
    assert(not travel:can_push(target(2, 0, {}, physics(.5, 75, 4, 4))))
    creature.platform = {}
    assert(not travel:can_push(creature), "do not push between platforms")
end)


T.case("long_map_route", function()
    local ctx, position = context("host")
    ctx.G.CanEntitySeePoint = function(_, x) return x < 40 end
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 240, z = 0 }))
    local finished, maximum = false, 0
    for step = 0, 30 do
        local state = ctx.job:step(step * .1)
        if state == "done" then finished = true; break end
        assert(state == "running")
        local action = assert(ctx.player.bufferedaction)
        local goal = action:GetActionPoint()
        maximum = math.max(maximum, math.abs(goal.x - position.x))
        position.x, position.z = goal.x, goal.z
        action.success()
    end
    assert(finished and maximum <= 24 and position.x == 240, "long route is segmented into native movement requests")
    ctx.job:cancel("done")
    assert(travel:route_status() == "")
end)

local function pathfinder(ctx, result)
    local state = { status = 0, killed = 0, submitted = 0, result = result }
    local finder = ctx.G.TheWorld.Pathfinder
    finder.IsClear = function(_, x, _, z, gx, _, gz)
        return not (z == 0 and gz == 0 and math.abs(gx - x) > 30)
    end
    finder.SubmitSearch = function(_, x, _, z, gx, _, gz, caps)
        state.submitted = state.submitted + 1
        state.from, state.to, state.caps = { x = x, z = z }, { x = gx, z = gz }, caps
        if state.reject then return nil end
        return 41
    end
    finder.GetSearchStatus = function(_, handle) assert(handle == 41); return state.status end
    finder.GetSearchResult = function() return state.result end
    finder.KillSearch = function(_, handle) assert(handle == 41); state.killed = state.killed + 1 end
    return state
end

T.case("asynchronous_search_ownership", function()
    local ctx, position = context("host")
    local state = pathfinder(ctx, { steps = { { x = 0, z = 0 }, { x = 0, z = 20 }, { x = 100, z = 20 }, { x = 100, z = 0 } } })
    assert(Travel.new(ctx):walk({ x = 100, z = 0 }))
    assert(ctx.job:step(0) == "running" and state.submitted == 1)
    assert(ctx.player.bufferedaction == nil)
    assert(ctx.job:step(1) == "running" and state.submitted == 1)
    state.status = 1
    local done
    for n = 2, 30 do
        local status = ctx.job:step(n * .2)
        if status == "done" then done = true; break end
        assert(status == "running")
        local action = ctx.player.bufferedaction
        if action then
            local goal = action:GetActionPoint()
            position.x, position.z = goal.x, goal.z
            action.success()
        end
    end
    assert(done and state.killed == 1 and position.x == 100 and position.z == 0)
    ctx.job:cancel("done")
    assert(state.killed == 1, "completed handle is released exactly once")
end)
T.each("search_failure", { "cancel", "timeout", "no_path", "lost", "rejected" }, function(kind)
    local ctx = context("host")
    local state = pathfinder(ctx)
    if kind == "rejected" then state.reject = true end
    assert(Travel.new(ctx):walk({ x = 100, z = 0 }))
    local first = ctx.job:step(0)
    if kind == "cancel" then ctx.job:cancel("manual")
    elseif kind == "timeout" then assert(ctx.job:step(11) == "running")
    elseif kind == "no_path" then state.status = 2; assert(ctx.job:step(.1) == "running")
    elseif kind == "lost" then state.status = nil; assert(ctx.job:step(.1) == "running")
    else assert(first == "running") end
    assert(ctx.player.bufferedaction == nil)
    assert(state.killed == (kind == "rejected" and 0 or 1), "search cleanup before grid fallback on " .. kind)
    ctx.job:cancel("manual")
end)

T.case("unknown_route_connector", function()
    local ctx = context("host")
    ctx.player.CanSeePointOnMiniMap = function(_, x) return x <= 8 or x >= 12 end
    assert(Travel.new(ctx):walk({ x = 20, z = 0 }))
    assert(ctx.job:step(0) == "running" and not ctx.player.bufferedaction)
    local state
    for i = 1, 600 do
        state = ctx:step(i * .1)
        assert(not ctx.player.bufferedaction, "a fog barrier cannot be crossed by the fallback")
        if state ~= "running" then break end
    end
    assert(state == "failed", "an unbounded fog barrier exhausts the finite search budget")
end)
T.case("diagonal_fog_boundary", function()
    local ctx = context("host")
    ctx.player.CanSeePointOnMiniMap = function(_, x, _, z) return not (x >= 4 and x < 8 and z >= 0 and z < 4) end
    assert(Travel.new(ctx):walk({ x = 8, z = 8 }))
    assert(ctx.job:step(0) == "running" and not ctx.player.bufferedaction)
    for i = 1, 100 do
        assert(ctx:step(i * .1) == "running")
        if ctx.player.bufferedaction then break end
    end
    local goal = assert(ctx.player.bufferedaction):GetActionPoint()
    assert(goal.x ~= 8 or goal.z ~= 8, "grid search finds a detour around the unknown diagonal corner")
end)
T.case("native_path_final_connector", function()
    local ctx = context("host")
    local state = pathfinder(ctx, { steps = { { x = 0, z = 0 }, { x = 0, z = 20 }, { x = 100, z = 20 } } })
    ctx.player.CanSeePointOnMiniMap = function(_, x, _, z) return not (x >= 96 and z >= 4 and z < 16) end
    assert(Travel.new(ctx):walk({ x = 100, z = 0 }))
    ctx.job:step(0)
    state.status = 1
    assert(ctx.job:step(1) == "running" and state.killed == 1 and not ctx.player.bufferedaction,
        "a native path crossing fog is discarded before the grid fallback")
end)

T.case("waypoint_order_and_limit", function()
    local ctx, position = context("host")
    local travel = Travel.new(ctx)
    assert(travel:add_waypoint({ x = 10, z = 0 }))
    assert(travel:navigate({ x = 10, z = 10 }, true))
    assert(travel:route_status():match("2") and not ctx.job)
    assert(travel:navigate({ x = 20, z = 10 }, false))
    assert(ctx.job:step(0) == "running")
    assert(ctx.player.bufferedaction:GetActionPoint().x == 10 and ctx.player.bufferedaction:GetActionPoint().z == 0)
    position.x = 10
    assert(ctx.job:step(.1) == "running")
    assert(ctx.player.bufferedaction:GetActionPoint().z == 10)
    position.z = 10
    assert(ctx.job:step(.2) == "running")
    assert(ctx.player.bufferedaction:GetActionPoint().x == 20)
    position.x = 20
    assert(ctx.job:step(.3) == "done")
    ctx.job:cancel("done")
    for _ = 1, 64 do assert(travel:add_waypoint({ x = 0, z = 0 })) end
    assert(not travel:add_waypoint({ x = 0, z = 0 }))
    travel:clear_waypoints()
    assert(travel:route_status() == "" and not travel:navigate(nil, false))
end)

T.case("captured_search_world", function()
    local ctx = context("host")
    local old_world = pathfinder(ctx)
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 100, z = 0 }))
    ctx.job:step(0)
    ctx.G.TheWorld = { Pathfinder = { KillSearch = function() error("must not touch another world's search handles") end } }
    ctx.job:cancel("invalid")
    assert(old_world.killed == 1, "search cancellation belongs to the captured world")
end)

T.case("live_route_edit", function()
    local ctx, position = context("host")
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 10, z = 0 }))
    local job = ctx.job
    assert(job:step(0) == "running")
    local first_action = ctx.player.bufferedaction
    assert(travel:add_waypoint({ x = 20, z = 0 }))
    assert(ctx.job == job and ctx.player.bufferedaction == first_action,
        "appending does not restart movement or replace the active destination")
    local snapshot = travel:route_snapshot()
    assert(snapshot.active and snapshot.phase == "walking" and #snapshot.points == 2)
    assert(snapshot.origin.x == 0 and #snapshot.path == 1 and snapshot.path[1].x == 10)
    snapshot.points[1].x = -999
    snapshot.origin.x, snapshot.path[1].x = -999, -999
    assert(travel:route_snapshot().points[1].x == 10, "rendering cannot mutate the route")
    assert(travel:route_snapshot().origin.x == 0 and travel:route_snapshot().path[1].x == 10,
        "drawn paths and player origins are read-only snapshots too")
    position.x = 10
    assert(job:step(.1) == "running" and ctx.player.bufferedaction:GetActionPoint().x == 20)
    position.x = 20
    assert(travel:add_waypoint({ x = 30, z = 0 }), "append at the tail before its next tick stays in the same job")
    assert(job:step(.2) == "running" and ctx.player.bufferedaction:GetActionPoint().x == 30)
    position.x = 30
    assert(job:step(.3) == "done")
    job:cancel("done")
    assert(travel:add_waypoint({ x = 40, z = 0 }))
    snapshot = travel:route_snapshot()
    assert(not snapshot.active and snapshot.phase == "planned" and #snapshot.points == 1,
        "a completed job cannot receive later draft points")
end)

T.case("append_during_search", function()
    local ctx, position = context("host")
    local travel = Travel.new(ctx)
    local state = pathfinder(ctx, { steps = { { x = 0, z = 20 }, { x = 100, z = 20 }, { x = 100, z = 0 } } })
    assert(travel:walk({ x = 100, z = 0 }))
    local job = ctx.job
    assert(job:step(0) == "running" and state.submitted == 1)
    assert(travel:add_waypoint({ x = 110, z = 0 }))
    assert(job:step(.1) == "running" and state.submitted == 1 and state.killed == 0,
        "appending during native search retains the handle for its existing destination")
    state.status = 1
    for n = 1, 30 do
        local status = job:step(n * .2)
        if status == "done" then break end
        assert(status == "running")
        local action = ctx.player.bufferedaction
        if action then
            local goal = action:GetActionPoint()
            position.x, position.z = goal.x, goal.z
            action.success()
        end
    end
    assert(position.x == 110 and state.submitted == 1 and job:step(7) == "done")
    job:cancel("done")
end)

T.each("pause_editor_ownership", { "planning", "walking" }, function(phase)
    local ctx, position = context("predict")
    local travel, editor = Travel.new(ctx), {}
    local state = phase == "planning" and pathfinder(ctx) or nil
    assert(travel:walk({ x = state and 100 or 10, z = 0 }))
    local job = ctx.job
    job:step(0)
    local old_action = ctx.player.bufferedaction
    assert(travel:pause_route(editor) and travel:is_editing(job, editor))
    assert(#travel:route_snapshot().path == 0 and travel:route_snapshot().paused,
        "an edit invalidates the old path; rendering must show pending connectors instead")
    assert(not travel:is_editing(job, {}) and not travel:pause_route({}), "another editor cannot steal route ownership")
    if state then assert(state.killed == 1) end
    assert(not ctx.player.bufferedaction)
    assert(travel:add_waypoint({ x = 20, z = 5 }))
    assert(job:step(20) == "running" and not ctx.player.bufferedaction, "paused editor never submits native movement")
    travel:resume_route({})
    assert(travel:is_editing(job), "unrelated cleanup cannot resume the route")
    travel:resume_route(editor)
    assert(not travel:is_editing(job) and job:step(21) == "running", "resume resets movement progress deadlines")
    if state then assert(state.submitted == 2 and state.killed == 1) end
    job:cancel("manual")
    travel:resume_route(editor)
    assert(not travel:pause_route(editor) and travel:route_snapshot().phase == "idle")
    if old_action and old_action.preview_cb then old_action.preview_cb() end
    local count = #ctx.player.components.playercontroller.actions
    assert(job:step(22) == "failed" and #ctx.player.components.playercontroller.actions == count,
        "cancelled job methods cannot resume or dispatch late actions")
    assert(travel:add_waypoint({ x = position.x + 5, z = 0 }))
    assert(not travel:route_snapshot().active and #travel:route_snapshot().points == 1)
end)

T.case("consumed_waypoints_release_capacity", function()
    local ctx, position = context("host")
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 1, z = 0 }))
    for i = 2, 64 do assert(travel:add_waypoint({ x = i, z = 0 })) end
    assert(not travel:add_waypoint({ x = 65, z = 0 }))
    ctx.job:step(0)
    position.x = 1
    ctx.job:step(.1)
    assert(travel:add_waypoint({ x = 65, z = 0 }), "consumed points release bounded queue capacity")
    assert(#travel:route_snapshot().points == 64 and travel:route_snapshot().completed == 1)
end)

T.case("edited_route_on_boat", function()
    local ctx, position = context("host")
    local boat = { x = 0, z = 0, valid = true, IsValid = function(self) return self.valid end }
    ctx.G.TheWorld.Map.GetPlatformAtPoint = function() return boat end
    ctx.player.platform = boat
    local travel, editor = Travel.new(ctx), {}
    assert(travel:walk({ x = 10, z = 0 }))
    ctx.job:step(0)
    assert(travel:pause_route(editor))
    assert(travel:add_waypoint({ x = 20, z = 0 }))
    boat.x, position.x = 5, 5
    local points = travel:route_snapshot().points
    assert(points[1].x == 15 and points[2].x == 25, "live and appended points retain platform coordinates")
    travel:resume_route(editor)
    assert(ctx.job:step(1) == "running" and ctx.player.bufferedaction:GetActionPoint().x == 15)
    assert(travel:route_snapshot().path[1].x == 15, "visible paths resolve current platform positions")
    boat.valid = false
    assert(travel:route_snapshot().path[1].invalid, "lost platforms break lines instead of retaining stale coordinates")
    assert(ctx.job:step(2) == "failed", "a platform lost during editing fails safely")
end)

T.case("executor_deadline_survives_edit", function()
    local ctx = context("host")
    local now, editor = 0, {}
    ctx.executor = require("fieldkit/core/executor").new(function() return now end,
        function() return true end, function() end, error)
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 10, z = 0 }))
    local job = ctx.executor.current
    ctx.executor:tick()
    now = 500
    assert(travel:pause_route(editor))
    assert(travel:add_waypoint({ x = 20, z = 0 }))
    ctx.executor:tick()
    assert(ctx.executor.current == job and job.deadline == 600)
    now = 600
    ctx.executor:tick()
    assert(not ctx.executor.current and not travel:route_snapshot().active)
    travel:resume_route(editor)
    assert(not ctx.executor.current and not travel:pause_route(editor))
end)

T.case("native_pointer_dispatch", function()
    local Input = require("fieldkit/game/input")
    local PointerInput = require("fieldkit/game/pointer_input")
    local Scope = require("fieldkit/core/scope")
    local Executor = require("fieldkit/core/executor")
    local ctx = context()
    local G, player = ctx.G, ctx.player
    local controller = player.components.playercontroller
    local keys, held, listeners, native_controls = {}, {}, {}, {}
    local hover, hudhit, active, aoe, selected, announced
    G.KEY_SHIFT, G.KEY_ALT, G.KEY_CTRL = 1, 2, 3
    G.MOUSEBUTTON_LEFT, G.MOUSEBUTTON_MIDDLE, G.MOUSEBUTTON_RIGHT = 1000, 1002, 1001
    G.CONTROL_PRIMARY, G.CONTROL_ACCEPT, G.CONTROL_SECONDARY, G.CONTROL_ACTION = 4, 5, 6, 7
    G.GetTime = function() return 0 end
    G.TheInput = {
        mouse_enabled = true,
        IsMouseDown = function(_, button) return held[button] == true end,
        IsKeyDown = function(_, key) return keys[key] == true end,
        IsControlPressed = function() return true end,
        ControllerAttached = function() return false end,
        GetWorldEntityUnderMouse = function() return hover end,
        GetHUDEntityUnderMouse = function() return hudhit end,
        AddMouseButtonHandler = function(_, fn)
            listeners[#listeners + 1] = fn
            return { Remove = function() end }
        end,
        oncontrol = { HandleEvent = function(_, event, control, down)
            if event == "oncontrol" then controller:OnControl(control, down) end
        end },
        onmousebutton = { HandleEvent = function(_, _, ...)
            for _, fn in ipairs(listeners) do fn(...) end
        end },
    }
    G.TheInputProxy = { GetLocalizedControl = function(_, _, control)
        if control == G.CONTROL_PRIMARY then return 1, 1, G.MOUSEBUTTON_LEFT end
        if control == G.CONTROL_SECONDARY then return 1, 1, G.MOUSEBUTTON_RIGHT end
        return 0, 0
    end }
    player.replica = { inventory = { GetActiveItem = function() return active end } }
    ctx.hud = { controls = {}, OnControl = function() return false end, OnMouseButton = function() return false end }
    G.TheFrontEnd = { screen = ctx.hud, GetActiveScreen = function(self) return self.screen end }
    function G.TheFrontEnd:OnControl(control, down)
        self.isprimary = control == G.CONTROL_PRIMARY
        local result = self.screen:OnControl(self.isprimary and G.CONTROL_ACCEPT or control, down)
        self.isprimary = false
        return result
    end
    function G.TheFrontEnd:OnMouseButton(...) return self.screen:OnMouseButton(...) end
    function controller:IsAOETargeting() return aoe end
    function controller:OnControl(control, down) native_controls[#native_controls + 1] = { control, down } end
    ctx.scope = Scope.new(player, error)
    ctx.selection = { handle_control = function(_, _, _, down)
        if down and keys[G.KEY_ALT] then selected = true; return true end
        return false
    end }
    ctx.executor = Executor.new(G.GetTime, function() return true end, function() end, error)
    ctx.travel = Travel.new(ctx)
    ctx.announcements = { target = function() announced = true; return true end }
    local env = setmetatable({ require = function() return {} end, ValidateLineNumber = function() end,
        Class = T.Class, IsNotConsole = function() return false end },
        { __index = function(_, key) return G[key] or _G[key] end })
    G.TheInputProxy.GetInputDeviceCount = function() return 0 end
    T.native("events", env)
    local NativeInput = T.native("input", env)
    env.TheInput = G.TheInput
    Input.attach(ctx)
    PointerInput.attach(ctx)
    local function control(down, id)
        NativeInput.OnControl(G.TheInput, id or G.CONTROL_PRIMARY, down)
    end
    local function raw(down)
        NativeInput.OnMouseButton(G.TheInput, G.MOUSEBUTTON_LEFT, down, 0, 0)
    end
    local function click(order)
        held[G.MOUSEBUTTON_LEFT] = true
        T.pointer(raw, control, true, order.down)
        local job = ctx.executor.current
        held[G.MOUSEBUTTON_LEFT] = false
        T.pointer(raw, control, false, order.up)
        return job
    end
    keys[G.KEY_SHIFT], hover = true, target(8)
    T.each("follow_dispatch", T.pointer_orders, function(order)
        local generation = ctx.executor.generation
        assert(click(order) and ctx.executor.generation == generation + 1 and #native_controls == 0,
            "either native dispatch order starts follow exactly once and consumes both releases")
    end)
    keys[G.KEY_CTRL] = true
    assert(click(T.pointer_orders[1]).timeout == 30, "Shift+Ctrl pushes through the existing bounded travel job")
    keys[G.KEY_CTRL] = nil
    hover = target(8, 0, { balloon = true })
    assert(click(T.pointer_orders[4]).timeout == 600 and ctx.travel:can_follow(hover), "visible loose balloons support following")
    hover.entity.IsVisible = function() return false end
    hover.GetPosition = function() error("hidden target coordinates must never be sampled") end
    assert(not ctx.travel:can_follow(hover))
    local count = #native_controls
    click(T.pointer_orders[1])
    assert(#native_controls == count + 2 and not ctx.executor.current, "hidden targets keep native click behavior")
    hover = target(8)
    for _, block in ipairs({
        function(value) hudhit = value end,
        function(value) active = value end,
        function(value) controller.placer = value end,
        function(value) controller.deployplacer = value end,
        function(value) aoe = value end,
        function(value) controller.draggingonground = value end,
    }) do
        block(true); count = #native_controls; click(T.pointer_orders[4]); block(nil)
        assert(#native_controls == count + 2 and not ctx.executor.current,
            "native HUD, held items, placement, AOE and dragging retain their complete gesture")
    end
    held[G.MOUSEBUTTON_LEFT] = true
    raw(true); control(true)
    assert(ctx.executor.current and not G.TheInput:IsControlPressed(G.CONTROL_PRIMARY))
    assert(G.TheInput:IsControlPressed(G.CONTROL_SECONDARY) and G.TheInput:IsControlPressed(G.CONTROL_ACCEPT),
        "owning the left mouse never masks unrelated right-click or keyboard accept polling")
    count = #native_controls
    control(true, G.CONTROL_SECONDARY); control(false, G.CONTROL_SECONDARY)
    assert(#native_controls == count + 2 and not ctx.executor.current,
        "WX shielding/screech secondary controls reach native handlers and cancel following")
    keys[G.KEY_SHIFT] = nil
    held[G.MOUSEBUTTON_LEFT] = false
    raw(false); control(false)
    assert(#native_controls == count + 2, "releasing Shift early cannot leak the owned left release")
    keys[G.KEY_ALT] = true
    click(T.pointer_orders[4])
    assert(selected and not ctx.executor.current, "ordinary Alt selection retains its controller entry")
    keys[G.KEY_SHIFT], ctx.config.announcements = true, true
    selected = nil
    click(T.pointer_orders[1])
    assert(announced and not selected and not ctx.executor.current, "Alt+Shift announcement wins over following and selection")
    ctx.scope:close()
end)

T.each("movement_mode", { "host", "predict", "remote" }, function(mode)
    T.each("cane_restore", { false, true }, function(original)
        local ctx, point = context(mode)
        local inv = ctx.player.replica.inventory
        local cane, axe = ctx:item("cane", 1.25), original and ctx:item("axe") or nil
        inv.slots[1], inv.hands, ctx.params.travel_cane = cane, axe, true
        local travel = Travel.new(ctx)
        assert(travel:walk({ x = 10, z = 0 }))
        ctx.params.travel_cane = false
        assert(ctx:step(0) == "running" and inv.calls == 1 and #ctx.player.components.playercontroller.actions == 0)
        assert(ctx:step(.1) == "running" and inv.calls == 1, "equipping is not replayed while awaiting replication")
        ctx:deliver_equip(); ctx:step(.2)
        assert(inv.hands == cane and #ctx.player.components.playercontroller.actions == 1)
        point.x = 10
        assert(ctx:step(.3) == "running" and inv.calls == 2, "arrival waits for original equipment or empty hands to be restored")
        ctx:deliver_equip()
        assert(ctx:step(.4) == "done" and inv.hands == axe and inv.calls == 2)
    end)
end)
T.case("cane_follow_pause", function()
    local ctx, point = context("host")
    local inv = ctx.player.replica.inventory
    local cane, axe = ctx:item("cane", 1.25), ctx:item("axe")
    inv.slots[1], inv.hands, ctx.params.travel_cane, ctx.params.follow_mode = cane, axe, true, "direct"
    local travel, leader = Travel.new(ctx), target(12, 0)
    assert(travel:follow(leader)); ctx:step(0); ctx:deliver_equip(); ctx:step(.1)
    point.x = 8
    ctx:step(.2); ctx:step(1.19)
    assert(inv.calls == 1, "short pauses in following do not repeatedly switch equipment")
    ctx:step(1.21); assert(inv.calls == 2)
    ctx:deliver_equip(); ctx:step(1.3)
    leader.x = 13
    ctx:step(1.4)
    assert(inv.calls == 2, "a one-meter adjustment does not borrow a cane")
    leader.x = 20
    ctx:step(1.5)
    assert(inv.calls == 3, "a new long follow segment can borrow after the previous item was restored")
end)
T.each("cane_protection", { "light", "lighter", "locked", "prevent", "heavy", "mounted", "stack", "short", "disabled" }, function(boundary)
    local ctx = context("host")
    local inv = ctx.player.replica.inventory
    local cane, axe = ctx:item("cane", 1.25), ctx:item("axe")
    inv.slots[1], inv.hands, ctx.params.travel_cane = cane, axe, true
    if boundary == "light" or boundary == "lighter" then axe.tags[boundary] = true
    elseif boundary == "locked" then axe.locked = true
    elseif boundary == "prevent" then axe.prevent = true
    elseif boundary == "heavy" then inv.heavy = true
    elseif boundary == "mounted" then ctx.player.replica.rider = { IsRiding = function() return true end }
    elseif boundary == "stack" then axe.replica.stackable = {}
    elseif boundary == "disabled" then ctx.params.travel_cane = false end
    assert(Travel.new(ctx):walk({ x = boundary == "short" and 3 or 10, z = 0 }))
    ctx:step(0)
    assert(inv.calls == 0 and #ctx.player.components.playercontroller.actions == 1,
        "travel preserves equipment for protected state: " .. boundary)
end)
T.case("cane_preference", function()
    local ctx = context("host")
    local inv = ctx.player.replica.inventory
    inv.slots[1], inv.slots[2], inv.slots[3] = ctx:item("walking_stick", 1.25), ctx:item("orangestaff", 1.25), ctx:item("cane", 1.25)
    ctx.params.travel_cane = true
    assert(Travel.new(ctx):walk({ x = 10, z = 0 })); ctx:step(0)
    assert(ctx.equip_pending == inv.slots[3], "equal native speeds prefer the permanent ordinary cane")
end)
T.each("cancel_reason", { "manual", "invalid", "cancelled", "replaced" }, function(reason)
    T.each("cane_cancel_inflight", { false, true }, function(delivered)
        local ctx, point = context("host")
        local inv = ctx.player.replica.inventory
        local cane, axe = ctx:item("cane", 1.25), ctx:item("axe")
        inv.slots[1], inv.hands, ctx.params.travel_cane = cane, axe, true
        assert(Travel.new(ctx):walk({ x = 10, z = 0 })); ctx:step(0)
        if delivered then ctx:deliver_equip(); ctx:step(.1) end
        ctx.job:cancel(reason)
        ctx:deliver_equip()
        assert(ctx:step(.2) == "failed" and inv.calls == 1 and inv.hands == cane,
            "cancellation never sends a restoration, including an in-flight equip")
    end)
end)
T.case("cane_map_edit_and_takeover", function()
    local ctx, point = context("host")
    local inv = ctx.player.replica.inventory
    local cane, axe = ctx:item("cane", 1.25), ctx:item("axe")
    inv.slots[1], inv.hands, ctx.params.travel_cane = cane, axe, true
    local travel, editor = Travel.new(ctx), {}
    assert(travel:walk({ x = 10, z = 0 })); ctx:step(0)
    assert(travel:pause_route(editor)); ctx:deliver_equip()
    ctx:step(20)
    assert(inv.calls == 1 and #ctx.player.components.playercontroller.actions == 0)
    travel:resume_route(editor); ctx:step(21)
    assert(inv.hands == cane and #ctx.player.components.playercontroller.actions == 1,
        "map editing sends no equipment command and observes settled equipment when resumed")
    inv.hands = ctx:item("spear")
    assert(ctx:step(22) == "failed" and inv.calls == 1, "unexpected hand replacement relinquishes the task without restoring")
end)
T.case("cane_full_bag", function()
    local ctx, point = context("host")
    local inv = ctx.player.replica.inventory
    inv.slots[1], ctx.params.travel_cane = ctx:item("cane", 1.25), true
    assert(Travel.new(ctx):walk({ x = 10, z = 0 })); ctx:step(0); ctx:deliver_equip(); ctx:step(.1)
    for slot = 1, inv:GetNumSlots() do inv.slots[slot] = ctx:item("log") end
    point.x = 10
    assert(ctx:step(.2) == "failed" and inv.calls == 1 and inv.hands.prefab == "cane",
        "restoring empty hands cannot drop a cane when the bag filled during travel")
end)
T.case("cane_depletion", function()
    local ctx = context("host")
    local inv = ctx.player.replica.inventory
    local stick, axe, spare = ctx:item("walking_stick", 1.25), ctx:item("axe"), ctx:item("cane", 1.1)
    inv.slots[1], inv.slots[2], inv.hands, ctx.params.travel_cane = stick, spare, axe, true
    assert(Travel.new(ctx):walk({ x = 10, z = 0 })); ctx:step(0); ctx:deliver_equip(); ctx:step(.1)
    stick.removed, inv.hands = true, nil
    ctx:step(.2); ctx:deliver_equip(); ctx:step(.3)
    assert(inv.hands == axe and inv.calls == 2, "a depleted walking stick restores the original and does not start borrowing replacements")
end)
local function direct_context(mode)
    local ctx, player, rpc = context(mode)
    local controller, G = player.components.playercontroller, ctx.G
    G.RPC.DirectWalking, G.RPC.StopWalking = "direct", "stop"
    controller.remote_vector = { x = 0, y = 0, z = 0 }
    local env = { PlayerController = {}, RPC = G.RPC, SendRPCToServer = G.SendRPCToServer }
    for _, method in ipairs({ "RemoteDirectWalking", "RemoteStopWalking" }) do
        controller[method] = T.eval(assert(T.source("components/playercontroller")
            :match("function PlayerController:" .. method .. "%b().-\nend"))
            .. "\nreturn PlayerController." .. method, env)
    end
    local locomotor = controller.locomotor
    if locomotor then
        locomotor.RunInDirection = T.eval(assert(T.source("components/locomotor")
            :match("function LocoMotor:RunInDirection%b().-\nend")) .. "\nreturn LocoMotor.RunInDirection", { LocoMotor = {} })
        function locomotor:WalkInDirection(direction, run)
            self.direction, self.run, self.bufferedaction = direction, run, nil
        end
    end
    return ctx, player, rpc
end

T.each("water walking follows actual player physics", { "host", "predict", "remote" }, function(mode)
    local ctx, player, rpc = direct_context(mode)
    local controller = player.components.playercontroller
    ctx.G.TheWorld.Map.IsPassableAtPoint = function(_, x, _, _, water) return x <= 0 or water == true end
    local travel = Travel.new(ctx)
    assert(not travel:walk({ x = 10, z = 0 }), "an ordinary player cannot walk into open water")
    player.Physics.GetCollisionMask = function() return 3 end
    local checked
    function ctx.G.TheWorld.Pathfinder:IsClear(_, _, _, _, _, _, caps)
        checked = caps.allowocean
        return caps.allowocean == true
    end
    assert(travel:walk({ x = 10, z = 0 }))
    assert(ctx:step(0) == "running" and checked and #controller.actions == 0,
        "water movement must not be sent through the land-only WALKTO picker")
    if mode == "remote" then
        assert(rpc[1][1] == "direct" and rpc[1][2] == 1 and rpc[1][3] == 0,
            "native direct walking takes a unit direction, not a world position")
    else assert(controller.locomotor.direction == 0 and controller.locomotor.run) end
    player.x = 9.5
    assert(ctx:step(.1) == "done")
    ctx.job:cancel("done")
    if mode == "remote" then assert(rpc[#rpc][1] == "stop" and controller.remote_vector.y == 0)
    else assert(controller.locomotor.stopped) end
end)

T.each("cursor travel never drops the held item", { "host", "predict", "remote" }, function(mode)
    local ctx, player, rpc = direct_context(mode)
    local cursor = { prefab = "log" }
    player.replica.inventory.active = cursor
    assert(Travel.new(ctx):walk({ x = 10, z = 0 }))
    assert(ctx.job.input.cursor == "log" and ctx.job.input.placement and ctx:step(0) == "running")
    assert(#player.components.playercontroller.actions == 0 and player.replica.inventory.active == cursor)
    ctx.job:cancel("cancelled")
    if mode == "remote" then assert(#rpc == 2 and rpc[2][1] == "stop") end
end)

T.case("direct movement relinquishes newer actions", function()
    local ctx, player = direct_context("predict")
    player.Physics.GetCollisionMask = function() return 3 end
    assert(Travel.new(ctx):walk({ x = 10, z = 0 })); ctx:step(0)
    local newer, locomotor = {}, player.components.playercontroller.locomotor
    player.bufferedaction, locomotor.bufferedaction = newer, newer
    assert(ctx:step(.1) == "failed")
    ctx.job:cancel("manual")
    assert(player.bufferedaction == newer and locomotor.bufferedaction == newer and not locomotor.stopped)
end)

T.each("continuous movement stops while the next leg is being planned", { "host", "predict", "remote" }, function(mode)
    local ctx, player, rpc = direct_context(mode)
    player.replica.inventory.active = { prefab = "log" }
    local finder = ctx.G.TheWorld.Pathfinder
    function finder:IsClear(_, _, _, _, _, z) return z == 0 end
    function finder:SubmitSearch() return 1 end
    function finder:GetSearchStatus() return 0 end
    function finder:KillSearch() end
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 10, z = 0 })); assert(travel:add_waypoint({ x = 10, z = 10 }))
    ctx:step(0)
    player.x = 10
    assert(ctx:step(.1) == "running" and ctx.job.phase == "planning")
    if mode == "remote" then assert(rpc[#rpc][1] == "stop")
    else assert(player.components.playercontroller.locomotor.stopped) end
    ctx.job:cancel("cancelled")
end)

local function sailing_context(mode, steering)
    local ctx, player, rpc = context(mode)
    local G, controller = ctx.G, player.components.playercontroller
    local boat = T.entity({ tags = { boat = true }, Physics = physics(4) })
    player.platform = boat
    local map = G.TheWorld.Map
    function map:GetPlatformAtPoint(x, z)
        for _, candidate in ipairs(ctx.boats or { boat }) do
            if (candidate.x - x)^2 + (candidate.z - z)^2 <= candidate:GetPhysicsRadius(0)^2 then return candidate end
        end
    end
    function map:IsOceanTileAtPoint() return true end
    map.IsOceanAtPoint = T.eval(assert(T.source("components/map"):match("function Map:IsOceanAtPoint%b().-\nend"))
        .. "\nreturn Map.IsOceanAtPoint", { Map = {} })
    function map:IsVisualGroundAtPoint() return false end
    function map:IsPassableAtPoint(x, _, z, allow_water)
        return allow_water or self:GetPlatformAtPoint(x, z) ~= nil
    end
    G.ACTIONS.ROW, G.ACTIONS.ROW_FAIL, G.ACTIONS.ROW_CONTROLLER = { id = "ROW", code = 10 },
        { id = "ROW_FAIL", code = 11 }, { id = "ROW_CONTROLLER", code = 12 }
    G.RPC.SteerBoat = "steer"
    G.TUNING = { MAX_WALKABLE_PLATFORM_RADIUS = 4 }
    G.TheSim = { FindEntities = function() return ctx.boats or { boat } end }
    local oar = ctx:item("oar")
    function oar:HasActionComponent(name) return name == "oar" end
    player.replica.inventory.hands = oar
    if steering then
        player:AddTag("steeringboat")
        player.components.steeringwheeluser = { SteerInDir = function(_, x, z) ctx.heading = { x = x, z = z } end }
    end
    player.AnimState = { IsCurrentAnimation = function() return false end,
        GetCurrentAnimationFrame = function() return ctx.row_fail and 0 or 40 end }
    local row = T.eval(assert(T.source("componentactions"):match("local function Row%b().-\nend"))
        .. "\nreturn Row", { ACTIONS = G.ACTIONS, TheWorld = G.TheWorld, ThePlayer = player,
            VecUtil_Normalize = function(x, z)
                local d = math.sqrt(x*x + z*z)
                if d == 0 then return 0, 0 end
                return x/d, z/d
            end })
    player.components.playeractionpicker.GetPointActions = function(_, point, item, right)
        assert(item == oar and right)
        local definitions, actions = {}, {}
        row(item, player, point, definitions)
        for _, definition in ipairs(definitions) do
            actions[#actions + 1] = G.BufferedAction(player, nil, definition, item, point)
        end
        return actions
    end
    return ctx, player, boat, rpc
end

T.each("sailing uses the hull and fixed world waypoints", { "host", "predict", "remote" }, function(mode)
    local ctx, player, boat = sailing_context(mode)
    local controller = player.components.playercontroller
    ctx.params.travel_cane = true
    local checks = {}
    function ctx.G.TheWorld.Pathfinder:IsClear(_, _, z, _, _, _, caps)
        assert(caps.allowocean and caps.ignoreLand and caps.ignorecreep)
        checks[z] = true
        return true
    end
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 12, z = 0 }))
    assert(ctx:step(0) == "running" and controller.actions[1].action.id == "ROW")
    assert(checks[4.25] and checks[-4.25], "both sides of the hull must clear the shoreline")
    assert(player.replica.inventory.calls == 0, "rowing must keep its oar instead of borrowing a cane")
    local cast = controller.actions[1]
    if mode == "predict" then cast.preview_cb() end
    boat.x, player.x = 8, 11
    assert(ctx:step(.6) == "running", "walking to the bow does not complete the boat's route")
    assert(travel:route_snapshot().origin.x == 8 and travel:route_snapshot().path[1].x == 12)
    boat.x, player.x = 11, 14
    assert(ctx:step(.7) == "done")
    ctx.job:cancel("done")
end)

T.each("an oar does not turn a neighbouring deck into a sailing destination", { "host", "predict", "remote" }, function(mode)
    local ctx, player, boat = sailing_context(mode)
    local neighbour = T.entity({ x = 8.25, tags = { boat = true }, Physics = physics(4) })
    ctx.boats = { boat, neighbour }
    player.x = 3.7
    assert(Travel.new(ctx):walk({ x = neighbour.x, z = 0 }))
    assert(ctx.job.movement.mode == "walk", "a deck is a native walking destination even when an oar is equipped")
    ctx:step(0)
    local action = player.components.playercontroller.actions[1]
    assert(action.action == ctx.G.ACTIONS.WALKTO and action.pos.walkable_platform == neighbour,
        "native WALKTO owns platform hopping and follows the destination deck")
end)

T.case("early rowing frames wait instead of issuing ROW_FAIL", function()
    local ctx, player = sailing_context("host")
    player:AddTag("is_rowing")
    ctx.row_fail = true
    assert(Travel.new(ctx):walk({ x = 20, z = 0 }))
    assert(ctx:step(0) == "running" and #player.components.playercontroller.actions == 0)
    ctx.row_fail = false
    assert(ctx:step(.1) == "running" and player.components.playercontroller.actions[1].action.id == "ROW")
    ctx:step(.2)
    assert(#player.components.playercontroller.actions == 1)
end)

T.each("sailing rejects a path narrower than its hull", { "shore", "boat" }, function(obstacle)
    local ctx, player, boat = sailing_context("host")
    local finder = ctx.G.TheWorld.Pathfinder
    if obstacle == "shore" then
        function finder:IsClear(_, _, z, _, _, dest_z)
            return z < 4 or dest_z < 4
        end
    else
        ctx.boats = { boat, T.entity({ x = 10, z = 7, tags = { boat = true }, Physics = physics(4) }) }
    end
    function finder:SubmitSearch() return 1 end
    function finder:GetSearchStatus() return 1 end
    function finder:GetSearchResult() return { steps = { { x = 20, z = 0 } } } end
    function finder:KillSearch() self.killed = true end
    assert(Travel.new(ctx):walk({ x = 20, z = 0 }))
    assert(ctx:step(0) == "running" and #player.components.playercontroller.actions == 0)
    for i = 1, 200 do
        assert(ctx:step(i * .1) == "running")
        if #player.components.playercontroller.actions > 0 then break end
    end
    local snapshot = ctx.job:path_snapshot()
    assert(#snapshot > 0 and #player.components.playercontroller.actions > 0,
        "hull checks guide the search around the obstruction")
    local bent, previous = false, boat:GetPosition()
    for _, point in ipairs(snapshot) do
        bent = bent or point.z < 0
        assert(ctx.job.movement:clear(previous, point), "every detour segment leaves room for the hull")
        previous = point
    end
    assert(bent, "the detour leaves room for the hull on the obstructed side")

end)

T.case("controller rowing approaches the deck edge first", function()
    local ctx, player, boat = sailing_context("host")
    local controller = player.components.playercontroller
    controller.isclientcontrollerattached = true
    assert(Travel.new(ctx):walk({ x = 20, z = 0 })); ctx:step(0)
    assert(controller.actions[1].action.id == "WALKTO")
    player.x = 3.6
    assert(ctx:step(.1) == "running" and controller.actions[2].action.id == "ROW_CONTROLLER")
    assert(boat.x == 0 and ctx.job.movement.sailing)
end)

T.each("controller rowing faces the desired direction from the correct edge", { "west", "north" }, function(side)
    local ctx, player, boat = sailing_context("host")
    local controller = player.components.playercontroller
    controller.isclientcontrollerattached = true
    player.x, player.z = side == "west" and -3.7 or 0, side == "north" and 3.7 or 0
    assert(Travel.new(ctx):walk({ x = 20, z = 0 })); ctx:step(0)
    local approach = controller.actions[1]
    assert(approach.action.id == "WALKTO" and approach:GetActionPoint().x > 3.5,
        "being at any deck edge is insufficient: the actor must reach the intended side")
    player.x, player.z = 3.7, 0
    ctx:step(.1)
    local action = controller.actions[2]
    assert(action.action.id == "ROW_CONTROLLER")
    local force
    boat.components.boatphysics = { ApplyRowForce = function(_, x, z) force = { x = x, z = z } end }
    local row = T.eval(assert(T.source("components/oar"):match("function Oar:Row%b().-\nend"))
        .. "\nreturn Oar.Row", { Oar = {}, VecUtil_Normalize = function(x, z)
            local length = math.sqrt(x*x + z*z)
            return x/length, z/length
        end })
    row({ force = .4, max_velocity = 4 }, player, action:GetActionPoint())
    assert(force.x == 1 and force.z == 0, "the native oar uses the actor's radial position")
end)

T.each("steering preserves the native control path", { "host", "predict", "remote" }, function(mode)
    local ctx, player, boat, rpc = sailing_context(mode, true)
    player.components.playercontroller.IsBusy = function() return true end
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 12, z = 0 }))
    assert(ctx:step(0) == "running" and #player.components.playercontroller.actions == 0)
    if mode == "host" then assert(ctx.heading.x == 1 and ctx.heading.z == 0)
    else assert(rpc[1][1] == "steer" and rpc[1][2] == 1 and rpc[1][3] == 0) end
    assert(travel:pause_route("map"))
    local sent = #rpc
    assert(ctx:step(.1) == "running" and #rpc == sent)
    travel:resume_route("map")
    player:RemoveTag("steeringboat")
    assert(ctx:step(.2) == "failed" and #rpc == sent)
end)

T.case("boat loss and pending rowing cancellation", function()
    local ctx, player, boat, rpc = sailing_context("predict")
    assert(Travel.new(ctx):walk({ x = 20, z = 0 })); ctx:step(0)
    local action = player.components.playercontroller.actions[1]
    player.platform = nil
    assert(ctx:step(.1) == "failed")
    ctx.job:cancel("cancelled"); action.preview_cb()
    assert(#rpc == 0 and player.bufferedaction == nil)
end)

T.case("arrival cancels a row which has not executed yet", function()
    local ctx, player, boat, rpc = sailing_context("predict")
    assert(Travel.new(ctx):walk({ x = 20, z = 0 })); ctx:step(0)
    local action = player.components.playercontroller.actions[1]
    boat.x, player.x = 20, 20
    assert(ctx:step(.1) == "done")
    ctx.job:cancel("done"); action.preview_cb()
    assert(#rpc == 0 and player:GetBufferedAction() == nil,
        "another sailor moving the boat to its destination cannot leave a queued paddle stroke")
end)

T.each("static previews plan all waypoints without dispatch", { "staged", "paused" }, function(mode)
    local ctx, position, rpc = context("remote")
    local travel, editor = Travel.new(ctx), {}
    if mode == "paused" then
        assert(travel:walk({ x = 12, z = 0 }))
        assert(travel:pause_route(editor))
    else assert(travel:add_waypoint({ x = 12, z = 0 })) end
    assert(travel:add_waypoint({ x = 20, z = 8 }))
    for _ = 1, 8 do travel:update_preview() end
    local snapshot = travel:route_snapshot()
    assert(snapshot.preview_legs == 2 and #snapshot.path == 2 and not snapshot.reason)
    assert(ctx.now == 0 and #rpc == 0 and not ctx.player.bufferedaction,
        "static UI frames neither advance game time nor send actions")
    if mode == "paused" then travel:resume_route(editor)
    else assert(travel:navigate()) end
    assert(ctx:step(.1) == "running")
    local goal = assert(ctx.player.bufferedaction):GetActionPoint()
    assert(goal.x == 12 and goal.z == 0, "execution accepts the first validated preview leg")
    ctx.job:cancel("manual")
end)

T.case("default route preferences use observed dangers and native road speed", function()
    local ctx = context("host")
    ctx.params.travel_fast, ctx.params.travel_avoid = true, true
    ctx.G.TUNING = { BISHOP_TARGET_DIST = 4 }
    local enemy = target(20, 0, { hostile = true })
    enemy.prefab = "bishop"
    local unseen = target(999, 0)
    unseen.prefab = "bishop"
    ctx.G.TheSim = { FindEntities = function() return { enemy, unseen } end,
        GetScreenPos = function(_, x, _, z) return x, z + 50 end, GetScreenSize = function() return 100, 100 end }
    ctx.G.GROUND_ROADWAYS = { [1] = true }
    ctx.G.TheWorld.Map.GetTileAtPoint = function(_, _, _, z) return z >= 5 and z <= 7 and 1 or 0 end
    local locomotor = ctx.player.components.playercontroller.locomotor
    locomotor.FasterOnRoad = function() return true end
    locomotor.FasterOnCreep = function() return false end
    locomotor.IsFasterOnGroundTile = function() return false end
    local travel = Travel.new(ctx)
    assert(travel:walk({ x = 60, z = 0 }))
    for i = 1, 200 do
        assert(ctx:step(i * .1) == "running")
        if ctx.player.bufferedaction then break end
    end
    local snapshot, road, previous = travel:route_snapshot(), false, { x = 0, z = 0 }
    local Planner = require("fieldkit/game/route_planner")
    local hazards = Planner.hazards(ctx, ctx.job.movement)
    assert(#hazards == 1 and #snapshot.path > 1)
    for _, point in ipairs(snapshot.path) do
        road = road or point.z >= 5 and point.z <= 7
        assert(Planner.safe(previous, point, hazards))
        previous = point
    end
    assert(road, "the chosen detour uses the faster road")
    local actions = #ctx.player.components.playercontroller.actions
    enemy.x, enemy.z = snapshot.path[1].x, snapshot.path[1].z
    assert(ctx:step(ctx.now + 1) == "running")
    assert(#ctx.player.components.playercontroller.actions == actions and not ctx.player.bufferedaction,
        "a newly blocked next leg stops the owned action and replans")
    ctx.job:cancel("manual")
end)

T.done()
