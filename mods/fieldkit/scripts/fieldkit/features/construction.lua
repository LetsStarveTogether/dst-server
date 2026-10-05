local Parameters = require("fieldkit/core/parameters")
local Actions = require("fieldkit/game/actions")
local Inventory = require("fieldkit/game/inventory")
local Placement = require("fieldkit/game/placement")
local M = {}

local function valid(item) return item and item:IsValid() end
local function finite(n) return type(n) == "number" and n == n and math.abs(n) < math.huge end

function M.new(ctx)
    local self, player = {}, ctx.player
    local controller, builder = player.components.playercontroller, player.replica.builder
    local last
    local function text(zh, en) return ctx:text(zh, en) end
    local function remember(recipe, skin)
        if type(recipe) == "table" and type(recipe.name) == "string" then
            last = { name = recipe.name, skin = skin }
        end
    end
    if builder then
        for _, method in ipairs({ "MakeRecipeFromMenu", "MakeRecipeAtPoint" }) do
            if builder[method] then
                ctx.scope:wrap(builder, method, function(original)
                    if method == "MakeRecipeFromMenu" then
                        return function(owner, recipe, skin, ...)
                            if not ctx.executor:is_running() then remember(recipe, skin) end
                            return original(owner, recipe, skin, ...)
                        end
                    end
                    return function(owner, recipe, point, rotation, skin, ...)
                        if not ctx.executor:is_running() then remember(recipe, skin) end
                        return original(owner, recipe, point, rotation, skin, ...)
                    end
                end)
            end
        end
    end

    local function ready()
        if ctx.config.work == false then return false, text("动作队列已关闭", "Work queue is disabled") end
        local allowed, reason = ctx:ready()
        if not allowed then return false, reason end
        if ctx.executor:is_running() then return false, text("请先停止当前操作", "Stop the current operation first") end
        local inv = player.replica.inventory
        if not Inventory.readable(ctx, inv) then
            return false, text("物品栏尚不可用", "Inventory is unavailable")
        end
        return true
    end
    local function recipe_for(value)
        local name = type(value) == "table" and value.name or value
        return type(name) == "string" and ctx.G.GetValidRecipe(name) or nil
    end
    local function inventory_busy()
        local inv = player.replica.inventory
        local overflow = inv:GetOverflowContainer()
        return Inventory.busy(ctx, inv) or overflow and Inventory.busy(ctx, overflow)
    end
    local function count_limit(value, fallback)
        local n = value == nil and fallback or value
        local limit = Parameters.LIMIT
        if not finite(n) or n < 1 or n ~= math.floor(n) or n > limit then return end
        return n
    end
    local function begin(job)
        job.timeout = 600
        job.timeout_message = text("施工超过十分钟，已停止", "Construction exceeded ten minutes; stopped")
        job.error_message = text("施工失败，已停止", "Construction failed; stopped")
        return ctx.executor:start(job)
    end
    local function summary(job, exhausted)
        return text("已完成 ", "Completed ") .. job.completed
            .. text("，跳过 ", "; skipped ") .. (job.skipped or 0)
            .. (exhausted and text("；材料或工具已耗尽", "; materials or tools exhausted") or "")
    end

    function self:repeat_recipe(count)
        if not last then return false, text("尚未记录制作配方", "No crafting recipe has been recorded") end
        return self:craft(last.name, count, last.skin)
    end

    function self:craft(value, count, skin)
        local allowed, reason = ready()
        if not allowed then return false, reason end
        local recipe = recipe_for(value)
        count = count_limit(count, ctx.params.craft_count)
        if not builder or not recipe or not count then
            return false, text("配方或制作数量无效", "Invalid recipe or crafting quantity")
        end
        if player.replica.inventory:GetActiveItem() then
            return false, text("请先放下鼠标物品", "Put down the cursor item first")
        end
        remember(recipe, skin)
        local job = { name = text("批量制作", "Crafting queue"), completed = 0,
            total = recipe.placer and 1 or count, input = { mode = "world", placement = recipe.placer ~= nil } }
        local ticket, waiting, deadline, synchronizing
        function job:step(now)
            if not ctx.equipment:ready(self) then return "running" end
            deadline = deadline or now + 12
            if ticket then
                if ticket.failed then return "failed", ticket.failure or text("原生制作失败", "Native crafting failed") end
                if ticket.recipe.name == recipe.name and ticket:result_ready() or ticket:product_ready() then
                    local finished = ticket.recipe.name == recipe.name
                    local station = ticket.station
                    ticket:close(); ticket = nil
                    self.completed, deadline = self.completed + (finished and 1 or 0), now + 12
                    if self.completed >= self.total then return "done", summary(self) end
                    if station and station:HasTag("pickable") and not station:HasTag("prototyper") then
                        return "done", summary(self) .. text("；请收取制造站成品后继续", "; collect the station's product before continuing")
                    end
                    synchronizing = station ~= nil
                elseif now >= deadline then
                    return "failed", text("未收到制作结果，已停止", "No crafting result received; stopped")
                else return "running" end
            end
            if controller:IsBusy() or builder:IsBusy() or inventory_busy() then
                if now >= deadline then return "failed", text("等待制作同步超时", "Crafting synchronization timed out") end
                return "running"
            end
            if recipe.placer and builder:IsBuildBuffered(recipe.name) then
                controller:StartBuildPlacementMode(recipe, skin)
                self.completed = 1
                return "done", text("建筑已预制；可单点放置或框选施工区域", "Building prepared; place it or select a construction area")
            end
            if waiting then
                if now >= deadline then return "failed", text("未收到建筑预制结果", "Building preparation was not observed") end
                return "running"
            end
            local craftable, failure = Inventory.craft_step(ctx, recipe)
            if not craftable then
                if failure == "ingredients" then return "done", summary(self, true) end
                if synchronizing and now < deadline then return "running" end
                return "failed", text("当前无法解锁或制作此配方", "This recipe cannot be learned or crafted here")
            end
            synchronizing = nil
            if craftable == recipe and recipe.placer then
                waiting, deadline = true, now + 8
                builder:BufferBuild(recipe.name)
            else
                ticket, reason = Actions.build(ctx, craftable, nil, nil, craftable == recipe and skin or nil)
                if not ticket then return "failed", reason end
                deadline = now + (craftable.manufactured and 60 or 12)
            end
            return "running"
        end
        function job:cancel(why)
            Actions.stop(ctx, ticket, why)
        end
        return begin(job)
    end

    local function has_soil(point)
        for _, entity in ipairs(ctx.G.TheSim:FindEntities(point.x, 0, point.z, 0.2,
            { "soil" }, { "INLIMBO", "NOCLICK", "NOBLOCK" })) do
            if valid(entity) then return true end
        end
        return false
    end
    local function tool(item, kind)
        if not valid(item) then return false end
        if kind == "till" then return item:HasActionComponent("farmtiller") end
        if kind == "terraform" then return item:HasActionComponent("terraformer") and not item:HasTag("plow") end
        return item:HasTag("wateringcan") and not item:HasTag("usesdepleted")
            and item:HasActionComponent("wateryprotection")
    end
    local function droppable(item)
        local replica = valid(item) and item.replica.inventoryitem
        return replica and not replica:IsLockedInSlot()
    end
    local function fertilizer(item)
        return valid(item) and item.HasTag and item:HasTag("fertilizer") and item:HasTag("tile_deploy")
    end
    local function point_action(point, item, kind, rotation)
        for _, action in ipairs(Actions.candidates(ctx, nil, kind == "drop" and "left" or "right", item, point, rotation)) do
            local id = action.action.id
            if kind == "till" and id == "TILL" or kind == "water" and id == "POUR_WATER_GROUNDTILE"
                or kind == "fertilize" and id == "DEPLOY_TILEARRIVE"
                or kind == "terraform" and id == "TERRAFORM" or kind == "drop" and id == "DROP"
                or kind == "deploy" and (id == "DEPLOY" or id == "DEPLOY_TILEARRIVE" or id == "DEPLOY_FLOATING") then
                if kind == "drop" then action.options.wholestack = false end
                return action
            end
        end
    end
    function self:area(area, requested, preview)
        local allowed, reason = ready()
        if not allowed then return false, reason end
        if not ctx.placement or type(area) ~= "table" or type(area.corners) ~= "table" or #area.corners < 3 then
            return false, text("施工区域无效", "Invalid construction area")
        end
        local inv = player.replica.inventory
        local recipe, skin = Placement.selected_recipe(ctx)
        local item = inv:GetActiveItem()
        local hands = inv:GetEquippedItem(ctx.G.EQUIPSLOTS.HANDS)
        local kind = requested or (recipe and "build" or fertilizer(item) and "fertilize"
            or item and "deploy" or tool(hands, "till") and "till" or tool(hands, "water") and "water")
        local cursor_item = kind == "deploy" or kind == "fertilize" or kind == "drop"
        local equipped_tool = kind == "till" or kind == "water" or kind == "terraform"
        if equipped_tool then
            if item then return false, text("请先放下鼠标物品", "Put down the cursor item first") end
            if not tool(hands, kind) and ctx.config.queue_tools ~= false then
                for _, entry in ipairs(Inventory.items(ctx)) do
                    if tool(entry.item, kind) then hands = entry.item; break end
                end
            end
            item, recipe = hands, nil
        elseif cursor_item then recipe = nil
        elseif kind == "build" then item = nil end
        if kind == "build" and not (recipe and builder) or equipped_tool and not tool(item, kind)
            or cursor_item and kind ~= "drop" and not (valid(item) and item.replica.inventoryitem and item.replica.inventoryitem:IsDeployable(player))
            or kind == "drop" and not droppable(item)
            or kind == "fertilize" and not fertilizer(item) or not (kind == "build" or equipped_tool or cursor_item) then
            return false, text("请选择建筑、未锁定的鼠标物品或对应工具", "Choose a building, unlocked cursor item, or matching tool")
        end
        local limit = Parameters.LIMIT
        local orientation
        if preview and preview.kind == kind and preview.recipe == recipe and preview.item == item then
            orientation = preview.rotation
            local platform = preview.platform
            local corner = area.corners[1]
            if valid(platform) and platform == ctx.G.TheWorld.Map:GetPlatformAtPoint(corner.x, corner.z) then
                orientation = orientation + platform.Transform:GetRotation() - preview.platform_rotation
            end
        end
        local records, detail = ctx.placement:points(area, nil, limit, item, recipe, kind, orientation)
        if not records or #records == 0 then
            return false, text("区域没有可用格点或超过点数上限：", "No grid points or area exceeds the point limit: ") .. tostring(detail or "empty")
        end
        local prefab = item and item.prefab
        local repetitions = (kind == "water" or kind == "fertilize") and count_limit(ctx.params.repeat_count, 1) or 1
        if not repetitions then return false, text("投入次数无效", "Invalid application count") end
        local ticket, index, deadline, count_before, buffered, refill_pending, equip_pending, sent_at = nil, 1
        local applied = 0
        local preparing_material, tile_before
        local job = { name = kind == "water" and text("区域浇水", "Area watering")
                or kind == "fertilize" and text("区域施肥", "Area fertilizing")
                or kind == "terraform" and text("区域挖地皮", "Area turf digging")
                or kind == "drop" and text("按格丢物", "One item per grid point") or text("区域施工", "Area construction"),
            total = #records * repetitions, completed = 0, skipped = 0,
            input = { mode = "world", cursor = cursor_item and prefab or nil, placement = true } }
        local function close_ticket()
            if ticket then ticket:close(); ticket = nil end
        end
        local function advance(completed)
            close_ticket()
            if completed then job.completed, applied = job.completed + 1, applied + 1
            else job.skipped, applied = job.skipped + repetitions - applied, repetitions end
            if applied == repetitions then index, applied = index + 1, 0 end
            deadline, count_before, buffered, tile_before = nil, nil, nil, nil
        end
        function job:step(now)
            if not ctx.equipment:ready(self) then return "running" end
            if equip_pending then
                if equip_pending:state() ~= "done" then return "failed", text("装备工具未确认", "Equipping the tool was not confirmed") end
                equip_pending = nil
            end
            local record = records[index]
            if not record then return "done", summary(self) end
            local point = ctx.placement:resolve(record)
            if not point then return "failed", text("施工平台已离开或消失", "Construction platform is unavailable") end
            deadline = deadline or now + 15
            if ticket then
                if preparing_material then
                    if ticket:product_ready() then
                        ticket:close(); ticket, preparing_material, deadline = nil, nil, now + 15
                    elseif ticket.failed or now >= deadline then
                        return "failed", text("中间材料制作未完成", "Ingredient crafting did not complete")
                    end
                    return "running"
                end
                local count_now = cursor_item and Inventory.count(ctx, prefab)
                local consumed = cursor_item and count_before and count_now and count_now < count_before
                if kind == "drop" and consumed and count_now ~= count_before - 1 then
                    return "failed", text("丢物数量异常，已停止", "Unexpected dropped quantity; stopped")
                end
                if kind == "water" or kind == "fertilize" and not item.replica.stackable then
                    local quantized = ticket.usage_resolution ~= nil and valid(item)
                        and (kind == "water" and Inventory.equipped(ctx, item)
                            or kind == "fertilize" and inv:GetActiveItem() == item)
                        and now - sent_at >= .5 and not controller:IsBusy()
                        and player:GetBufferedAction() == nil
                    if quantized then
                        local here = player:GetPosition()
                        quantized = math.abs(here.x - point.x) <= 2 and math.abs(here.z - point.z) <= 2
                            and ctx.placement:validate(point, item, nil, kind)
                            and point_action(point, item, kind) ~= nil
                    end
                    consumed = ticket:consumed(consumed, quantized)
                end
                if (kind == "build" and ticket:result_ready() or ticket.succeeded) or kind == "till" and has_soil(point)
                    or kind == "terraform" and ticket.sent and tile_before ~= nil
                        and ctx.G.TheWorld.Map:GetTileAtPoint(point.x, 0, point.z) ~= tile_before
                    or ticket.sent and consumed then
                    advance(true)
                    return "running"
                end
                if ticket.failed then
                    if repetitions > 1 then return "failed", text("水肥投入被拒绝，已停止", "Watering or fertilizing was rejected; stopped") end
                    advance(false); return "running"
                end
                if now >= deadline then
                    return "failed", text("未观察到施工结果，已停止", "No construction result observed; stopped")
                end
                return "running"
            end
            if controller:IsBusy() or builder and builder:IsBusy() or inventory_busy() then
                if now >= deadline then return "failed", text("等待施工同步超时", "Construction synchronization timed out") end
                return "running"
            end
            if cursor_item then
                local active = inv:GetActiveItem()
                if active and active.prefab ~= prefab then return "failed", text("鼠标物品已改变", "Cursor item changed") end
                if active and not valid(active) then
                    if now >= deadline then return "failed", text("鼠标物品同步超时", "Cursor item synchronization timed out") end
                    return "running"
                end
                if not active then
                    if refill_pending then
                        if now >= refill_pending then return "failed", text("补充物品超时", "Item refill timed out") end
                    elseif Inventory.refill(ctx, prefab) then refill_pending = now + 6
                    else return "done", summary(self, true) end
                    return "running"
                end
                item, refill_pending = active, nil
                if kind == "drop" and not droppable(item) then
                    return "failed", text("物品已锁定，已停止丢物", "Item is locked; dropping stopped")
                end
                count_before = Inventory.count(ctx, prefab)
                if count_before == nil then
                    if now >= deadline then return "failed", text("等待堆叠同步超时", "Stack synchronization timed out") end
                    return "running"
                end
            elseif equipped_tool then
                item = inv:GetEquippedItem(ctx.G.EQUIPSLOTS.HANDS)
                if not tool(item, kind) then
                    if ctx.config.queue_tools == false then return "done", summary(self, true) end
                    for _, entry in ipairs(Inventory.items(ctx)) do
                        if tool(entry.item, kind) then
                            local why
                            equip_pending, why = ctx.equipment:equip(self, entry.item)
                            if equip_pending or why == "busy" then
                                return "running"
                            end
                        end
                    end
                    return "done", summary(self, true)
                end
            end
            local _, planned_rotation = ctx.placement:resolve(record)
            local can, projected, rotation = ctx.placement:validate(point, item, recipe, kind, planned_rotation or record.rotation)
            if not can then advance(false); return "running" end
            point = projected or point
            rotation = rotation or record.rotation or 0
            if kind == "build" then
                if not builder then return "failed", text("建筑能力不可用", "Building is unavailable") end
                if not builder:IsBuildBuffered(recipe.name) then
                    if buffered then
                        if now >= deadline then return "failed", text("预制建筑同步超时", "Building preparation timed out") end
                    else
                        local next_recipe = Inventory.craft_step(ctx, recipe)
                        if not next_recipe then return "done", summary(self, true) end
                        if next_recipe == recipe then
                            buffered = true
                            builder:BufferBuild(recipe.name)
                        else
                            ticket, reason = Actions.build(ctx, next_recipe)
                            if not ticket then return "failed", reason end
                            preparing_material = true
                        end
                    end
                    return "running"
                end
                if not builder:CanBuildAtPoint(point, recipe, rotation) then advance(false); return "running" end
                ticket, reason = Actions.build(ctx, recipe, point, rotation, skin)
            else
                local action = point_action(point, item, kind, rotation)
                if not action then advance(false); return "running" end
                action:SetActionPoint(point)
                action.rotation = rotation
                if kind == "water" and Inventory.usage(item) == nil then
                    if now >= deadline then return "failed", text("水壶用量同步超时", "Watering can usage synchronization timed out") end
                    return "running"
                end
                if kind == "terraform" then tile_before = ctx.G.TheWorld.Map:GetTileAtPoint(point.x, 0, point.z) end
                ticket, reason = Actions.dispatch(ctx, action, kind == "drop" and "left" or "right")
            end
            if not ticket then return "failed", reason end
            deadline, sent_at = now + 15, now
            return "running"
        end
        function job:cancel(why)
            Actions.stop(ctx, ticket, why)
            close_ticket()
        end
        return begin(job)
    end

    return self
end

return M
