local Scope = require("fieldkit/core/scope")
local Inventory = require("fieldkit/game/inventory")
local Query = require("fieldkit/game/query")
local Rules = require("fieldkit/rules/timers")
local Cooking = require("cooking")
local M = {}

local function food_data(prefab)
    for _, cooker in ipairs({ "cookpot", "portablecookpot", "portablespicer" }) do
        local recipe = Cooking.recipes[cooker] and Cooking.recipes[cooker][prefab]
        if recipe then return recipe end
    end
end

function M.new(ctx)
    local G, player, book = ctx.G, ctx.player, ctx.journal
    local self, pending, tracked = { weather = {} }, nil, {}
    local weather, last_phase, next_world, was_dead = Rules.weather(), nil, 0, false
    local function name(prefab) return G.STRINGS.NAMES[prefab:upper()] or prefab end
    function self:observe_food(prefab)
        if type(prefab) ~= "string" then return end
        for _, effect in ipairs(Rules.food(G.TUNING, prefab, food_data(prefab))) do
            if effect.duration <= 86400 then
                book:put({ key = "buff:" .. (effect.key or effect.id), kind = "buff", state = effect.id,
                    prefab = prefab, clock = "player", due = G.GetTime() + effect.duration })
            end
        end
        book:flush()
    end
    local function eat_request(item)
        if G.TheWorld.ismastersim or not item or not item:IsValid()
            or type(item.prefab) ~= "string" or #Rules.food(G.TUNING, item.prefab, food_data(item.prefab)) == 0 then return end
        local count = Inventory.stack_size(item)
        if count then pending = { item = item, prefab = item.prefab, count = count, started = G.GetTime() } end
    end
    -- Observe the actual outbound request after prediction has decided to send it.
    ctx.scope:wrap(G, "SendRPCToServer", function(original)
        return function(rpc, ...)
            local code, item = ...
            if rpc == G.RPC.UseItemFromInvTile or rpc == G.RPC.ControllerUseItemOnSelfFromInvTile then
                if code == G.ACTIONS.EAT.code then eat_request(item) else pending = nil end
            end
            return original(rpc, ...)
        end
    end)
    local controller = player.components.playercontroller
    ctx.scope:wrap(controller, "DoAction", function(original)
        return function(object, action, ...)
            if action and action.action == G.ACTIONS.EAT then eat_request(action.invobject or action.target)
            else pending = nil end
            return original(object, action, ...)
        end
    end)
    ctx.scope:wrap(controller, "OnControl", function(original)
        return function(object, control, down, ...)
            if down then pending = nil end
            return original(object, control, down, ...)
        end
    end)
    ctx.scope:listen("oneat", function(_, data)
        if data and data.food then self:observe_food(data.food.prefab); pending = nil end
    end)
    ctx.scope:listen("performaction", function(_, data)
        if pending and (not data or data.action and data.action.action == G.ACTIONS.EAT
            and (data.action.invobject == pending.item or data.action.target == pending.item)) then pending.performed = true end
    end)
    local function dead()
        pending = nil
        if was_dead then return end
        was_dead = true
        book:clear("buff")
        book:flush()
    end
    ctx.scope:listen("death", dead)
    ctx.scope:listen("ms_becameghost", dead)
    ctx.scope:listen("serverpauseddirty", function() weather:reset() end, G.TheWorld)

    local function boss_key(entity)
        local rule = Rules.bosses[entity.prefab]
        local key = rule.key or entity.prefab
        if entity.prefab == "walrus" then
            local p = entity:GetPosition()
            -- Camps are distinct. Death coordinates remain a labelled observation, not a camp identifier.
            key = key .. ":" .. math.floor(p.x / 8) .. ":" .. math.floor(p.z / 8)
        end
        return "boss:" .. key
    end
    local function observe_death(entity, record)
        if record.dead or not record.alive or not Query.observed(ctx, entity, true) then return end
        local low, high, _, season = Rules.respawn(G.TUNING, entity.prefab)
        local now = book:time()
        if not now or not low then return end
        record.dead = true
        local p = entity:GetPosition()
        book:put({ key = boss_key(entity), kind = "boss", prefab = entity.prefab, name = entity:GetDisplayName(),
            due = now + low, duration = high - low, state = season or "cooldown", x = p.x, z = p.z })
        book:flush()
    end
    local function update_bosses()
        local now, position, present = G.GetTime(), player:GetPosition(), {}
        local count = 0
        for _ in pairs(tracked) do count = count + 1 end
        for _, entity in ipairs(G.TheSim:FindEntities(position.x, 0, position.z, 80, nil, { "INLIMBO", "CLASSIFIED" })) do
            if Rules.bosses[entity.prefab] and Query.observed(ctx, entity, true) then
                local record = tracked[entity]
                if not record and count < 32 then
                    record = { scope = Scope.new(player, function(err) ctx:report(err) end) }
                    tracked[entity], count = record, count + 1
                    record.scope:listen("death", function() observe_death(entity, record) end, entity)
                end
                if record then
                    present[entity], record.seen = true, now
                    local health = entity.replica.health
                    local animation = entity.AnimState and entity.AnimState:GetCurrentAnimationName() or ""
                    local is_dead = health and health:IsDead() or animation:find("death", 1, true) ~= nil
                    if is_dead then observe_death(entity, record)
                    else
                        if not record.alive and entity.prefab ~= "walrus" then
                            local old = book:get(boss_key(entity))
                            if old and old.due then
                                old.state = "seen"
                                book:put(old)
                            end
                        end
                        record.alive = true
                    end
                end
            end
        end
        for entity, record in pairs(tracked) do
            if not present[entity] and (not entity:IsValid() or now - record.seen > 2) then
                record.scope:close()
                tracked[entity] = nil
            end
        end
    end
    function self:update()
        local now = G.GetTime()
        if player:HasTag("playerghost") then dead()
        elseif was_dead then
            local health = player.replica.health
            if not health or not health:IsDead() then was_dead = false end
        end
        if pending then
            local item, animation = pending.item, player.AnimState:GetCurrentAnimationName() or ""
            if animation:find("eat", 1, true) or animation:find("drink", 1, true) then pending.animation = true end
            local count = item:IsValid() and Inventory.stack_size(item) or 0
            if pending.performed and pending.animation and count and count < pending.count then
                self:observe_food(pending.prefab)
                pending = nil
            elseif now - pending.started > 6 then pending = nil end
        end
        for _, record in ipairs(book:records("buff")) do
            if not record.due or record.due <= now then book:remove(record.key) end
        end
        if now >= next_world then
            next_world = now + .5
            self.weather = weather:update(G.TheWorld.state, now, G.TUNING)
            if last_phase and last_phase ~= self.weather.phase then
                book:add("weather", { state = self.weather.phase })
            end
            last_phase = self.weather.phase
            update_bosses()
        end
    end
    function self:duration(seconds)
        if not Rules.finite(seconds) then return "?" end
        seconds = math.max(0, seconds)
        local day = G.TUNING.TOTAL_DAY_TIME
        if seconds >= day then return string.format("%.1f", seconds / day) .. ctx:text(" 游戏天", " game days") end
        return string.format("%d:%02d", math.floor(seconds / 60), math.floor(seconds % 60))
    end
    function self:snapshot()
        local result = { buffs = {}, bosses = {}, weather = self.weather }
        local now, worldtime = G.GetTime(), book:time()
        for _, record in ipairs(book:records("buff")) do
            local label = Rules.buff_names[record.state]
            if label and record.due and record.due > now then
                result.buffs[#result.buffs + 1] = { label = ctx:text(label[1], label[2]),
                    remaining = record.due - now, source = record.prefab and name(record.prefab) }
            end
        end
        table.sort(result.buffs, function(a, b) return a.remaining < b.remaining end)
        for _, record in ipairs(book:records("boss")) do
            if record.due and record.duration and record.prefab and worldtime then
                local label = record.name or name(record.prefab)
                if record.prefab == "walrus" and record.x and record.z then
                    label = label .. string.format(" (%d, %d)", record.x, record.z)
                end
                result.bosses[#result.bosses + 1] = { label = label, at = record.at,
                    low = math.max(0, record.due - worldtime), high = math.max(0, record.due + record.duration - worldtime),
                    state = record.state, key = record.key }
            end
        end
        return result
    end
    function self:weather_label()
        local names = { none = { "晴", "Dry" }, rain = { "雨", "Rain" }, snow = { "雪", "Snow" },
            acidrain = { "酸雨", "Acid rain" }, lunarhail = { "月雹", "Lunar hail" } }
        local state = self.weather
        local label = names[state.phase] or { "天气", "Weather" }
        local suffix = state.transition == "start" and ctx:text(" · 预计降水 ", " · Precipitation in ")
            or ctx:text(" · 预计结束 ", " · Ends in ")
        local duration = self:duration(state.low)
        if state.low and state.high and state.high - state.low > 5 then duration = duration .. "–" .. self:duration(state.high) end
        return ctx:text(label[1], label[2]) .. suffix .. (state.low and "≈" or "") .. duration
    end
    ctx.scope:periodic(.1, function() self:update() end)
    ctx.scope:defer(function() for _, record in pairs(tracked) do record.scope:close() end end)
    return self
end

return M
