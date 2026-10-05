local Inventory = require("fieldkit/game/inventory")
local M = {}

function M.attach(ctx)
    local G, controller = ctx.G, ctx.player.components.playercontroller
    if controller and controller.IsControlPressed then
        ctx.scope:wrap(controller, "IsControlPressed", function(original)
            return function(object, control, ...)
                if ctx._queue_query and (control == G.CONTROL_FORCE_INSPECT or control == G.CONTROL_FORCE_ATTACK
                    or control == G.CONTROL_FORCE_TRADE or control == G.CONTROL_FORCE_STACK) then return false end
                return original(object, control, ...)
            end
        end)
    end
    if controller and controller.IsAxisAlignedPlacement then
        ctx.scope:wrap(controller, "IsAxisAlignedPlacement", function(original)
            return function(object, ...)
                if ctx._queue_query then return false end
                return original(object, ...)
            end
        end)
    end
    if controller and controller.EncodeControlMods then
        ctx.scope:wrap(controller, "EncodeControlMods", function(original)
            return function(object, ...)
                if ctx._queue_query then return nil end
                return original(object, ...)
            end
        end)
    end
end

local function handle(ctx, action)
    local player, listeners = ctx.player, {}
    local ticket = { action = action, controller = player.components.playercontroller, sent = false, closed = false }
    function ticket:listen(event, fn, source)
        local function receive(...)
            if self.sent and not self.closed then return fn(...) end
        end
        player:ListenForEvent(event, receive, source)
        listeners[#listeners + 1] = { event, receive, source }
    end
    function ticket:close()
        if self.closed then return end
        self.closed = true
        for _, entry in ipairs(listeners) do player:RemoveEventCallback(entry[1], entry[2], entry[3]) end
        listeners = {}
    end
    local item = action and action.invobject
    ticket.usage_before, ticket.usage_resolution = Inventory.usage(item)
    local percent = ticket.usage_before
    if percent ~= nil then
        ticket:listen("percentusedchange", function(_, data)
            local value = data and data.percent
            if type(value) == "number" and value >= 0 and value <= 1 then percent = value end
        end, item)
    end
    function ticket:usage()
        -- Keep the final event when the consumed item has already disappeared.
        percent = Inventory.usage(item) or percent
        return percent
    end
    function ticket:consumed(evidence, unchanged)
        if self.failed then return false end
        if self.succeeded then return true end
        local current = self:usage()
        evidence = evidence or current ~= nil and self.usage_before ~= nil and current < self.usage_before
        if not self.usage_resolution then return evidence == true end
        -- Rounded percentages can hide a use. Drain its server echo before the next
        -- request; the feature supplies the required range, ownership and idle checks.
        return self.attempt_hint == true and (evidence == true or unchanged == true
            and current == self.usage_before and current > 0 and item:IsValid() and not item:HasTag("usesdepleted"))
    end
    -- Execution evidence must still be paired with the feature's observed result.
    function ticket:performed()
        if self.failed then return false end
        if self.succeeded then return true end
        local attempt = self.attempt
        if not attempt then return false end
        if attempt.identified or attempt.in_range == true then return true end
        if attempt.in_range == false or not attempt.target_position or not attempt.arrival then return false end
        local point, position = attempt.target_position, attempt.position
        return (position.x - point.x)^2 + (position.z - point.z)^2 <= attempt.arrival ^ 2
    end
    ticket:listen("actionfailed", function(_, data)
        if ticket.action and data and data.action == ticket.action then ticket.failed = true end
    end)
    return ticket
end

function M.arrival(ctx, action)
    local player, target, definition = ctx.player, action.target, action.action
    if not target then return end
    local inventoryitem = target.components and target.components.inventoryitem
    local owner = inventoryitem and inventoryitem.GetGrandOwner and inventoryitem:GetGrandOwner() or target
    -- Match native GoToEntity: the step adds to collision radii, with explicit distance as a lower bound.
    local arrival = .15 + player:GetPhysicsRadius(0) + owner:GetPhysicsRadius(0)
    if action.arrivedist or definition.arrivedist then return action.arrivedist or definition.arrivedist end
    if action.distance or definition.distance then
        local collision = .15 + (player.Physics and player.Physics:GetRadius() or 0)
            + (owner.Physics and owner.Physics:GetRadius() or 0)
        return math.max(collision, action.distance or definition.distance)
    end
    if definition.extra_arrive_dist then
        local destination = { GetPoint = function() return target.Transform:GetWorldPosition() end }
        arrival = arrival + definition.extra_arrive_dist(player, destination, action, arrival)
    end
    return math.max(arrival, definition.mindistance or 0)
end

function M.query(ctx, fn, rotation)
    local previous = ctx._queue_query
    local controller = ctx.player.components.playercontroller
    local placer = controller and controller.deployplacer
    if controller and rotation ~= nil then
        -- The native point picker reads orientation from the controller's preview.
        -- Supply this query's captured orientation even after that preview has gone.
        controller.deployplacer = { Transform = { GetRotation = function() return rotation end } }
    end
    ctx._queue_query = true
    local ok, value = pcall(fn)
    ctx._queue_query = previous
    if controller and rotation ~= nil then controller.deployplacer = placer end
    if not ok then error(value) end
    return value
end

function M.candidates(ctx, target, button, item, point, rotation)
    local picker = ctx.player.components.playeractionpicker
    if not picker or (target and not target:IsValid()) then return {} end
    point = point or (target and target:GetPosition())
    return M.query(ctx, function()
        if item then
            return target and picker:GetEquippedItemActions(target, item, button == "right")
                or picker:GetPointActions(point, item, button == "right")
        end
        return button == "left" and picker:GetLeftClickActions(point, target)
            or picker:GetRightClickActions(point, target)
    end, rotation) or {}
end

function M.pick(ctx, target, allowed, preferred)
    for _, button in ipairs(preferred and { preferred } or { "left", "right" }) do
        local candidates = M.candidates(ctx, target, button)
        for _, action in ipairs(candidates or {}) do
            if action.target == target and allowed[action.action.id] then
                return action, button
            end
        end
    end
end

function M.build(ctx, recipe, point, rotation, skin)
    local player, builder = ctx.player, ctx.player.replica.builder
    local controller = player.components.playercontroller
    if not builder or controller:IsBusy() or builder:IsBusy() then return nil, "Builder is busy" end
    local station = recipe.manufactured and builder:GetCurrentPrototyper()
    if recipe.manufactured and not (station and station:IsValid() and station:HasTag("prototyper")) then
        return nil, ctx:text("制造站尚未就绪", "Manufacturing station is not ready")
    end
    local inv, sources = player.replica.inventory, {}
    local function ingredients()
        for owner in pairs(sources) do
            if not Inventory.readable(ctx, owner) or Inventory.busy(ctx, owner) then return end
            for slot = 1, owner:GetNumSlots() do
                local item = owner:GetItemInSlot(slot)
                if item and Inventory.stack_size(item) == nil then return end
            end
        end
        local counts = {}
        for _, ingredient in ipairs(recipe.ingredients or {}) do
            if ingredient.amount > 0 then
                local _, count = inv:Has(ingredient.type, 1, false)
                counts[ingredient.type] = count
            end
        end
        return counts
    end
    local expected_ingredients
    if station and not controller.ismastersim then
        if builder:IsFreeBuildMode() then
            return nil, ctx:text("远端制造需要实际耗料，请先关闭免费制作", "Remote manufacturing requires material consumption; disable free crafting")
        end
        local shared_message = ctx:text("请先关闭含配料的容器，并将材料放入物品栏或背包",
            "Close containers holding ingredients and move the materials into your inventory or backpack")
        local busy_message = ctx:text("制作库存仍在同步", "Crafting inventory is still synchronizing")
        sources[inv] = true
        local overflow = inv:GetOverflowContainer()
        if overflow then sources[overflow] = true end
        -- Native crafting consumes open shared containers first; their changes cannot identify our request.
        for entity in pairs(inv:GetOpenContainers() or {}) do
            local owner = entity.replica.container or entity.replica.inventory
            if owner and owner ~= overflow and not owner.excludefromcrafting
                and not (owner.IsReadOnlyContainer and owner:IsReadOnlyContainer()) then
                if not Inventory.readable(ctx, owner) or Inventory.busy(ctx, owner) then return nil, busy_message end
                for _, ingredient in ipairs(recipe.ingredients or {}) do
                    if ingredient.amount > 0 and owner:Has(ingredient.type, 1, true) then return nil, shared_message end
                end
            end
        end
        expected_ingredients = ingredients()
        if not expected_ingredients then return nil, busy_message end
        if next(expected_ingredients) == nil then return nil, shared_message end
        for _, ingredient in ipairs(recipe.ingredients or {}) do
            if ingredient.amount > 0 then
                local amount = math.max(1, ctx.G.RoundBiasedUp(ingredient.amount * builder:IngredientMod()))
                local count = expected_ingredients[ingredient.type]
                if count < amount then return nil, shared_message end
                expected_ingredients[ingredient.type] = count - amount
            end
        end
    end
    local ticket = handle(ctx)
    ticket.recipe, ticket.station = recipe, station
    local product = recipe.product or recipe.name
    local before_product = Inventory.count(ctx, product)
    local before_buffered = point and builder:IsBuildBuffered(recipe.name)
    local before_entities = {}
    local origin = not controller.ismastersim and (point or player:GetPosition())
    local function nearby_products()
        return origin and ctx.G.TheSim:FindEntities(origin.x, 0, origin.z, 6, nil, { "INLIMBO" }) or {}
    end
    for _, entity in ipairs(nearby_products()) do
        if entity.prefab == product then before_entities[entity] = true end
    end
    function ticket:product_ready()
        if station and not self:result_ready() then return false end
        local count = Inventory.count(ctx, product)
        return (self.succeeded or self.build_hint) and before_product ~= nil and count ~= nil and count > before_product or false
    end
    function ticket:result_ready()
        if station then
            if self.failed then return false end
            if not station:IsValid() then self.failed = true; return false end
            local current = not controller.ismastersim and builder:GetCurrentPrototyper()
            if current and current ~= station then
                self.failed = true
                self.failure = ctx:text("制造站发生改变，无法确认结果，已停止", "Manufacturing station changed; result cannot be confirmed, stopped")
                return false
            end
            if not station:HasTag("prototyper") then self.manufacturing = true end
            local accepted = self.succeeded
            if not controller.ismastersim and self.build_hint and self.manufacturing then
                local counts = ingredients()
                accepted = counts ~= nil
                if accepted then
                    for prefab, count in pairs(expected_ingredients) do
                        if counts[prefab] ~= count then accepted = false; break end
                    end
                end
            end
            -- Native factories remove this tag while processing; ovens expose their result as pickable.
            return accepted and self.manufacturing
                and (station:HasTag("prototyper") or station:HasTag("pickable")) or false
        end
        if self.succeeded then return true end
        if not self.build_hint then return false end
        if self:product_ready() or before_buffered and not builder:IsBuildBuffered(recipe.name) then return true end
        -- Full inventories drop crafted products on the ground instead of increasing item counts.
        for _, entity in ipairs(nearby_products()) do
            if entity.prefab == product and not before_entities[entity] and entity:IsValid() then return true end
        end
        return false
    end
    if controller.ismastersim then
        local function built(_, data)
            if data and data.recipe and data.recipe.name == recipe.name then ticket.succeeded = true end
        end
        ticket:listen("builditem", built)
        ticket:listen("buildstructure", built)
    else
        -- The network event carries no recipe or action identity; require replicated result evidence.
        ticket:listen("buildsuccess", function() ticket.build_hint = true end)
        if station then ticket:listen("performaction", function(_, data)
            if data == nil then ticket.build_hint = true end
        end) end
    end
    local before = player:GetBufferedAction()
    ticket.sent = true
    local ok, err
    if point then ok, err = pcall(builder.MakeRecipeAtPoint, builder, recipe, point, rotation or 0, skin)
    else ok, err = pcall(builder.MakeRecipeFromMenu, builder, recipe, skin) end
    if not ok then ticket:close(); return nil, tostring(err) end
    local action = player:GetBufferedAction()
    if action and action ~= before and action.recipe == recipe.name then
        ticket.action = action
        if station and controller.ismastersim then
            action:AddSuccessAction(function()
                if not ticket.closed then
                    -- Native tech scanning may switch stations during the BUILD animation.
                    station = builder:GetCurrentPrototyper()
                    ticket.station, ticket.manufacturing, ticket.succeeded = station, nil, true
                end
            end)
            action:AddFailAction(function() if not ticket.closed then ticket.failed = true end end)
        end
        for _, key in ipairs({ "preview_cb", "non_preview_cb" }) do
            local send = action[key]
            if send then action[key] = function(...) if not ticket.closed then return send(...) end end end
        end
    end
    return ticket
end

function M.dispatch(ctx, action, button)
    local G, player = ctx.G, ctx.player
    local controller = player.components.playercontroller
    if not controller or not controller:IsEnabled() or controller:IsBusy() then
        return nil, ctx:text("角色当前无法行动", "Player cannot act now")
    end
    if not action or action.doer ~= player or not action:IsValid() then
        return nil, ctx:text("动作已失效", "Action is no longer valid")
    end
    local ticket = handle(ctx, action)

    if controller.ismastersim then
        action:AddSuccessAction(function()
            if not ticket.closed then ticket.succeeded = true end
        end)
        action:AddFailAction(function()
            if not ticket.closed then ticket.failed = true end
        end)
        ticket.sent = true
    else
        -- Local events identify the attempt; player_classified strips identity from its network echo.
        ticket:listen("performaction", function(_, data)
            if data == nil then ticket.attempt_hint = true
            elseif data.action == action then ticket.attempted = true
            else return end
            local target = action.target
            local valid_target = target and target:IsValid()
            ticket.attempt = { at = G.GetTime(), position = player:GetPosition(), identified = data ~= nil,
                target_position = valid_target and target:GetPosition() or nil,
                arrival = valid_target and data == nil and not action.action.rangecheckfn and M.arrival(ctx, action) or nil }
            if valid_target and action.action.rangecheckfn then
                ticket.attempt.in_range = action.action.rangecheckfn(player, target) == true
            end
        end)
        local function send()
            if ticket.closed or ticket.sent then return end
            if button == "inventory" then
                ticket.sent = true
                G.SendRPCToServer(G.RPC.ControllerUseItemOnItemFromInvTile,
                    action.action.code, action.target, action.invobject, action.action.mod_name)
                return
            end
            local point = action:GetActionPoint() or (action.target and action.target:GetPosition())
            if not point then return end
            local platform, x, z = controller:GetPlatformRelativePosition(point.x, point.z)
            ticket.sent = true
            local noforce = controller.locomotor == nil and action.action.canforce or nil
            -- The server rebuilds DROP from its picker; the fourth modifier bit selects one item.
            local modifiers = action.action == G.ACTIONS.DROP and action.options
                and action.options.wholestack == false and 8 or 0
            if button == "right" then
                G.SendRPCToServer(G.RPC.RightClick, action.action.code, x, z, action.target,
                    action.rotation ~= 0 and action.rotation or nil, true, modifiers, noforce,
                    action.action.mod_name, platform, platform ~= nil)
            else
                G.SendRPCToServer(G.RPC.LeftClick, action.action.code, x, z, action.target,
                    true, modifiers, noforce, action.action.mod_name, platform, platform ~= nil)
            end
        end
        action.non_preview_cb = send
        if action.action ~= G.ACTIONS.WALKTO then action.preview_cb = send end
    end
    local ok, err
    if button == "inventory" then
        -- Match native inventory use without DoAction's automatic hand-equipment switching.
        ok, err = pcall(function()
            if controller.ismastersim then controller.locomotor:PushAction(action, true)
            elseif controller.locomotor then
                if controller:CanLocomote() then controller.locomotor:PreviewAction(action, true) end
            else
                if action.action.pre_action_cb then action.action.pre_action_cb(action) end
                action.non_preview_cb()
            end
        end)
    else ok, err = pcall(controller.DoAction, controller, action) end
    if not ok then ticket:close(); return nil, tostring(err) end
    if action.action == G.ACTIONS.WALKTO and controller.locomotor
        and controller.locomotor.bufferedaction ~= action and player.bufferedaction ~= action
        and not ticket.succeeded then
        ticket:close()
        return nil, ctx:text("原生控制器未接收移动请求", "Native controller did not accept movement")
    end
    return ticket
end

function M.walk(ctx, point, previous)
    local action = ctx.G.BufferedAction(ctx.player, nil, ctx.G.ACTIONS.WALKTO,
        nil, ctx.G.Vector3(point.x, 0, point.z))
    local controller = ctx.player.components.playercontroller
    -- DoAction silently drops duplicate destinations. Keep the ticket that owns the actual action.
    if previous and not previous.closed and not previous.failed and previous.controller == controller
        and previous.action.pos and previous.action.pos == action.pos
        and ((controller.locomotor and controller.locomotor.bufferedaction == previous.action)
            or ctx.player.bufferedaction == previous.action) then
        return previous
    end
    return M.dispatch(ctx, action)
end

function M.stop(ctx, ticket, reason)
    if not ticket then return end
    ticket:close()
    if reason == "done" then return end
    local controller, player = ticket.controller, ctx.player
    local locomotor = controller.locomotor
    -- A newer local action owns input even if the previous RPC has no local locomotor.
    local superseded = player.bufferedaction and player.bufferedaction ~= ticket.action
        or locomotor and locomotor.bufferedaction and locomotor.bufferedaction ~= ticket.action
    local owns_locomotor = ticket.action and locomotor and locomotor.bufferedaction == ticket.action
    local owns_player = ticket.action and player.bufferedaction == ticket.action
    local owns = owns_locomotor or owns_player
    if owns_locomotor then locomotor:Stop(); locomotor:Clear() end
    if owns_player then player:ClearBufferedAction() end
    -- Manual control is already taking over; never send a late corrective action.
    if superseded or reason == "manual" or reason == "invalid" or not player:IsValid() then return end
    if owns or (not controller.ismastersim and not locomotor and ticket.sent) then
        controller:ClearActionHold()
        if not controller.ismastersim then
            controller:RemoteStopWalking()
            if not locomotor then
                -- Reassert the current mode: native reset clears the server path even with a cursor item.
                ctx.G.SendRPCToServer(ctx.G.RPC.SetMovementPredictionEnabled, false)
            end
        end
    end
end

return M
