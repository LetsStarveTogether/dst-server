local Inventory = require("fieldkit/game/inventory")
local Information = require("fieldkit/game/information")
local Metrics = require("fieldkit/game/metrics")
local Data = require("fieldkit/game/announcement_data")
local Query = require("fieldkit/game/query")
local M = {}

function M.clean(text, limit)
    text = tostring(text or ""):gsub("[%z\1-\31\127]", " ")
    return text:utf8sub(1, limit)
end

local function number(value)
    return type(value) == "number" and string.format("%.0f", value) or "?"
end

local function freshness(item)
    if item.components and item.components.perishable then return item.components.perishable:GetPercent() end
    local replica = item.replica and item.replica.inventoryitem
    local value = replica and replica.classified and replica.classified.perish
    value = value and value:value()
    -- Native inventoryitem_classified uses 63 for unavailable and 0..62 for freshness.
    if type(value) == "number" and value >= 0 and value <= 62 then return value / 62 end
end

function M.new(ctx)
    local G, player = ctx.G, ctx.player
    local self = { last_sent = -math.huge }
    local function send(message, screen)
        if not ctx.config.announcements then return false, ctx:text("宣告已关闭", "Announcements are disabled") end
        local frontend = G.TheFrontEnd
        local allowed = screen == nil and frontend:GetActiveScreen() == ctx.hud
            or screen ~= nil and screen.fieldkit_announcement_context == ctx and screen.owner == player
                and screen.inst:IsValid() and frontend:GetActiveScreen() == screen
        if ctx.scope.closed or G.ThePlayer ~= player or not player:IsValid() or player.HUD ~= ctx.hud
            or not allowed or frontend.textProcessorWidget ~= nil then
            return false, ctx:text("当前界面无法宣告", "Announcements are unavailable in this screen")
        end
        if G.GetStaticTime() - self.last_sent < 5 then return false, ctx:text("请等 5 秒再宣告", "Wait 5 seconds between announcements") end
        local nearby = ctx.params.announcement_channel == "local"
        if G.TheInput:IsKeyDown(G.KEY_CTRL) then nearby = not nearby end
        G.TheNet:Say(M.clean(message, G.MAX_CHAT_INPUT_LENGTH or 150), nearby)
        self.last_sent = G.GetStaticTime()
        return true
    end
    local function metric(value)
        local names = value.label
        if value.unit == "temperature" then
            return ctx:text(names[1], names[2]) .. " " .. Information.temperature(value.value)
        end
        return ctx:text(names[1], names[2]) .. " " .. number(value.value)
            .. (value.max and ("/" .. number(value.max)) or "")
            .. (value.bonus and ctx:text(" · 额外生命上限 ", " · bonus max health ")
                .. number(value.bonus.value) .. "/" .. number(value.bonus.max) or "")
    end

    function self:status()
        local parts, own = {}, ctx.information:snapshot().own
        for _, key in ipairs({ "health", "hunger", "sanity" }) do parts[#parts + 1] = metric(own[key]) end
        return send(table.concat(parts, " · "))
    end

    function self:metric(key)
        if not Metrics.definition(key) then return false end
        local value = ctx.information:snapshot().own[key]
        if not value or value.value == nil then
            return false, ctx:text("当前没有这项状态的观测", "This state is not currently available")
        end
        return send(metric(value))
    end

    function self:position()
        local x, _, z = player.Transform:GetWorldPosition()
        return send(ctx:text("我在 ", "I am at ") .. string.format("(%.0f, %.0f)", x, z))
    end

    function self:world()
        local state = ctx.information:snapshot().world
        local seasons = { autumn = { "秋", "Autumn" }, winter = { "冬", "Winter" },
            spring = { "春", "Spring" }, summer = { "夏", "Summer" } }
        local season = seasons[state.season]
        local next_season = not state.endless and seasons[state.next_season]
        return send(ctx:text("第 ", "Day ") .. number(state.day) .. " · "
            .. (season and ctx:text(season[1], season[2]) or "?")
            .. ctx:text(" 第 ", " day ") .. number(state.season_day)
            .. ctx:text(" · 本季剩余 ", " · season days left ")
            .. (state.endless and ctx:text("无尽", "endless") or number(state.remaining_days))
            .. (next_season and ctx:text(" · 下一季 ", " · next season ") .. ctx:text(next_season[1], next_season[2]) or ""))
    end

    function self:world_temperature()
        return send(ctx:text("环境温度 ", "Air temperature ")
            .. Information.temperature(ctx.information:snapshot().world.temperature))
    end

    function self:moon()
        local world = ctx.information:snapshot().world
        local phases = { new = { "新月", "New moon" }, quarter = { "眉月", "Crescent" },
            half = { "半月", "Half moon" }, threequarter = { "凸月", "Gibbous" }, full = { "满月", "Full moon" } }
        local phase, full = phases[world.moonphase], world.fullmoon or {}
        local label = phase and ctx:text(phase[1], phase[2]) or "?"
        if world.moonphase ~= "new" and world.moonphase ~= "full" and world.waxing ~= nil then
            label = label .. (world.waxing and ctx:text("（渐盈）", " (waxing)") or ctx:text("（渐亏）", " (waning)"))
        end
        local estimate = full.quality == "estimated" and full.min_day and full.max_day
        return send(ctx:text("月相：", "Moon: ") .. label
            .. (estimate and ctx:text(" · 预计满月日 ", " · estimated full moon day ") .. number(full.min_day)
                .. (full.max_day ~= full.min_day and "–" .. number(full.max_day) or "")
                or ctx:text(" · 满月日未知", " · full moon day unknown")))
    end

    function self:naughtiness()
        local state = ctx.information:snapshot().naughtiness
        if not state then return false, ctx:text("尚无淘气值观测", "Naughtiness has not been observed") end
        return send(ctx:text("淘气值 ", "Naughtiness ") .. number(state.actions) .. "/" .. number(state.threshold))
    end

    function self:nightmare()
        local state = ctx.information:snapshot().nightmare
        local phases = { calm = { "平静", "Calm" }, warn = { "预警", "Warning" },
            wild = { "暴动", "Wild" }, dawn = { "消退", "Fading" } }
        local phase = phases[state.phase]
        if not phase then return false, ctx:text("尚无梦魇阶段观测", "Nightmare phase has not been observed") end
        return send(ctx:text("梦魇：", "Nightmare: ") .. ctx:text(phase[1], phase[2])
            .. (state.progress and (" " .. number(state.progress * 100) .. "%") or "")
            .. (state.remaining and (ctx:text(" · 约剩 ", " · about ") .. number(state.remaining) .. "s") or ""))
    end

    function self:ride()
        local ride = ctx.information:snapshot().ride
        if not ride then return false, ctx:text("当前没有骑乘观测", "No mount is being observed") end
        local remaining = ride.remaining_min and ride.remaining_max
            and ("≈" .. number(ride.remaining_min) .. "–" .. number(ride.remaining_max) .. "s") or "?"
        return send(ctx:text("骑乘 ", "Riding ") .. number(ride.observed) .. "s"
            .. ctx:text(" · 预计剩余 ", " · estimated remaining ") .. remaining)
    end

    function self:item(item)
        if not item or not item:IsValid() then return false, ctx:text("物品已失效", "Item is unavailable") end
        local parts = { M.clean(item:GetDisplayName(), 100) .. " ×" .. number(Inventory.stack_size(item)),
            ctx:text("身上共 ", "Carrying ") .. number(Inventory.count(ctx, item.prefab)) }
        local usage, fresh = Inventory.usage(item), freshness(item)
        if usage then parts[#parts + 1] = ctx:text("耐久 ", "Durability ") .. number(usage * 100) .. "%" end
        if fresh then parts[#parts + 1] = ctx:text("新鲜度 ", "Freshness ") .. number(fresh * 100) .. "%" end
        return send(table.concat(parts, " · "))
    end

    function self:equipment()
        return self:item(player.replica.inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS))
    end

    function self:target(target)
        if not Query.visible(ctx, target) then return false, ctx:text("目标不可见或已失效", "Target is unavailable or not visible") end
        local radius = ctx.params.search_radius
        local matches = Query.nearby(ctx, radius, nil, nil, nil, function(entity)
            local item = entity.replica and entity.replica.inventoryitem
            return entity.prefab == target.prefab and not (item and item:IsHeld())
        end, 256)
        local count, partial = 0, #matches == 256
        for _, entity in ipairs(matches) do
            local size = Inventory.stack_size(entity)
            count, partial = count + (size or 1), partial or size == nil
        end
        local distance = math.sqrt(player:GetDistanceSqToInst(target))
        return send(ctx:text("附近 ", "Within ") .. radius .. "m: " .. M.clean(target:GetDisplayName(), 70)
            .. " ×" .. (partial and "≥" or "") .. count
            .. ctx:text(" · 目标距离 ", " · target ") .. number(distance) .. "m")
    end

    local function recipe_status(state)
        local labels = {
            restricted = { "角色或技能不可用", "Unavailable for this character or skill" },
            buffered = { "已预制", "Already buffered" }, free = { "自由制作", "Free crafting" },
            known = { "科技已满足", "Technology available" }, prototype = { "可以解锁", "Can prototype" },
            locked = { "科技未满足", "Technology required" },
        }
        local label = labels[state]
        return ctx:text(label[1], label[2])
    end

    function self:recipe(value)
        local data = Data.recipe(ctx, type(value) == "table" and value.name or value)
        if not data then return false, ctx:text("配方信息尚未就绪", "Recipe information is not available") end
        local parts = { data.name, recipe_status(data.status) }
        if data.status ~= "buffered" and data.status ~= "free" then
            local missing, unknown = {}, false
            for _, ingredient in ipairs(data.ingredients) do
                if ingredient.quality == "unknown" then
                    unknown = true
                    missing[#missing + 1] = ingredient.name .. ctx:text("未知", " unknown")
                elseif not ingredient.has then
                    missing[#missing + 1] = ingredient.name .. (ingredient.unit == "count"
                        and " ×" .. number(math.max(0, ingredient.required - ingredient.count))
                        or ingredient.unit == "level" and " " .. number(ingredient.required)
                        or ctx:text("不足", " insufficient"))
                end
            end
            parts[#parts + 1] = #missing > 0 and ((unknown and ctx:text("缺少或未知：", "Missing or unknown: ")
                or ctx:text("缺少：", "Missing: ")) .. table.concat(missing, ", "))
                or ctx:text("材料已齐", "Ingredients ready")
        end
        return send(table.concat(parts, " · "))
    end

    function self:ingredient(target)
        local data = target and Data.recipe(ctx, target.recipe)
        if data then
            for _, ingredient in ipairs(data.ingredients) do
                if ingredient.category == target.category and ingredient.index == target.index then
                    if ingredient.quality ~= "known" then break end
                    local amount = ingredient.name .. " "
                    if ingredient.unit == "count" then
                        amount = amount .. number(ingredient.count) .. "/" .. number(ingredient.required)
                            .. (ingredient.has and ctx:text(" · 材料已齐", " · Ready")
                                or ctx:text(" · 缺少 ×", " · Missing ×") .. number(math.max(0, ingredient.required - ingredient.count)))
                    elseif ingredient.unit == "percent" then
                        amount = amount .. ctx:text("消耗上限 ", "max cost ") .. number(ingredient.required * 100)
                            .. ctx:text("% · 当前上限 ", "% · current max ") .. number(ingredient.count * 100) .. "%"
                    else
                        amount = amount .. (ingredient.unit == "level" and ctx:text("等级 ", "level ") or "")
                            .. number(ingredient.count) .. ctx:text(" · 需要 ", " · needs ")
                            .. (ingredient.strict and ">" or "≥") .. number(ingredient.required)
                    end
                    if ingredient.unit ~= "count" then
                        amount = amount .. (ingredient.has and ctx:text(" · 足够", " · Sufficient")
                            or ctx:text(" · 不足", " · Insufficient"))
                    end
                    return send(data.name .. " · " .. amount)
                end
            end
        end
        return false, ctx:text("当前材料信息不可用", "This ingredient is not currently available")
    end

    function self:food(target, screen)
        local data = target and Data.food(ctx, target.category, target.prefab)
        if not data then return false, ctx:text("尚未解锁这道菜的效果", "This food's effects are not unlocked") end
        local parts = { data.name .. ctx:text(" · 食谱基础值", " · Recipe base values") }
        for _, key in ipairs({ "health", "hunger", "sanity" }) do
            local label, value = Metrics.definition(key).label, data[key]
            parts[#parts + 1] = ctx:text(label[1], label[2]) .. " "
                .. (value ~= nil and string.format("%+g", value) or "?")
        end
        parts[#parts + 1] = data.effect
        return send(table.concat(parts, " · "), screen)
    end

    function self:circuit(target)
        if not target then return false end
        for _, entry in ipairs(Data.circuits(ctx)) do
            if entry.bar == target.bar and entry.index == target.index and entry.netid == target.netid then
                return send(ctx:text("电路 ", "Circuit ") .. (entry.bar + 1) .. " · " .. entry.name
                    .. ctx:text(" · 占用格数 ", " · Slots ") .. entry.slots
                    .. (entry.powered and ctx:text(" · 已供电", " · Powered") or ctx:text(" · 未供电", " · Unpowered")))
            end
        end
        return false, ctx:text("芯片已变化或尚未同步", "The circuit changed or has not synchronized")
    end
    return self
end

return M
