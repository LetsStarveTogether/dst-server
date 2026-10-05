local Parameters = require("fieldkit/core/parameters")
local Actions = require("fieldkit/game/actions")
local Inventory = require("fieldkit/game/inventory")
local Query = require("fieldkit/game/query")
local Rules = require("fieldkit/rules/work")
local M = {}

function M.new(ctx)
    local self = {}
    local self_owner = self
    local player, G = ctx.player, ctx.G
    local inventory, controller = player.replica.inventory, player.components.playercontroller
    local limit = Parameters.LIMIT

    local function hands(item)
        local equip = item and item.replica and item.replica.equippable
        return equip and equip:EquipSlot() == G.EQUIPSLOTS.HANDS
    end
    local function acceptable(action, target, id)
        return Rules.accept(action, player) and action.target == target and (not id or action.action.id == id)
    end
    local function resolve(target, button, id)
        if not Query.visible(ctx, target) or target == player or target:HasTag("player") then return end
        local exhausted
        local function usable(action, tools_only)
            if not acceptable(action, target, id) then return false end
            local policy = Rules.policies[action.action.id]
            if tools_only and not policy.tool then return false end
            if policy.tool and not policy.refills and action.invobject and action.invobject:HasTag("usesdepleted") then
                exhausted = "exhausted"
                return false
            end
            return true
        end
        for _, action in ipairs(Actions.candidates(ctx, target, button)) do
            if usable(action) then return action end
        end
        -- An active consumable is the player's explicit intent; never substitute another item.
        if inventory:GetActiveItem() or ctx.config.queue_tools == false then return nil, exhausted end
        local picker = player.components.playeractionpicker
        for _, entry in ipairs(Inventory.items(ctx)) do
            local item = entry.item
            local candidates = Actions.query(ctx, function()
                return picker:GetUseItemActions(target, item, button == "right")
            end) or {}
            for _, action in ipairs(candidates) do
                if usable(action, true) then return action end
            end
            for _, action in ipairs(Actions.candidates(ctx, target, button, item)) do
                if usable(action, true) then return action end
            end
        end
        if not ctx.config.queue_craft_tools then return nil, exhausted end
        -- No synthetic action is dispatched. This only advertises a preparable intent;
        -- the newly crafted tool must pass the native picker again before use.
        local craft_actions = {}
        for _, tool_id in ipairs(Rules.craftable) do
            local action = G.ACTIONS[tool_id]
            local valid = action and (tool_id == "FISH" and target:HasTag("fishable")
                or target.IsActionValid and target:IsActionValid(action, button == "right"))
            if valid and (not id or id == tool_id) then craft_actions[#craft_actions + 1] = action end
        end
        table.sort(craft_actions, function(a, b)
            if (a.priority or 0) ~= (b.priority or 0) then return (a.priority or 0) > (b.priority or 0) end
            return a.id < b.id
        end)
        for _, action in ipairs(craft_actions) do
            for _, name in ipairs(Rules.policies[action.id].recipes) do
                local recipe = Inventory.craft_step(ctx, name)
                if recipe then return { target = target, action = action, recipe_to_make = recipe } end
            end
        end
        return nil, exhausted
    end
    local function capture(target, button, id)
        local action = resolve(target, button, id)
        if not action then return end
        -- REEL is a phase of an explicitly selected pond, never an independent repeated action.
        if action.action.id == "REEL" then return end
        local active = inventory:GetActiveItem()
        local policy = Rules.policies[action.action.id]
        return { target = target, button = button, id = action.action.id,
            remaining = action.action.id == "FISH" and ctx.params.fish_count or nil,
            applications = policy.mode == "count" and action.invobject and (ctx.params.repeat_count or 1) or nil,
            clear_stump = action.action.id == "CHOP" and ctx.params.clear_stumps == true,
            cursor = active and not (policy.tool and hands(active)) and active.prefab or nil }
    end
    local function forget(target)
        if ctx.selection and ctx.selection.forget then ctx.selection:forget(target) end
    end
    function self:preview(target, button)
        local action = resolve(target, button == "right" and "right" or "left")
        return action and action.action.id ~= "REEL" and action or nil
    end

    local function settled()
        if controller:IsBusy() then return false end
        if player.sg then return player.sg:HasStateTag("idle") or player.sg:HasStateTag("moving") end
        return controller.ismastersim or player:HasTag("idle") or player:HasTag("moving")
    end

    local function new_job(entries)
        local active = inventory:GetActiveItem()
        local job = { entries = entries, completed = 0, skipped = 0, caught = 0, applied = 0, timeout = 600,
            name = ctx:text("实体交互队列", "Interaction queue"),
            input = { mode = "world", cursor = active and active.prefab, placement = active ~= nil },
            timeout_message = ctx:text("交互队列已达到时限", "Interaction queue reached its time limit"),
            error_message = ctx:text("交互队列发生错误，已停止", "Interaction queue failed; stopped") }
        local state
        local function close(reason)
            if state then Actions.stop(ctx, state.ticket, reason) end
        end
        local function pond_rod()
            local item = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
            return item and item.replica and item.replica.fishingrod
        end
        local function cancel_fishing(reason)
            if not state or not state.fishing or reason == "manual" or reason == "invalid" then return end
            local item = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
            local rod = pond_rod()
            local pending = player:GetBufferedAction()
            if not Inventory.equipped(ctx, state.fish_item) or inventory:GetActiveItem()
                or rod and rod:GetTarget() and rod:GetTarget() ~= state.entry.target
                or pending and (not state.ticket or pending ~= state.ticket.action) then
                if state.ticket then state.ticket:close(); state.ticket = nil end
                return
            end
            if not rod or rod:GetTarget() ~= state.entry.target or rod:HasCaughtFish() or controller:IsBusy() then return end
            if rod:HasHookedFish() or player:HasTag("nibble") then
                -- REEL hooks/pulls here. Native UNEQUIP stops fishing and its rod-loss timer.
                local receipt = ctx.equipment:unequip(job, item, ctx.executor.finishing == job)
                if receipt then
                    if state.ticket then state.ticket:close(); state.ticket = nil end
                    if receipt.cursor then
                        ctx:notify(ctx:text("库存已满，正在将鱼竿收至鼠标", "Inventory is full; moving the fishing rod to the cursor"))
                    end
                else
                    ctx:notify(ctx:text("无法安全收起鱼竿，请手动移动停止钓鱼",
                        "The fishing rod cannot be put away safely; move manually to stop fishing"))
                end
            else
                local action = resolve(state.entry.target, state.entry.button, "REEL")
                if action then
                    -- A cancellation command replaces the old ticket; generic movement cleanup
                    -- must not send another action after the native inventory/REEL request.
                    if state.ticket then state.ticket:close(); state.ticket = nil end
                    local ticket = Actions.dispatch(ctx, action, state.entry.button)
                    -- Native REEL is instant and has no client SG handler: both predictive
                    -- and non-predictive controllers send it synchronously in DoAction.
                    if ticket then ticket:close() end
                end
            end
        end
        local function dropped(point)
            local found = {}
            for _, entity in ipairs(G.TheSim:FindEntities(point.x, point.y or 0, point.z, 4,
                { "_inventoryitem" }, { "INLIMBO", "NOCLICK" })) do
                if Query.visible(ctx, entity) then found[entity.GUID or entity] = entity end
            end
            return found
        end
        local function fish_drop(target)
            return target.prefab == "pondfish" or target.prefab == "pondeel"
        end
        local function new_item(identity, target, fishing)
            -- Local age can rule out an already observed entity, including one outside
            -- the first search circle. A newly replicated old entity remains ambiguous.
            return not state.drops[identity] and (fishing
                or target:GetTimeAlive() <= G.GetTime() - state.started_at)
        end
        local function new_drop()
            if not state.drops then return false end
            for identity, target in pairs(dropped(state.drop_point)) do
                if new_item(identity, target) then return true end
            end
            return false
        end
        local function observe_execution()
            local ticket, target = state.ticket, state.entry.target
            if not ticket or not ticket.sent then return end
            local rangecheck = ticket.action and ticket.action.action.rangecheckfn
            if target:IsValid() and rangecheck and rangecheck(player, target) then
                -- Preserve a native in-range observation for an echo delivered after removal.
                state.arrival = math.max(state.arrival, math.sqrt(player:GetDistanceSqToInst(target)))
            end
            local attempt = ticket.attempt
            if attempt and attempt ~= state.attempt_seen then
                state.attempt_seen = attempt
                local point = attempt.target_position or state.drop_point
                if point then
                    local distance = (attempt.position.x - point.x)^2 + (attempt.position.z - point.z)^2
                    if attempt.identified or attempt.in_range == true
                        or attempt.in_range == nil and distance <= state.arrival ^ 2 then
                        state.execution = { position = attempt.position, ticket = ticket,
                            reach = math.max(state.arrival, math.sqrt(distance)) }
                        if state.drops then state.drop_point = point end
                    end
                end
            end
            if state.execution and target:IsValid() then
                local point, execution = target:GetPosition(), state.execution
                if (execution.position.x - point.x)^2 + (execution.position.z - point.z)^2 > execution.reach ^ 2 then
                    -- The target continued moving away from that attempt. Resume observing
                    -- the chase instead of applying its old anonymous echo at a new position.
                    state.execution = nil
                end
            end
        end
        local function track_drops()
            local target = state.entry.target
            if not state.drops or state.fishing or not Query.visible(ctx, target) then return end
            local point = target:GetPosition()
            if point.x == state.drop_point.x and point.z == state.drop_point.z then return end
            -- Chase observations grow the old-loot baseline. Once execution is observed,
            -- new items can precede removal in replication and must stay outside it.
            if not state.execution and not (state.ticket and state.ticket.succeeded) then
                for identity, item in pairs(dropped(point)) do state.drops[identity] = item end
            end
            state.drop_point = point
        end
        local function append_drops(fishing, after_current)
            if not state.drops or not (fishing or ctx.config.queue_collect) then return end
            local at = after_current and 2 or 1
            for identity, target in pairs(dropped(fishing and player:GetPosition() or state.drop_point)) do
                local queued = false
                for i = after_current and 2 or 1, #job.entries do
                    if job.entries[i].target == target then queued = true; break end
                end
                if not queued and new_item(identity, target, fishing) and #job.entries < limit
                    and (not fishing or fish_drop(target)) then
                    local entry = capture(target, "left", "PICKUP")
                    if entry then
                        if fishing then table.insert(job.entries, at, entry); at = at + 1
                        else job.entries[#job.entries + 1] = entry end
                    end
                end
            end
            state.drops = nil
        end
        local function new_fish_drop()
            for identity, target in pairs(dropped(player:GetPosition())) do
                if fish_drop(target) and state.drops and not state.drops[identity] then return true end
            end
            return false
        end
        local function return_tool()
            local active = inventory:GetActiveItem()
            if state.managed_cursor and (not active or (active.GUID or active) == state.managed_cursor) then
                -- Native ReturnActiveItem sends its RPC even before the borrowed cursor replicates.
                inventory:ReturnActiveItem()
                job.returning = true
            end
        end
        local function advance(success, reason, fishing)
            -- ponytail: one-second replication window; collecting arbitrarily late loot needs server receipts.
            if success and ctx.config.queue_collect and state.drops and not state.fishing then
                state.collect_at = state.collect_at or G.GetTime()
                if G.GetTime() - state.collect_at < 1 then return end
            end
            cancel_fishing(reason or "skipped")
            close(success and "done" or "skipped")
            table.remove(job.entries, 1)
            if success then
                local target = state.entry.target
                if state.entry.clear_stump and state.worked and target:IsValid() and target:HasTag("stump") then
                    local stump = capture(target, "right", "DIG") or capture(target, "left", "DIG")
                    if stump and #job.entries < limit then table.insert(job.entries, 1, stump) end
                end
                append_drops(fishing); job.completed = job.completed + 1
            else job.skipped = job.skipped + 1 end
            forget(state.entry.target)
            return_tool()
            state = nil
        end
        local function applied(now)
            local entry = state.entry
            if not entry.applications then advance(true); return end
            job.applied, entry.applications = job.applied + 1, entry.applications - 1
            state.applied = (state.applied or 0) + 1
            if entry.applications == 0 then advance(true); return end
            state.ticket:close()
            state.ticket, state.effect, state.success_seen = nil, nil, nil
            state.progress, state.count, state.usage = now, nil, nil
        end
        local function confirmed(now)
            local ticket = state.ticket
            if not ticket then return false end
            if ticket.succeeded then return true end
            if state.entry.applications and ticket.usage_resolution then
                local item = state.item
                return ticket:consumed(state.effect, state.execution ~= nil and state.execution.ticket == ticket
                    and now - state.sent_at >= .5 and settled()
                    and (Inventory.equipped(ctx, item) or inventory:GetActiveItem() == item))
            end
            return state.effect == true
        end
        local function progress(now)
            if state.entry.target:IsValid() then
                local distance = player:GetDistanceSqToInst(state.entry.target)
                if not state.distance or distance < state.distance - .25 then state.distance, state.progress = distance, now end
            end
            local ticket = state.ticket
            if not ticket or not ticket.sent then return end
            if ticket.succeeded and not state.success_seen then
                state.success_seen, state.progress = true, now
            end
            local usage = ticket:usage()
            local refills = Rules.policies[state.entry.id].refills
            if usage and state.usage and (refills and usage > state.usage
                or not refills and usage < state.usage) then
                state.effect, state.progress = true, now
            end
            if usage then state.usage = usage end
            if state.prefab and state.count and (not refills or state.usage == nil) then
                local count = Inventory.count(ctx, state.prefab)
                if count and (state.entry.id == "PICKUP" and count > state.count
                    or state.entry.id ~= "PICKUP" and count < state.count) then state.effect, state.progress = true, now end
                -- A consumed/broken item is one change, not fresh progress on every tick.
                if count then state.count = count end
            end
            if state.execution and (Rules.policies[state.entry.id].mode == "work" and state.entry.target:IsValid()
                and not Rules.workable(state.entry.target, G.ACTIONS[state.entry.id], state.entry.button)
                or not Query.visible(ctx, state.entry.target) and new_drop()) then
                state.effect, state.progress = true, now
            end
            if ticket.succeeded or state.effect then state.worked = true end
        end
        local function prepare(action, now, unavailable)
            if Inventory.busy(ctx, inventory) then return "wait" end
            local entry, item = state.entry, action and action.invobject
            local active = inventory:GetActiveItem()
            if entry.cursor and (not active or active.prefab ~= entry.cursor) then
                job.input.cursor, job.input.placement = entry.cursor, true
                if not state.preparing then
                    if not Inventory.refill(ctx, entry.cursor) then return "skip" end
                    state.preparing = { kind = "cursor", until_time = now + 6 }
                end
                return "wait"
            end
            if not action then
                if unavailable == "exhausted" then return "skip" end
                -- A filled/repaired target may finish before the requested maximum.
                -- Cursor replenishment above must complete before absence means completion.
                if state.applied and settled() then return "complete" end
                return "wait"
            end
            if action.recipe_to_make then
                if not state.preparing then
                    state.ticket = Actions.build(ctx, action.recipe_to_make)
                    if not state.ticket then return "wait" end
                    state.preparing = { kind = "craft", until_time = now + 8 }
                end
                return "wait"
            end
            if Rules.policies[entry.id].tool and item and not Inventory.equipped(ctx, item) then
                if ctx.config.queue_tools == false and active ~= item then return "skip" end
                if not state.preparing then
                    if hands(item) then
                        local receipt, why = ctx.equipment:equip(job, item)
                        if not receipt then return why == "busy" and "wait" or "skip" end
                        state.preparing = { kind = "equip", receipt = receipt, until_time = now + 6 }
                    else
                        job.input.cursor = item.prefab
                        state.managed_cursor = state.managed_cursor or active == nil and (item.GUID or item)
                        if active ~= item and not Inventory.refill(ctx, item.prefab) then return "skip" end
                        state.preparing = { kind = "cursor", until_time = now + 6 }
                    end
                end
                -- Non-equippable tools (razor) are native USEITEM actions on the cursor.
                if not hands(item) and inventory:GetActiveItem() == item then state.preparing = nil
                else return "wait" end
            end
            return "ready"
        end
        local function dispatch(action, now)
            if state.ticket then state.ticket:close() end
            state.arrival = Actions.arrival(ctx, action)
            state.item = action.invobject
            state.prefab = state.entry.id == "PICKUP" and state.entry.target.prefab
                or action.invobject and action.invobject.prefab
            state.usage = Inventory.usage(state.item)
            state.count = state.prefab and Inventory.count(ctx, state.prefab)
            state.effect, state.success_seen, state.sent_at = false, false, now
            state.ticket = Actions.dispatch(ctx, action, state.entry.button)
            if state.ticket then state.started_at = state.started_at or now end
            return state.ticket ~= nil
        end
        local function finish_fish()
            local entry = state.entry
            job.caught, entry.remaining = job.caught + 1, entry.remaining - 1
            job.name = ctx:text("实体交互队列 · 已钓 ", "Interaction queue · caught ") .. job.caught
            if entry.remaining == 0 then advance(true, "done", true); return end
            append_drops(true, true)
            close("done")
            -- One catch per turn prevents a selected pond from starving other queued work.
            table.remove(job.entries, 1)
            job.entries[#job.entries + 1] = entry
            state = nil
        end
        local function fish(now)
            local item = state.fish_item
            local rod = item and item:IsValid() and item.replica.fishingrod
            local phase = state.fishing
            local usage = Inventory.usage(item)
            local collected = phase == "pull" and (usage and state.fish_usage and usage < state.fish_usage
                or (not rod or not rod:GetTarget()) and new_fish_drop())
            if (rod and rod:HasCaughtFish() or collected) and phase ~= "collect" then
                state.fishing, state.fish_deadline = "collect", now + 8
            end
            if state.fishing == "collect" then
                if now >= state.fish_deadline then advance(false)
                elseif (not rod or not rod:GetTarget()) and settled() then
                    -- Collection clears replicated rod state before the last animation frame.
                    -- Allow the dropped fish to replicate before scheduling its native PICKUP.
                    state.collected_at = state.collected_at or now
                    if new_fish_drop() or now - state.collected_at >= 1 then finish_fish() end
                end
                return
            end
            if not rod or state.ticket and state.ticket.failed then advance(false); return end
            if rod:GetTarget() ~= state.entry.target then
                if phase ~= "cast" or now - state.progress > 8 then advance(false) end
                return
            end
            if phase == "cast" then
                state.fishing, state.fish_deadline = "wait", now + 45
                state.drops = dropped(player:GetPosition())
            end
            if now >= state.fish_deadline then advance(false); return end
            if controller:IsBusy() then return end
            local next_phase
            if rod:HasHookedFish() and state.fishing ~= "pull" then next_phase = "pull"
            elseif player:HasTag("nibble") and state.fishing == "wait" then next_phase = "hook" end
            if next_phase then
                local action = resolve(state.entry.target, state.entry.button, "REEL")
                if action and dispatch(action, now) then state.fishing, state.fish_deadline = next_phase, now + 6 end
            end
        end
        function job:step(now)
            if not ctx.equipment:ready(self) then return "running" end
            if self.returning then
                if inventory:GetActiveItem() or Inventory.busy(ctx, inventory) then
                    self.return_until = self.return_until or now + 6
                    if now >= self.return_until then return "failed", ctx:text("无法收回工具", "Could not return the tool to inventory") end
                    return "running"
                end
                self.returning, self.return_until = nil, nil
            end
            local entry = self.entries[1]
            if not entry then
                return "done", ctx:text("交互队列完成；成功/跳过：", "Interaction queue complete; completed/skipped: ")
                    .. self.completed .. "/" .. self.skipped
                    .. (self.caught > 0 and ctx:text("；钓获：", "; caught: ") .. self.caught or "")
                    .. (self.applied > 0 and ctx:text("；投入次数：", "; applications: ") .. self.applied or "")
            end
            if not state then state = { entry = entry, progress = now } end
            if state.ticket and state.ticket.failed then
                if entry.applications then return "failed", ctx:text("投入被拒绝，已停止", "Item use was rejected; stopped") end
                advance(false); return "running"
            end
            observe_execution()
            track_drops()
            progress(now)
            if not Query.visible(ctx, entry.target) or entry.target:HasTag("dead") then
                if entry.id == "FISH" then advance(false)
                elseif confirmed(now) then
                    if entry.applications then
                        self.applied = self.applied + 1
                        advance(true)
                    else advance(true) end
                elseif state.applied and not state.ticket then advance(true)
                elseif not state.ticket or not state.ticket.sent then advance(false)
                else
                    -- Entity removal and owner-inventory replication can arrive separately.
                    -- Disappearance by itself is never an action receipt.
                    state.lost_at = state.lost_at or now
                    if now - state.lost_at >= 1 then advance(false) end
                end
                return "running"
            end
            if state.fishing then
                if state.fishing == "cast" and now - state.progress > 8 then advance(false)
                else fish(now) end
                return "running"
            end
            if state.preparing then
                local pending = state.preparing
                if pending.kind == "equip" and pending.receipt:state() ~= "done" then advance(false); return "running" end
                if now >= pending.until_time then advance(false); return "running" end
                local ready = pending.kind == "equip" and pending.receipt:state() == "done"
                    or pending.kind == "cursor" and inventory:GetActiveItem() ~= nil
                    or pending.kind == "craft" and state.ticket and state.ticket:product_ready()
                if not ready then return "running" end
                if pending.kind == "craft" then state.ticket:close(); state.ticket = nil end
                state.preparing, state.progress = nil, now
            end
            if now - state.progress > 8 then advance(false); return "running" end
            local action, unavailable = resolve(entry.target, entry.button, entry.id)
            if state.ticket then
                if Rules.policies[entry.id].mode ~= "work" then
                    if confirmed(now) then applied(now); return "running" end
                    -- Changed native target availability is evidence only while the same item
                    -- remains usable. Cursor depletion itself cannot stand in for a receipt.
                    local same_item = not state.item or state.item:IsValid()
                        and (Inventory.equipped(ctx, state.item) or inventory:GetActiveItem() == state.item)
                    if not entry.applications and state.ticket.sent and state.execution and not action and same_item and not controller:IsBusy()
                        and now - state.sent_at >= .5 then advance(true) end
                    return "running"
                end
                if not action and not Rules.workable(entry.target, G.ACTIONS[entry.id], entry.button) then
                    if state.worked then advance(true)
                    else
                        state.lost_at = state.lost_at or now
                        if now - state.lost_at >= 1 then advance(false) end
                    end
                    return "running"
                end
                if not settled() or now - state.sent_at < .3
                    or not (state.ticket.succeeded or state.execution and state.execution.ticket == state.ticket
                        or state.effect) then return "running" end
                state.ticket:close(); state.ticket = nil
            end
            if not settled() then return "running" end
            local prepared = prepare(action, now, unavailable)
            if prepared == "complete" then advance(true); return "running" end
            if prepared == "skip" then
                if entry.applications then return "failed", ctx:text("材料不足或无法取用，已停止", "Materials are exhausted or unavailable; stopped") end
                advance(false); return "running"
            end
            if prepared == "wait" then return "running" end
            -- Equipment/cursor mutations can change action priority; always ask again.
            action = resolve(entry.target, entry.button, entry.id)
            if not action or action.recipe_to_make then advance(false); return "running" end
            if Rules.policies[entry.id].collect and not state.drops then
                state.drop_point = entry.target:GetPosition()
                state.drops = dropped(state.drop_point)
            end
            if dispatch(action, now) and entry.id == "FISH" then
                state.fishing, state.fish_deadline = "cast", now + 8
                state.fish_item, state.fish_usage = action.invobject, Inventory.usage(action.invobject)
            end
            return "running"
        end
        function job:revise(target, button)
            for i, entry in ipairs(self.entries) do
                if entry.target == target then
                    if entry.button == button then return true, entry end
                    local replacement = capture(target, button)
                    if not replacement then
                        return false, ctx:text("目标没有可执行的交互", "Target has no available interaction")
                    end
                    if i == 1 and state then
                        cancel_fishing("changed"); close("changed"); return_tool(); state = nil
                    end
                    self.entries[i] = replacement
                    return true, replacement
                end
            end
            return true
        end
        function job:remove(target)
            for i = #self.entries, 1, -1 do
                if self.entries[i].target == target then
                    if i == 1 and state then
                        cancel_fishing("removed"); close("removed"); return_tool(); state = nil
                    end
                    table.remove(self.entries, i)
                end
            end
        end
        function job:cancel(reason)
            cancel_fishing(reason)
            close(reason)
            for _, entry in ipairs(self.entries) do forget(entry.target) end
            if self == self_owner.job then self_owner.job = nil end
        end
        -- The job and feature have different receivers; keep their ownership explicit.
        return job
    end

    local function enqueue(entries, single)
        if single and ctx.config.ground == false or not single and ctx.config.work == false then
            return false, ctx:text("交互辅助已关闭", "Interaction assistance is disabled")
        end
        local ready, reason = ctx:ready()
        if not ready then return false, reason end
        local current = ctx.executor.current
        local radius = ctx.params.search_radius
        if current and current ~= self.job then
            return false, ctx:text("请先停止当前任务", "Stop the current operation first")
        end
        local selected, seen, limited, retained = {}, {}, false, false
        if current then for _, entry in ipairs(current.entries) do seen[entry.target] = true end end
        local remaining = current and limit - #current.entries or limit
        for _, supplied in ipairs(entries or {}) do
            local target = supplied.target
            if seen[target] then retained = true end
            if not seen[target] and Query.visible(ctx, target) and player:GetDistanceSqToInst(target) <= radius * radius then
                local entry = capture(target, supplied.button == "right" and "right" or "left", supplied.id)
                if entry then
                    if #selected >= remaining then limited = true; break end
                    selected[#selected + 1], seen[target] = entry, true
                end
            end
        end
        local limit_message = limited and (ctx:text("队列最多 ", "Queue limit: ") .. limit
            .. ctx:text(" 个目标；请分批处理", " targets; split the work")) or nil
        if #selected == 0 then
            if retained and not limited then return true end
            return false, limit_message or ctx:text("没有可加入队列的目标", "No targets can be added to the queue")
        end
        if current then
            for _, entry in ipairs(selected) do current.entries[#current.entries + 1] = entry end
            return true, limit_message
        end
        local job = new_job(selected)
        -- Set ownership before start; executor cancellation is synchronous.
        self.job = job
        local ok, message = ctx.executor:start(job)
        if not ok then self.job = nil end
        return ok, message or ok and limit_message or nil
    end
    function self:enqueue(entries) return enqueue(entries) end
    function self:revise(target, button)
        if self.job then return self.job:revise(target, button) end
        return true
    end
    function self:remove(target) if self.job then self.job:remove(target) end end
    function self:start(target, id)
        local radius = ctx.params.search_radius
        local entry = capture(target, "left", id)
        if not entry and id then entry = capture(target, "right", id) end
        if not entry then return false, ctx:text("目标没有可执行的交互", "Target has no available interaction") end
        local entries = { entry }
        -- Fish exactly the selected pond; similar nearby ponds are not an implicit intent.
        if entry.id ~= "FISH" then
            for _, candidate in ipairs(Query.nearby(ctx, radius, nil, nil, nil, function(entity)
                return entity ~= target and entity.prefab == target.prefab and resolve(entity, entry.button, entry.id) ~= nil
            end, limit)) do
                entries[#entries + 1] = { target = candidate, button = entry.button, id = entry.id }
            end
        end
        return enqueue(entries)
    end
    function self:pickup(target) return enqueue({ { target = target, button = "left", id = "PICKUP" } }, true) end
    return self
end

return M
