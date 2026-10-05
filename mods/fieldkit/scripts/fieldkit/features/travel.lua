local Inventory = require("fieldkit/game/inventory")
local Query = require("fieldkit/game/query")
local Movement = require("fieldkit/game/movement")
local Planner = require("fieldkit/game/route_planner")
local M = {}
local route_errors = {
    invalid_destination = { "航点或所在平台已不可用", "Waypoint or platform is no longer available" },
    unexplored_path = { "路线经过未探索区域，请沿已知区域添加航点", "Route crosses unexplored ground; add waypoints through known areas" },
    blocked_path = { "原生路线存在不可通行路段", "Native route contains a blocked segment" },
    no_path = { "未找到可通行路线", "No passable route was found" },
    search_timeout = { "路线搜索超时，已停止", "Route search timed out; stopped" },
    search_unavailable = { "原生寻路暂时不可用", "Native pathfinding is currently unavailable" },
    route_changed = { "路线连续受阻，已停止", "Route repeatedly became blocked; stopped" },
    invalid_path = { "原生寻路返回了无效路线", "Native pathfinding returned an invalid route" },
    complex_path = { "路线过于复杂，请分段选择航点", "Route is too complex; choose intermediate waypoints" },
}

local function finite(n)
    return type(n) == "number" and n == n and math.abs(n) < math.huge
end

local function distance(a, b)
    return math.sqrt((a.x - b.x) ^ 2 + (a.z - b.z) ^ 2)
end

local function radius(entity)
    local value = entity.Physics and entity.Physics:GetRadius() or entity:GetPhysicsRadius(0)
    return finite(value) and math.max(0, value) or 0
end

-- A bounded FIFO preserves turns. Overflow stops instead of silently losing the route.
local function trail()
    local self = { points = {}, head = 1, size = 0, length = 0 }
    function self:first() return self.points[self.head] end
    function self:clear()
        self.points, self.head, self.size, self.length = {}, 1, 0, 0
    end
    function self:add(point, length)
        if self.size == 128 or self.length + length > 96 then return false end
        local index = (self.head + self.size - 1) % 128 + 1
        self.points[index] = { position = point, incoming = length }
        self.size, self.length = self.size + 1, self.length + length
        return true
    end
    function self:pop()
        self.points[self.head] = nil
        self.head, self.size = self.head % 128 + 1, self.size - 1
        local first = self:first()
        self.length = first and math.max(0, self.length - first.incoming) or 0
        if first then first.incoming = 0 end
    end
    return self
end

function M.new(ctx)
    local self, G, player = {}, ctx.G, ctx.player
    local map, pathfinder = G.TheWorld.Map, G.TheWorld.Pathfinder
    local waypoints, preview = {}, nil
    local function fail(zh, en)
        return false, ctx:text(zh, en)
    end
    local function point_valid(point)
        return point and finite(point.x) and finite(point.z)
    end
    local function passable(point)
        return point_valid(point) and map:IsPassableAtPoint(point.x, 0, point.z, Movement.capabilities(ctx).allowocean)
    end
    local function usable(point)
        return passable(point) and G.CanEntitySeePoint(player, point.x, 0, point.z)
    end
    local function explored(point)
        return point_valid(point) and player:CanSeePointOnMiniMap(point.x, 0, point.z)
    end
    local function capture(point)
        return G.DynamicPosition(G.Vector3(point.x, 0, point.z))
    end
    local function platform(entity) return entity:GetCurrentPlatform() end
    local function clear(a, b)
        return pathfinder:IsClear(a.x, 0, a.z, b.x, 0, b.z, Movement.capabilities(ctx))
    end
    local function target_valid(target, search_radius)
        return target ~= player and Query.visible(ctx, target)
            and not target:HasTag("playerghost") and not target:HasTag("corpse")
            and not (target.replica and target.replica.health and target.replica.health:IsDead())
            and point_valid(target:GetPosition())
            and distance(player:GetPosition(), target:GetPosition()) <= (search_radius or ctx.params.search_radius)
    end
    function self:can_follow(target, search_radius)
        return target_valid(target, search_radius)
            and (target:HasTag("player") or target:HasTag("locomotor") or target:HasTag("balloon"))
    end
    function self:can_push(target, search_radius)
        if not target_valid(target, search_radius) or platform(player) ~= platform(target) then return false end
        local a, b = player.Physics, target.Physics
        return a ~= nil and b ~= nil and a:IsActive() and b:IsActive()
            and a:GetMass() > 0 and b:GetMass() > 0
            and G.bit.band(a:GetCollisionMask(), b:GetCollisionGroup()) ~= 0
            and G.bit.band(b:GetCollisionMask(), a:GetCollisionGroup()) ~= 0
    end
    local function adjacent(position, target_point, gap, target_platform)
        local dx, dz = position.x - target_point.x, position.z - target_point.z
        local length = math.sqrt(dx * dx + dz * dz)
        if length < .001 then dx, dz, length = 1, 0, 1 end
        dx, dz = dx / length, dz / length
        -- Try the near side first; shoreline and wall corners may require another side.
        for _, angle in ipairs({ 0, 30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180 }) do
            local c, s = math.cos(angle * math.pi / 180), math.sin(angle * math.pi / 180)
            local point = { x = target_point.x + (dx * c - dz * s) * gap,
                z = target_point.z + (dx * s + dz * c) * gap }
            if map:GetPlatformAtPoint(point.x, point.z) == target_platform
                and usable(point) and clear(target_point, point) then return point end
        end
    end
    local function discard_preview()
        if preview and preview.planner then preview.planner:cancel() end
        preview = nil
    end
    local function preview_matches(movement, queue)
        if not preview or preview.fast ~= (ctx.params.travel_fast == true)
            or preview.avoid ~= (ctx.params.travel_avoid == true) or #preview.goals ~= #queue
            or not preview.movement:valid() or preview.movement.mode ~= movement.mode
            or distance(preview.origin, movement:position()) > .5 then return false end
        local caps = movement:capabilities()
        for key, value in pairs(caps) do if preview.caps[key] ~= value then return false end end
        for key, value in pairs(preview.caps) do if caps[key] ~= value then return false end end
        for i, record in ipairs(queue) do
            local point = record:GetPosition()
            if not point or distance(point, preview.goals[i]) > .5 then return false end
        end
        return true
    end
    local function start(point, target, mode, destinations)
        local search_radius = ctx.params.search_radius
        local follow_distance, keep_distance = ctx.params.follow_distance, ctx.params.follow_keep_distance
        if ctx.config.travel == false then return fail("移动辅助已关闭", "Travel assistance is disabled") end
        local ready, reason = ctx:ready()
        if not ready then return false, reason end
        if not point_valid(point) then return fail("请选择有效位置", "Choose a valid position") end
        local movement = Movement.new(ctx, point, mode == "route")
        local function passable(point) return point_valid(point) and movement:passable(point) end
        local function clear(a, b) return movement:clear(a, b) end
        local function capture(point) return movement:capture(point) end
        if not (mode == "route" and explored(point) and passable(point) or mode ~= "route" and usable(point)) then
            return fail("请选择已探索且可通行的位置", "Choose an explored, passable position")
        end
        local route, last_sample = trail(), nil
        local ticket, destination, destination_key, next_update, progress_key, best_error, progress_at
        local waiting, last_push_position = false, nil
        local approach_origin, approach_goal
        local use_cane = ctx.params.travel_cane == true and mode ~= "push" and not movement.sailing
            and not player.replica.inventory:GetActiveItem()
        local borrowed, equip_receipt, restoring, cane_checked, cane_disabled, idle_since, equip_until
        local inv = player.replica.inventory
        local base_name = mode == "push" and ctx:text("推挤（最多 30 秒）", "Push (up to 30 seconds)")
            or target and (mode == "trail" and ctx:text("轨迹跟随", "Trail follow") or ctx:text("直接跟随", "Direct follow"))
            or movement.sailing and ctx:text("海面路线", "Sea route") or ctx:text("地图路线", "Map route")
        local cursor = inv:GetActiveItem()
        local job = {
            name = base_name,
            movement = movement,
            input = { mode = "world", cursor = movement.mode == "direct" and cursor and cursor.prefab or nil,
                placement = movement.mode == "direct" and cursor ~= nil or nil },
            timeout = mode == "push" and 30 or 600,
            timeout_message = ctx:text("移动辅助已达到时限，已停止", "Travel assistance reached its time limit; stopped"),
            error_message = ctx:text("移动辅助失败，已停止", "Travel assistance failed; stopped"),
        }
        local function stop_moving()
            movement:stop(ticket, "cancelled")
            ticket, destination, destination_key, next_update = nil, nil, nil, nil
        end
        local function equipment_error()
            return "failed", ctx:text("装备状态改变或切换未确认，移动停止", "Equipment changed or switching was not confirmed; travel stopped")
        end
        local function restore(now)
            stop_moving()
            equip_until = equip_until or now + 6
            local why
            if borrowed.original then equip_receipt, why = ctx.equipment:equip(job, borrowed.original)
            else equip_receipt, why = ctx.equipment:unequip(job, borrowed.item) end
            if equip_receipt then restoring = true
            elseif why ~= "busy" or now >= equip_until then
                return "failed", ctx:text("无法安全恢复原装备，请手动换回", "The original equipment cannot be restored safely; switch back manually")
            end
            return "running"
        end
        local function cane()
            local hands = inv:GetEquippedItem(G.EQUIPSLOTS.HANDS)
            local rider = player.replica.rider
            if inv:IsHeavyLifting() or rider and rider:IsRiding()
                or hands and (hands.replica.stackable ~= nil
                    or hands:HasTag("lighter") or hands:HasTag("light") or hands.prefab == "nightstick"
                    or hands.replica.inventoryitem:IsLockedInSlot()
                    or hands.replica.equippable:ShouldPreventUnequipping()) then return end
            local speed = hands and hands.replica.inventoryitem:GetWalkSpeedMult() or 1
            local chosen
            -- Stable order prefers the permanent cane when native speed multipliers tie.
            local items = Inventory.items(ctx)
            for _, prefab in ipairs({ "cane", "orangestaff", "walking_stick" }) do
                for _, entry in ipairs(items) do
                    local item = entry.item
                    local value = item.replica.inventoryitem and item.replica.inventoryitem:GetWalkSpeedMult()
                    if entry.slot and item.prefab == prefab and value and value > speed
                        and item.replica.equippable and not item.replica.equippable:IsRestricted(player) then
                        chosen, speed = item, value
                    end
                end
            end
            return chosen, hands
        end
        local legs, completed, path, path_index = destinations or {}, 0, nil, 1
        if movement.sailing then
            local fixed = {}
            for _, record in ipairs(legs) do
                local point = record:GetPosition()
                if not passable(point) then return fail("航行航点必须位于海面", "Sailing waypoints must be on the ocean") end
                fixed[#fixed + 1] = capture(point)
            end
            legs = fixed
        end
        if mode == "route" then job.waypoints, job.completed, job.phase = legs, 0, "planning" end
        function job:path_snapshot()
            local points = {}
            if not path then return points, false end
            -- Rendering is bounded independently of the native navigation path.
            local last = math.min(#path, path_index + 255)
            for i = path_index, last do
                local point = path[i]:GetPosition()
                points[#points + 1] = point and { x = point.x, z = point.z } or { invalid = true }
            end
            return points, last < #path
        end
        local planner, replans = nil, 0
        local fast, avoid = ctx.params.travel_fast == true, ctx.params.travel_avoid == true
        local function kill_search()
            if planner then planner:cancel() end
            planner = nil
        end
        function job:use_preview()
            if not preview_matches(movement, legs) or not preview.legs[1] then return end
            path, path_index = {}, 1
            for _, point in ipairs(preview.legs[1]) do path[#path + 1] = capture(point) end
        end
        if mode == "route" then job:use_preview() end
        local function next_route(now, position)
            local goal = legs[1] and legs[1]:GetPosition()
            while goal and distance(position, goal) <= movement.reach do
                table.remove(legs, 1)
                completed, path, path_index, replans = completed + 1, nil, 1, 0
                job.completed = completed
                kill_search()
                goal = legs[1] and legs[1]:GetPosition()
            end
            if #legs == 0 then return "done" end
            if not goal or not explored(goal) or not passable(goal) then return "failed", "invalid_destination" end
            if planner and distance(planner.goal, goal) > .5 then kill_search(); path = nil end
            if not path then
                if planner and distance(planner.origin, position) > 2 then kill_search() end
                planner = planner or Planner.new(ctx, movement, position, goal, { fast = fast, avoid = avoid, native = true })
                local state, points, reason = planner:step(now)
                if state == "planning" then return "planning" end
                if state ~= "done" then return "failed", reason end
                path = {}
                for _, point in ipairs(points) do path[#path + 1] = capture(point) end
            end
            local record = path[path_index]
            local goal_point = record and record:GetPosition()
            while goal_point and distance(position, goal_point) <= movement.reach do
                path_index = path_index + 1
                record = path[path_index]
                goal_point = record and record:GetPosition()
            end
            if not record then
                -- Moving platforms can carry the final leg point after the path was captured.
                path, path_index = nil, 1
                kill_search()
                return "planning"
            end
            if not goal_point or not explored(goal_point) then return "failed", "invalid_destination" end
            local safe = clear(position, goal_point)
            if safe and avoid then
                safe = planner and planner:clear(position, goal_point)
                    or not planner and Planner.safe(position, goal_point, Planner.hazards(ctx, movement))
            end
            if distance(position, goal_point) > 48 or not safe then
                stop_moving()
                path, path_index, replans = nil, 1, replans + 1
                kill_search()
                if replans > 3 then return "failed", "route_changed" end
                return "planning"
            end
            return "walking", goal_point, record
        end
        local function sample(point)
            local current = capture(point)
            local previous = last_sample and last_sample:GetPosition()
            if last_sample and not previous then return false end
            local length = previous and distance(previous, point) or 0
            if previous and length < .5 and last_sample.walkable_platform == current.walkable_platform then return true end
            if previous then
                if last_sample.walkable_platform == current.walkable_platform then
                    if length > 8 or not clear(previous, point) then return false end
                else
                    -- Keep both landing points. Native locomotor checks embark range, occupancy and hopping.
                    -- ponytail: platform transitions over 8m stop; replaying remote teleports needs explicit support.
                    if length > 8 then return false end
                end
            end
            if not route:add(current, route.size > 0 and length or 0) then return false end
            last_sample = current
            return true
        end
        local owner = self
        function job:pause(owner)
            if mode ~= "route" or self.closed or self.edit_owner and self.edit_owner ~= owner then return false end
            if self.edit_owner == owner then return true end
            self.edit_owner, self.phase = owner, "paused"
            stop_moving()
            kill_search()
            path, path_index, progress_key, best_error, progress_at = nil, 1, nil, nil, nil
            self.name = base_name .. ctx:text(" · 编辑中", " · editing")
            return true
        end
        function job:resume(owner)
            if self.edit_owner == owner then
                self:use_preview()
                self.edit_owner, self.phase = nil, "planning"
            end
        end
        function job:step(now)
            if self.closed then return "failed" end
            if self.edit_owner then return "running" end
            if not movement:valid() then
                return "failed", ctx:text("航行装备或船只已改变，导航停止", "Sailing equipment or platform changed; navigation stopped")
            end
            if not ctx.equipment:ready(self) then return "running" end
            if equip_receipt then
                if equip_receipt:state() ~= "done" then return equipment_error() end
                equip_receipt, equip_until = nil, nil
                if restoring then borrowed, restoring, cane_checked = nil, false, false end
                progress_key, progress_at = nil, now
            end
            if borrowed and not Inventory.equipped(ctx, borrowed.item) then
                if not borrowed.item:IsValid() and not inv:GetEquippedItem(G.EQUIPSLOTS.HANDS) then
                    cane_disabled = true
                    if borrowed.original then return restore(now) end
                    borrowed = nil
                else
                    return equipment_error()
                end
            end
            local position = movement:position()
            local key, remaining, approaching
            if target then
                if not (mode == "push" and owner:can_push(target, search_radius)
                    or mode ~= "push" and owner:can_follow(target, search_radius)) then
                    return "failed", ctx:text("目标已离开范围或不再适合此操作", "Target is no longer nearby or suitable")
                end
                local target_point = target:GetPosition()
                local gap = follow_distance + radius(player) + radius(target)
                local separation = distance(position, target_point)
                if mode == "push" then
                    point, key = target_point, target
                    remaining = math.max(0, separation - player.Physics:GetRadius() - target.Physics:GetRadius())
                    if remaining >= .4 or not last_push_position then
                        last_push_position = capture(target_point)
                    elseif distance(last_push_position:GetPosition() or target_point, target_point) >= .25 then
                        progress_at, last_push_position = now, capture(target_point)
                    end
                else
                    local same_platform = platform(player) == platform(target)
                    -- Native arrival also allows half a movement step; tolerate fast or mounted movement.
                    local in_range = separation <= gap + (waiting and .5 or .35)
                    local too_close = keep_distance and separation < gap - (waiting and .5 or .35)
                    if same_platform and in_range and not too_close and clear(position, target_point) then
                        stop_moving()
                        route:clear()
                        last_sample, progress_key, progress_at, waiting = nil, nil, now, true
                        approach_origin, approach_goal = nil, nil
                        self.name = base_name .. ctx:text(" · 等待目标", " · waiting")
                        idle_since = idle_since or now
                        if borrowed and now - idle_since >= 1 then return restore(now) end
                        return "running"
                    end
                    waiting = false
                    if mode == "trail" and not too_close then
                        -- Ocean points during the jump are not walk destinations; wait for a landing sample.
                        if not target:HasTag("boathopping") and (not passable(target_point) or not sample(target_point)) then
                            return "failed", ctx:text("轨迹中断或积压过多，已停止", "Trail interrupted or backlog limit reached; stopped")
                        end
                        local first = route:first()
                        while first do
                            point = first.position:GetPosition()
                            if not point then return "failed", ctx:text("轨迹所在平台已消失", "A trail platform disappeared") end
                            if distance(position, point) > movement.reach then break end
                            route:pop()
                            first = route:first()
                        end
                        if not first then
                            stop_moving()
                            progress_key, progress_at = nil, now
                            return "running"
                        end
                        key, remaining = first, distance(position, point)
                    else
                        point = adjacent(position, target_point, gap, platform(target))
                        if not point then return "failed", ctx:text("目标附近没有可用落脚点", "No usable position around target") end
                        key, remaining = target, math.abs(separation - gap)
                        local origin = approach_origin and approach_origin:GetPosition()
                        local goal = approach_goal and approach_goal:GetPosition()
                        approaching = origin and goal and distance(origin, goal) - distance(position, goal) >= .25
                        if not origin or not goal or approaching then
                            approach_origin, approach_goal = capture(position), capture(point)
                        end
                    end
                end
            else
                local state, next_point, record = next_route(now, position)
                self.phase = state
                if state == "done" then
                    if borrowed then return restore(now) end
                    if movement.mode == "steer" then
                        return "done", ctx:text("已到达航点，请收帆或下锚停船", "Waypoint reached; furl the sail or lower the anchor to stop the boat")
                    end
                    return "done"
                end
                if state == "failed" then
                    local message = route_errors[next_point] or route_errors.no_path
                    return "failed", ctx:text(message[1], message[2])
                end
                if state == "planning" then
                    stop_moving()
                    self.name = base_name .. ctx:text(" · 正在规划", " · planning")
                    progress_key, progress_at = nil, now
                    return "running"
                end
                point, key, remaining = next_point, record, distance(position, next_point)
            end
            if not (target and usable(point) or not target and explored(point) and passable(point)) then
                return "failed", ctx:text("目的地已不可用", "Destination is no longer available")
            end
            idle_since = nil
            if use_cane and not borrowed and not cane_checked and not cane_disabled and remaining >= 4 then
                local item, original = cane()
                if item then
                    stop_moving()
                    equip_until = equip_until or now + 6
                    local why
                    equip_receipt, why = ctx.equipment:equip(self, item)
                    if equip_receipt then
                        borrowed, cane_checked = { item = item, original = original }, true
                        return "running"
                    elseif why == "busy" and now < equip_until then return "running" end
                end
                cane_checked, equip_until = true, nil
            end
            self.name = base_name .. (mode == "trail" and string.format(" · %d", route.size)
                or mode == "route" and string.format(" · %d/%d", completed + 1, completed + #legs) or "")
            -- A moving leader need not get closer: advancing toward its previous goal also counts.
            -- Platform-relative checkpoints exclude boat drift; circles around a fixed goal make no progress.
            if progress_key ~= key or best_error == nil or remaining < best_error - .25 or approaching then
                progress_key, best_error, progress_at = key, remaining, now
            elseif now - progress_at >= 8 then
                return "failed", ctx:text("接近或推挤无进展，已停止", "No approach or push progress; stopped")
            end
            if ticket and ticket.failed then return "failed", ctx:text("原生移动失败", "Native movement failed") end
            if movement.mode ~= "steer" and player.components.playercontroller:IsBusy() then return "running" end
            -- Native WALKTO stops at every waypoint. Advancing the trail cannot wait for retarget throttling.
            local next_waypoint = (mode == "trail" or mode == "route") and key ~= target and key ~= destination_key
            if movement.continuous or next_waypoint or not next_update or now >= next_update then
                next_update = now + .5
                local submitted = destination and destination:GetPosition()
                if movement.continuous or next_waypoint or not submitted or distance(submitted, point) > .25 or (ticket and ticket.succeeded) then
                    local next_ticket, failure = movement:move(point, now, ticket)
                    if next_ticket == false then return "failed", failure end
                    if next_ticket then
                        if ticket and ticket ~= next_ticket then ticket:close() end
                        ticket, destination, destination_key = next_ticket, capture(point), key
                    end
                end
            end
            return "running"
        end
        function job:cancel(reason)
            self.closed, self.edit_owner = true, nil
            kill_search()
            movement:stop(ticket, reason)
            if owner.route_job == self then owner.route_job = nil; discard_preview() end
        end
        local started, reason = ctx.executor:start(job)
        if started and mode == "route" then self.route_job = job; discard_preview() end
        return started, reason
    end
    function self:walk(point)
        if not point_valid(point) then return fail("请选择有效位置", "Choose a valid position") end
        return start(point, nil, "route", { capture(point) })
    end
    function self:pause_route(owner)
        return owner ~= nil and self.route_job ~= nil and self.route_job:pause(owner)
    end
    function self:resume_route(owner)
        if self.route_job then self.route_job:resume(owner) end
    end
    function self:is_editing(job, owner)
        local current = self.route_job
        return current ~= nil and (job == nil or current == job) and current.edit_owner ~= nil
            and (owner == nil or current.edit_owner == owner)
    end
    function self:add_waypoint(point)
        if ctx.config.travel == false then return fail("移动辅助已关闭", "Travel assistance is disabled") end
        if not point_valid(point) then return fail("请选择有效位置", "Choose a valid position") end
        local movement = self.route_job and self.route_job.movement or Movement.new(ctx, point, true)
        if not explored(point) or not movement:passable(point) then
            return fail("航点必须位于已探索且可通行的位置", "Waypoints must be explored and passable")
        end
        local queue = self.route_job and self.route_job.waypoints or waypoints
        if #queue >= 64 then return fail("航点已达到 64 个上限", "Waypoint limit reached (64)") end
        queue[#queue + 1] = movement:capture(point)
        return true, ctx:text("已添加航点：", "Waypoint added: ") .. #queue
    end
    function self:clear_waypoints() waypoints = {}; discard_preview() end
    -- MapScreen updates while autopaused. This method only reads the world and computes points.
    function self:update_preview()
        local job = self.route_job
        if ctx.config.travel == false or job and not job.edit_owner then discard_preview(); return end
        local queue = job and job.waypoints or waypoints
        if #queue == 0 then discard_preview(); return end
        local goal = queue[1]:GetPosition()
        if not goal then discard_preview(); return end
        local movement = job and job.movement or Movement.new(ctx, goal, true)
        if not movement:valid() then discard_preview(); return end
        if not preview_matches(movement, queue) then
            discard_preview()
            local origin = movement:position()
            preview = { origin = { x = origin.x, z = origin.z }, goals = {}, legs = {}, path = {},
                movement = movement, caps = movement:capabilities(),
                fast = ctx.params.travel_fast == true, avoid = ctx.params.travel_avoid == true }
            for _, record in ipairs(queue) do
                local point = record:GetPosition()
                if not point then discard_preview(); return end
                preview.goals[#preview.goals + 1] = { x = point.x, z = point.z }
            end
        end
        if preview.reason or #preview.legs == #preview.goals then return end
        local index = #preview.legs + 1
        preview.planner = preview.planner or Planner.new(ctx, movement,
            preview.goals[index - 1] or preview.origin, preview.goals[index],
            { fast = preview.fast, avoid = preview.avoid, native = false })
        local state, points, reason = preview.planner:step()
        if state == "done" then
            preview.legs[index] = points
            for _, point in ipairs(points) do
                if #preview.path < 256 then preview.path[#preview.path + 1] = point
                else preview.truncated = true end
            end
            preview.planner = nil
        elseif state == "failed" then preview.reason, preview.planner = reason, nil end
    end
    function self:navigate(point, append)
        if append then return self:add_waypoint(point) end
        local points = {}
        for _, record in ipairs(waypoints) do
            local waypoint = record:GetPosition()
            if not waypoint then return fail("航点所在平台已消失", "A waypoint platform disappeared") end
            points[#points + 1] = record
        end
        if point then
            if not point_valid(point) then return fail("请选择有效位置", "Choose a valid position") end
            points[#points + 1] = capture(point)
        end
        if #points == 0 then return fail("请先选择航点", "Select waypoints first") end
        if #points > 64 then return fail("航点已达到 64 个上限", "Waypoint limit reached (64)") end
        local ok, reason = start(points[1]:GetPosition(), nil, "route", points)
        if ok then waypoints = {} end
        return ok, reason
    end
    function self:route_snapshot()
        local job = self.route_job
        local snapshot = { active = job ~= nil, paused = self:is_editing(), completed = job and job.completed or 0,
            phase = job and job.phase or (#waypoints > 0 and "planned" or "idle"), points = {}, path = {} }
        local position = job and job.movement:position() or player:GetPosition()
        snapshot.origin = { x = position.x, z = position.z }
        if job then snapshot.path, snapshot.truncated = job:path_snapshot() end
        if preview and (not job or job.edit_owner) then
            snapshot.path, snapshot.preview_legs = {}, #preview.legs
            snapshot.truncated, snapshot.reason = preview.truncated, preview.reason
            for _, point in ipairs(preview.path) do snapshot.path[#snapshot.path + 1] = { x = point.x, z = point.z } end
        end
        for _, record in ipairs(job and job.waypoints or waypoints) do
            local point = record:GetPosition()
            snapshot.points[#snapshot.points + 1] = point and { x = point.x, z = point.z } or { invalid = true }
        end
        return snapshot
    end
    function self:route_status()
        if self.route_job then return self.route_job.name end
        return #waypoints > 0 and ctx:text("待行航点：", "Planned waypoints: ") .. #waypoints or ""
    end
    function self:follow(target, mode)
        if not self:can_follow(target) then return fail("请选择附近可见的玩家或生物", "Select a nearby visible player or creature") end
        mode = mode or ctx.params.follow_mode
        if mode ~= "trail" and mode ~= "direct" then return fail("跟随模式不可用", "Follow mode is unavailable") end
        return start(target:GetPosition(), target, mode)
    end
    function self:push(target)
        if not self:can_push(target) then return fail("目标不具备可推挤的碰撞条件", "Target does not have suitable collision physics") end
        return start(target:GetPosition(), target, "push")
    end
    function self:handle_pointer(button)
        local input, controller = G.TheInput, player.components.playercontroller
        if button ~= G.MOUSEBUTTON_LEFT or not ctx.config.travel or input:ControllerAttached()
            or not input:IsKeyDown(G.KEY_SHIFT) or input:IsKeyDown(G.KEY_ALT)
            or G.TheFrontEnd:GetActiveScreen() ~= ctx.hud or input:GetHUDEntityUnderMouse()
            or not ctx:ready() or player.replica.inventory:GetActiveItem()
            or controller.placer or controller.deployplacer or controller.draggingonground
            or controller:IsAOETargeting() then return false end
        local target = input:GetWorldEntityUnderMouse()
        local push = input:IsKeyDown(G.KEY_CTRL)
        if not self:can_follow(target) or push and not self:can_push(target) then return false end
        local ok, why
        if push then ok, why = self:push(target) else ok, why = self:follow(target) end
        if not ok and why then ctx:notify(why) end
        return true
    end
    return self
end

return M
