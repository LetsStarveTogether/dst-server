local Parameters = require("fieldkit/core/parameters")
local Actions = require("fieldkit/game/actions")
local Inventory = require("fieldkit/game/inventory")
local Query = require("fieldkit/game/query")
local Rules = require("fieldkit/rules/cooking")
local M = {}

local function valid(entity) return entity and entity:IsValid() end
local function matches(a, b)
    if not a or not b or #a ~= #b then return false end
    for i, prefab in ipairs(a) do if prefab ~= b[i] then return false end end
    return true
end

function M.new(ctx)
    local self, player, G = {}, ctx.player, ctx.G
    local inv, controller = player.replica.inventory, player.components.playercontroller
    local pending_memory, observed_buttons = {}, setmetatable({}, { __mode = "k" })
    local shortcut_widgets = setmetatable({}, { __mode = "k" })
    local function text(zh, en) return ctx:text(zh, en) end
    local function busy(owner) return Inventory.busy(ctx, owner) end
    local function readable(owner) return Inventory.readable(ctx, owner) end
    local function supported(pot)
        local container = valid(pot) and pot.replica and pot.replica.container
        return container and Rules.slots[pot.prefab] and container:GetNumSlots() == Rules.slots[pot.prefab]
            and not container:AcceptsStacks() and not container:IsReadOnlyContainer() and not pot:HasTag("burnt")
    end
    local function can_open(pot)
        return not pot:HasTag("mastercookware") or player:HasTag("masterchef")
    end
    local function ready(allow_running)
        if ctx.config.cooking == false then return false, text("烹饪辅助已关闭", "Cooking assistance is disabled") end
        local available, reason = ctx:ready()
        if not available then return false, reason end
        if not allow_running and ctx.executor:is_running() then
            return false, text("请先停止当前操作", "Stop the current operation first")
        end
        if not readable(inv) then return false, text("物品栏尚不可读", "Inventory is unavailable") end
        if inv:GetActiveItem() then return false, text("请先放下鼠标物品", "Put down the cursor item first") end
        if busy(inv) then return false, text("等待物品栏同步", "Wait for inventory updates") end
        return true
    end
    local function opened()
        local found
        for pot in pairs(inv:GetOpenContainers() or {}) do
            if supported(pot) and pot.replica.container:IsOpenedBy(player) then
                if found then return nil, text("请只打开一个厨具", "Leave only one cooking container open"), true end
                found = pot
            end
        end
        return found, found == nil and text("请打开一个原版厨具", "Open a supported cooking container") or nil
    end
    local function recipe_in(pot)
        local container = supported(pot) and pot.replica.container
        if not container or not container:IsOpenedBy(player) or busy(container) then return end
        local recipe = {}
        for slot = 1, Rules.slots[pot.prefab] do
            local item = container:GetItemInSlot(slot)
            if not valid(item) or Inventory.stack_size(item) ~= 1 or not container:CanTakeItemInSlot(item, slot) then return end
            recipe[slot] = item.prefab
        end
        return recipe
    end
    local function recipes()
        local saved, result = ctx.store:get("recipes"), {}
        for cooker in pairs(Rules.slots) do
            if type(saved) == "table" and Rules.valid_recipe(cooker, saved[cooker]) then result[cooker] = { unpack(saved[cooker]) } end
        end
        return result
    end
    local function remember(pot, recipe)
        if not ctx.store.ready then return false end
        local saved = recipes()
        saved[pot.prefab] = { unpack(recipe) }
        return ctx.store:set("recipes", saved) ~= false
    end
    local function started(pot)
        return supported(pot) and (pot:HasTag("donecooking") or not pot.replica.container:CanBeOpened())
    end
    local function current(allow_running)
        local available, reason = ready(allow_running)
        if not available then return nil, reason end
        local pot
        pot, reason = opened()
        if not pot then return nil, reason end
        if not pot.replica.container:CanBeOpened() or busy(pot.replica.container) then
            return nil, text("厨具尚不可操作", "Cooking container is not ready")
        end
        return pot
    end

    function self:remember()
        local pot, reason = current()
        if not pot then return false, reason end
        local recipe = recipe_in(pot)
        if not recipe then return false, text("请先放入一份完整配方", "Fill one complete recipe first") end
        if not remember(pot, recipe) then return false, text("配方未保存或记录尚未载入", "Recipe was not saved or storage is still loading") end
        return true, text("已记住此厨具的配方", "Recipe remembered for this cooking container")
    end
    function self:remember_inventory(source, start_slot)
        local pot, reason = current()
        if not pot then return false, reason end
        local owner = source == "inventory" and inv or source == "backpack" and inv:GetOverflowContainer()
        local count = Rules.slots[pot.prefab]
        start_slot = start_slot or 1
        if not readable(owner) or busy(owner) or type(start_slot) ~= "number" or start_slot ~= math.floor(start_slot)
            or start_slot < 1 or start_slot + count - 1 > owner:GetNumSlots() then
            return false, text("食材起始槽或来源不可用", "Ingredient source or starting slot is unavailable")
        end
        local recipe = {}
        for slot = 1, count do
            local entry = Inventory.slot(ctx, owner, start_slot + slot - 1)
            if not entry or not valid(entry.item) or entry.count < 1
                or not pot.replica.container:CanTakeItemInSlot(entry.item, slot) then
                return false, text("连续槽位必须放齐可用于此厨具的食材", "Consecutive slots must contain valid ingredients for this cooker")
            end
            recipe[slot] = entry.item.prefab
        end
        if not remember(pot, recipe) then return false, text("配方记录尚不可保存", "Recipe storage is unavailable") end
        return true, text("已记录连续槽位配方", "Consecutive-slot recipe remembered")
    end

    function self:preview()
        local pot, reason = current(true)
        if not pot then return false, reason end
        local recipe = recipe_in(pot)
        if not recipe then return false, text("放满食材后可预览候选", "Fill the container to preview candidates") end
        local candidates, uncertain = Rules.preview(require("cooking"), pot.prefab, recipe)
        local names = {}
        for _, prefab in ipairs(candidates) do names[#names + 1] = G.STRINGS.NAMES[string.upper(prefab)] or prefab end
        local message = #names > 0 and text("最高优先级候选：", "Highest-priority candidates: ") .. table.concat(names, ", ")
            or text("本地规则没有候选", "No candidates in local rules")
        if uncertain then message = message .. text("；部分规则不可读，结果不完整", "; some rules are unavailable, results are incomplete") end
        return true, message
    end

    local function open_sources()
        local extras = {}
        for entity in pairs(inv:GetOpenContainers() or {}) do
            local owner = valid(entity) and entity.replica.container
            if owner and owner.type == "chest" and not Rules.slots[entity.prefab] and readable(owner) then
                extras[#extras + 1] = owner
            end
        end
        table.sort(extras, function(a, b) return a.inst.GUID < b.inst.GUID end)
        return extras
    end
    local function plan(pot, recipe, extras)
        local stock, available, synchronizing = {}, {}, busy(inv)
        local backpack = inv:GetOverflowContainer()
        synchronizing = synchronizing or backpack and (not readable(backpack) or busy(backpack))
        for _, owner in ipairs(extras) do
            if readable(owner) then
                if busy(owner) then synchronizing = true else available[#available + 1] = owner end
            end
        end
        for _, entry in ipairs(Inventory.items(ctx, false, available)) do
            local size = Inventory.stack_size(entry.item)
            if entry.slot and size == nil then synchronizing = true end
            if entry.slot and size and size > 0 and readable(entry.owner) and not busy(entry.owner) then
                stock[#stock + 1] = { item = entry.item, owner = entry.owner, slot = entry.slot, count = size, reserved = 0 }
            end
        end
        local result = { recipe = { unpack(recipe) }, entries = {}, used = {}, index = 1 }
        for slot, prefab in ipairs(recipe) do
            for _, entry in ipairs(stock) do
                if entry.item.prefab == prefab and entry.count > entry.reserved
                    and pot.replica.container:CanTakeItemInSlot(entry.item, slot) then
                    entry.reserved = entry.reserved + 1
                    result.entries[slot], result.used[entry] = entry, true
                    break
                end
            end
            if not result.entries[slot] then
                return nil, synchronizing and text("等待食材来源同步", "Wait for ingredient source synchronization")
                    or text("缺少可用食材：", "Missing available ingredient: ") .. prefab, synchronizing
            end
        end
        return result
    end
    local function pot_matches(pot, recipe, filled)
        local container = pot.replica.container
        for slot = 1, #recipe do
            local item = container:GetItemInSlot(slot)
            if slot <= filled then
                if not valid(item) or item.prefab ~= recipe[slot] or Inventory.stack_size(item) ~= 1 then return false end
            elseif item then return false end
        end
        return true
    end
    local function source_matches(entry, amount)
        local item = entry.owner:GetItemInSlot(entry.slot)
        return amount == 0 and item == nil or valid(item) and item.GUID == entry.item.GUID
            and item.prefab == entry.item.prefab and Inventory.stack_size(item) == amount
    end

    local function run(mode, target, quantity)
        local radius = ctx.params.search_radius
        local available, reason = ready()
        if not available then return false, reason end
        local batch, harvest_only, quick = mode == "batch", mode == "harvest", mode == "quick"
        local harvest = not batch or ctx.params.cooking_harvest ~= false
        if not batch and not harvest_only and not quick then
            target, reason = current()
            if not target then return false, reason end
        elseif target == nil and batch then
            target, reason = opened()
            if not target then return false, reason end
        end
        if quick and (not target or not Query.visible(ctx, target)
            or player:GetDistanceSqToInst(target) > radius ^ 2) then
            return false, text("请打开或明确指向附近厨具", "Open or point to a nearby cooking container")
        end
        if target and not supported(target) then return false, text("不支持此厨具", "Unsupported cooking container") end
        if target and not harvest_only and not can_open(target) then
            return false, text("当前角色不能使用此厨具", "This character cannot use this cooking container")
        end
        local limit = Parameters.LIMIT
        quantity = batch and (quantity or ctx.params.cooking_count) or 1
        if type(quantity) ~= "number" or quantity ~= math.floor(quantity) or quantity < 1 or quantity > limit then
            return false, text("烹饪数量超出队列上限", "Cooking quantity exceeds the queue limit")
        end
        local recipe = target and recipe_in(target)
        if mode == "cook" and not recipe then return false, text("厨具内配方不完整", "Cooking container does not contain a complete recipe") end
        if mode ~= "cook" and not harvest_only and not recipe then
            if not ctx.store.ready then return false, text("配方记录尚未载入", "Recipe storage is still loading") end
            recipe = recipes()[target.prefab]
            if not recipe then return false, text("请先记住此厨具的完整配方", "Remember a complete recipe for this cooking container first") end
        end
        local function compatible(pot)
            return not target or pot.prefab == target.prefab
                or batch and Rules.compatible(require("cooking"), target.prefab, pot.prefab, recipe)
        end
        local pots = {}
        if batch or harvest_only then
            local function eligible(pot)
                return supported(pot) and compatible(pot) and (harvest_only or can_open(pot))
                    and (not harvest_only or pot:HasTag("donecooking"))
            end
            pots = Query.nearby(ctx, radius, nil, nil, nil, eligible, limit)
            if target then
                if not Query.visible(ctx, target) or player:GetDistanceSqToInst(target) > radius ^ 2 then
                    return false, text("目标厨具不在附近可见范围内", "Target cooking container is not nearby and visible")
                end
                for i = #pots, 1, -1 do if pots[i] == target then table.remove(pots, i) end end
                table.insert(pots, 1, target)
                for i = #pots, limit + 1, -1 do pots[i] = nil end
            end
        else pots = { target } end
        if quick and not target:HasTag("donecooking") and not target.replica.container:CanBeOpened() then
            return false, text("厨具正在烹饪，请完成后再重复", "The container is cooking; repeat once it finishes")
        end
        if #pots == 0 then return false, text("附近没有已完成的原版厨具", "No finished supported cooking containers nearby") end
        local sources = not harvest_only and mode ~= "cook" and open_sources() or {}
        local prepared
        if not batch and not harvest_only and not quick and mode ~= "cook" then
            if not target.replica.container:IsEmpty() then
                if mode == "fill" or not matches(recipe_in(target), recipe) then
                    return false, text("配料需要空锅；保留已有食材", "Filling requires an empty container; existing ingredients were retained")
                end
            else
                prepared, reason = plan(target, recipe, sources)
                if not prepared then return false, reason end
            end
        elseif (batch or quick) and target.replica.container:IsOpenedBy(player)
            and not target.replica.container:IsEmpty() and not recipe_in(target) then
            return false, text("目标锅有未完成配料，请先手动处理", "The selected pot has partial ingredients; resolve them manually first")
        end
        local phase = prepared and "fill" or "fire"
        if batch or harvest_only or quick then phase = "choose" end
        local pot, fill, ticket, deadline, wait_since, harvest_observed = target, prepared, nil, nil, nil, nil
        local preferred, blocked, pending, exhausted = target, {}, {}, false
        local job = { name = harvest_only and text("收获熟食", "Harvest cooked food") or text("烹饪队列", "Cooking queue"),
            input = { mode = "world" },
            timeout = batch and 600 or harvest_only and 120 or 45, cooked = 0, harvested = 0, skipped = 0,
            timeout_message = text("烹饪队列超时，已停止", "Cooking queue timed out; stopped"),
            error_message = text("烹饪队列失败，已停止", "Cooking queue failed; stopped") }
        local function summary()
            return text("已确认开火 ", "Confirmed cooking starts: ") .. job.cooked
                .. text("，收获 ", "; harvests: ") .. job.harvested .. text("，跳过 ", "; skipped: ") .. job.skipped
                .. (exhausted and text("；食材不足", "; ingredients exhausted") or "")
        end
        local function release_action(reason)
            if ticket then Actions.stop(ctx, ticket, reason or (ticket:performed() and "done" or "changed")) end
            ticket = nil
        end
        local function next_pot(reason)
            release_action(reason)
            fill, deadline, harvest_observed, phase = nil, nil, nil, "choose"
        end
        local function skip()
            blocked[pot], pending[pot], job.skipped = true, nil, job.skipped + 1
            next_pot("failed")
        end
        function job:step(now)
            if inv:GetActiveItem() or not readable(inv) then return "failed", text("物品栏状态改变，已停止", "Inventory state changed; stopped") end
            if phase == "choose" then
                if batch and (self.cooked >= quantity or exhausted) and next(pending) == nil then return "done", summary() end
                local waiting, chosen, chosen_phase, distance = false
                -- The startup query owns membership; subsequent movement only changes visit order.
                for _, candidate in ipairs(pots) do
                    if not blocked[candidate] and supported(candidate) and compatible(candidate)
                        and (harvest_only or can_open(candidate)) and Query.visible(ctx, candidate) then
                        if pending[candidate] and candidate.replica.container:CanBeOpened()
                            and not candidate:HasTag("donecooking") then pending[candidate] = nil end
                        local draining = batch and (self.cooked >= quantity or exhausted)
                        local candidate_phase
                        if (not draining or pending[candidate]) and candidate:HasTag("donecooking") then
                            if harvest then candidate_phase = "harvest"
                            else blocked[candidate], self.skipped = true, self.skipped + 1 end
                        elseif not harvest_only and not draining and candidate.replica.container:CanBeOpened() then
                            candidate_phase = "open"
                        elseif not harvest_only and (not draining or pending[candidate]) then
                            if harvest then waiting = true else blocked[candidate], self.skipped = true, self.skipped + 1 end
                        end
                        if candidate_phase then
                            local current_distance = player:GetDistanceSqToInst(candidate)
                            if not chosen or candidate == preferred or chosen ~= preferred
                                and (current_distance < distance or current_distance == distance and candidate.GUID < chosen.GUID) then
                                chosen, chosen_phase, distance = candidate, candidate_phase, current_distance
                            end
                        end
                    else pending[candidate] = nil end
                end
                if chosen then
                    pot, phase, deadline, preferred = chosen, chosen_phase, now + 15, nil
                    return "running"
                end
                if not waiting then
                    if quick then return "failed", text("目标厨具不可操作，已停止", "Target cooking container is unavailable; stopped") end
                    return "done", summary()
                end
                wait_since = wait_since or now
                if now - wait_since >= 240 then return "failed", text("等待熟食超时，已停止", "Waiting for cooked food timed out; stopped") end
                return "running"
            end
            if not supported(pot) or not compatible(pot) or not harvest_only and not can_open(pot) then
                return "failed", text("厨具状态或配方兼容性已改变", "Cooking container state or recipe compatibility changed")
            end
            local container = pot.replica.container
            deadline = deadline or now + 15
            if phase == "open" then
                if container:IsOpenedBy(player) and not busy(container) then
                    release_action()
                    if container:IsEmpty() then
                        local synchronizing
                        fill, reason, synchronizing = plan(pot, recipe, sources)
                        if not fill then
                            if synchronizing then
                                if now >= deadline then return "failed", text("来源同步超时", "Source synchronization timed out") end
                                return "running"
                            end
                            if quick then return "failed", reason end
                            exhausted = true; next_pot(); return "running"
                        end
                        phase = "fill"
                    elseif matches(recipe_in(pot), recipe) then phase = "fire"
                    else skip(); return "running" end
                    deadline = now + 15
                elseif now >= deadline or ticket and ticket.failed then skip(); return "running"
                elseif not ticket and not controller:IsBusy() then
                    local action, button = Actions.pick(ctx, pot, { RUMMAGE = true })
                    if not action then skip(); return "running" end
                    ticket, reason = Actions.dispatch(ctx, action, button)
                    if not ticket then return "failed", reason end
                end
                return "running"
            elseif phase == "harvest" then
                if not pot:HasTag("donecooking") and container:CanBeOpened() then
                    local performed = ticket and ticket:performed()
                    if not performed and ticket and not controller.ismastersim and not ticket.failed then
                        -- The cooker state and performaction echo replicate independently.
                        harvest_observed = harvest_observed or now
                        if now - harvest_observed < 1 then return "running" end
                    end
                    if performed then self.harvested = self.harvested + 1
                    else self.skipped = self.skipped + 1 end
                    pending[pot], wait_since = nil, nil
                    if harvest_only then blocked[pot] = true end
                    next_pot()
                elseif now >= deadline or ticket and ticket.failed then skip()
                elseif not ticket and not controller:IsBusy() then
                    local action, button = Actions.pick(ctx, pot, { HARVEST = true })
                    if not action then skip(); return "running" end
                    ticket, reason = Actions.dispatch(ctx, action, button)
                    if not ticket then return "failed", reason end
                end
                return "running"
            elseif phase == "confirm" then
                if started(pot) then
                    remember(pot, recipe)
                    self.cooked, wait_since = self.cooked + 1, nil
                    if not batch then return "done", text("已确认开始烹饪", "Cooking start confirmed") end
                    if harvest then pending[pot] = true else blocked[pot] = true end
                    next_pot()
                elseif now >= deadline then return "failed", text("未观察到开火，已停止", "Cooking start was not observed; stopped") end
                return "running"
            end
            if not container:IsOpenedBy(player) or not container:CanBeOpened() then
                return "failed", text("厨具关闭或玩家接管", "Cooking container closed or player took over")
            end
            if busy(inv) or busy(container) or controller:IsBusy() then
                if now >= deadline then return "failed", text("等待烹饪同步超时", "Cooking synchronization timed out") end
                return "running"
            end
            if phase == "fill" then
                for entry in pairs(fill.used) do
                    if not readable(entry.owner) then return "failed", text("食材来源已关闭", "Ingredient source closed") end
                    if busy(entry.owner) then
                        if now >= deadline then return "failed", text("来源同步超时", "Source synchronization timed out") end
                        return "running"
                    end
                end
                local entry = fill.entries[fill.index]
                if fill.pending then
                    local state = fill.pending:state()
                    if state == "changed" then return "failed", text("移料期间库存发生变化", "Inventory changed during ingredient transfer") end
                    if state == "done" and pot_matches(pot, fill.recipe, fill.index) then
                        entry.reserved = entry.reserved - 1
                        if entry.reserved == 0 then fill.used[entry] = nil end
                        entry.count, fill.index, fill.pending, deadline = entry.count - 1, fill.index + 1, false, now + 8
                        if fill.index > #fill.recipe then
                            if mode == "fill" then return "done", text("已观察到完整配料；可手动烹饪", "Complete ingredients observed; ready to cook") end
                            phase = "fire"
                        end
                    elseif now >= deadline then return "failed", text("未观察到预期移料，已停止", "Expected ingredient move was not observed; stopped") end
                    return "running"
                end
                if not pot_matches(pot, fill.recipe, fill.index - 1) then return "failed", text("厨具内容已改变", "Cooking container contents changed") end
                for source in pairs(fill.used) do
                    if not source_matches(source, source.count) then return "failed", text("食材来源已改变", "Ingredient source changed") end
                end
                local item = entry.owner:GetItemInSlot(entry.slot)
                if not valid(item) or item.replica.inventoryitem:IsLockedInSlot() or not container:CanTakeItemInSlot(item, fill.index) then
                    return "failed", text("此食材已不可移动", "This ingredient can no longer be moved")
                end
                fill.pending, reason = Inventory.move(ctx, entry, pot, 1)
                if not fill.pending then return "failed", text("此食材已不可移动", "This ingredient can no longer be moved") end
                deadline = now + 6
                return "running"
            end
            if not matches(recipe_in(pot), recipe) then return "failed", text("食材已改变", "Ingredients changed") end
            local widget = container:GetWidget()
            local button = widget and widget.buttoninfo
            if not button or type(button.fn) ~= "function" or button.validfn and not button.validfn(pot) then
                return "failed", text("原版烹饪按钮不可用", "Native cooking action is unavailable")
            end
            phase, deadline = "confirm", now + 6
            button.fn(pot, player)
            return "running"
        end
        function job:cancel(why) Actions.stop(ctx, ticket, why) end
        return ctx.executor:start(job)
    end

    function self:quick_repeat(target)
        local available, reason = ready()
        if not available then return false, reason end
        local pot, opened_reason, ambiguous = opened()
        -- Ambiguous open pots never fall through to a different cursor target.
        if ambiguous then
            return false, opened_reason
        end
        return run("quick", pot or target)
    end
    function self:shortcut()
        local input = G.TheInput
        if input:IsKeyDown(G.KEY_CTRL) then
            local entity = input:GetHUDEntityUnderMouse()
            local widget = entity and entity.widget
            while widget do
                if widget.owner == player and widget.container and widget.num then
                    local source = widget.container == inv and "inventory"
                        or widget.container == inv:GetOverflowContainer() and "backpack"
                    if not source then return false, text("请指向自己的物品栏或背包槽位", "Point to your own inventory or backpack slot") end
                    return self:remember_inventory(source, widget.num)
                end
                widget = widget.parent
            end
            return self:remember_inventory("inventory", 1)
        elseif input:IsKeyDown(G.KEY_SHIFT) then return self:remember_inventory("backpack", 1) end
        return self:quick_repeat(input:GetHUDEntityUnderMouse() == nil and input:GetWorldEntityUnderMouse() or nil)
    end

    function self:fill() return run("fill") end
    function self:cook() return run("cook") end
    function self:repeat_once() return run("repeat") end
    function self:batch(target, count) return run("batch", target, count) end
    function self:harvest() return run("harvest") end

    function self:observe_button(widget, pot)
        local button = widget.button
        if not button or observed_buttons[button] or type(button.onclick) ~= "function" or not supported(pot) then return end
        local original, own = button.onclick, rawget(button, "onclick")
        local function wrapper(...)
            if ctx.scope.closed then return original(...) end
            local enabled, hudblocking = controller:IsEnabled()
            local recipe = not player:HasTag("busy") and (enabled or hudblocking) and recipe_in(pot) or nil
            local function finish(...)
                if recipe and ctx.config.cooking ~= false then pending_memory[pot] = { recipe = recipe, deadline = G.GetTime() + 8 } end
                return ...
            end
            return finish(original(...))
        end
        observed_buttons[button] = { original = own, wrapper = wrapper }
        button.onclick = wrapper
    end
    function self:decorate(widget, pot)
        self:observe_button(widget, pot)
        if not supported(pot) or ctx.config.cooking == false or widget.fieldkit_repeat then return end
        local button = widget:AddChild(require("widgets/imagebutton")("images/ui.xml", "button_small.tex",
            "button_small_over.tex", "button_small_disabled.tex"))
        widget.fieldkit_repeat, shortcut_widgets[widget] = button, true
        local position = pot.replica.container:GetWidget().buttoninfo.position
        button:SetPosition(position.x, position.y - 52)
        button:SetFont(G.BUTTONFONT)
        button:SetTextSize(30)
        button:SetText(text("重复一次", "Repeat once"))
        button:SetHoverText(text("使用已记住的配方；保留锅内不完整食材", "Use the remembered recipe; preserve partial ingredients"))
        button:SetOnClick(function()
            if ctx.scope.closed then return end
            local _, reason = self:quick_repeat(pot)
            if reason then ctx:notify(reason) end
        end)
    end

    if ctx.scope then
        ctx.scope:periodic(.2, function()
            local now = G.GetTime()
            for pot, pending in pairs(pending_memory) do
                if started(pot) then remember(pot, pending.recipe); pending_memory[pot] = nil
                elseif not valid(pot) or now >= pending.deadline then pending_memory[pot] = nil end
            end
        end)
        ctx.scope:defer(function()
            pending_memory = {}
            for widget in pairs(shortcut_widgets) do
                if widget.fieldkit_repeat then widget.fieldkit_repeat:Kill(); widget.fieldkit_repeat = nil end
            end
            -- Native Open recreates buttons. Weak keys release dead widgets between openings.
            for button, saved in pairs(observed_buttons) do
                if button.onclick == saved.wrapper then button.onclick = saved.original end
            end
        end)
    end
    return self
end

function M.install(env, get_context)
    env.AddClassPostConstruct("widgets/containerwidget", function(widget)
        local original = widget.Open
        widget.Open = function(self, pot, doer, ...)
            local function finish(...)
                local ctx = get_context()
                if ctx and not ctx.scope.closed and ctx.cooking and doer == ctx.player then ctx.cooking:decorate(self, pot) end
                return ...
            end
            return finish(original(self, pot, doer, ...))
        end
        local close = widget.Close
        widget.Close = function(self, ...)
            if self.fieldkit_repeat then self.fieldkit_repeat:Kill(); self.fieldkit_repeat = nil end
            return close(self, ...)
        end
    end)
end

return M
