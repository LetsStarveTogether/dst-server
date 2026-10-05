local Query = require("fieldkit/game/query")
local Ranges = require("fieldkit/rules/ranges")
local Indicators = require("fieldkit/ui/indicators")
local Loot = require("fieldkit/features/loot")
local M = {}

local function point(entity)
    local x, _, z = entity.Transform:GetWorldPosition()
    return { x = x, z = z }
end

M.observed = Query.observed

local function inventory_hover(ctx)
    local hit = ctx.G.TheInput:GetHUDEntityUnderMouse()
    local widget = hit and hit.widget
    for _ = 1, 8 do
        if not widget then break end
        if widget.name == "ItemSlot" and widget.tile then return widget.tile.item end
        widget = widget.parent
    end
end

function M.new(ctx)
    local G, pins, bosses = ctx.G, {}, {}
    local renderer, loot = Indicators.new(ctx), Loot.new(ctx)
    local suppressed_hover, pin_serial = nil, 0
    local self = { frame = { ranges = {}, bosses = {}, loot = {} } }

    local function label(rules)
        local parts = {}
        local precision = {
            target_radius = ctx:text("含目标体积修正", "target size varies"),
            conditional = ctx:text("满足条件时", "when active"),
            upper_bound = ctx:text("最大范围", "maximum"),
            estimate = ctx:text("估计", "estimate"),
        }
        for _, rule in ipairs(rules) do
            local hint = precision[rule.precision]
            parts[#parts + 1] = string.format("%s %.1fm%s", ctx:text(rule.zh, rule.en), rule.radius,
                hint and " (" .. hint .. ")" or "")
        end
        return table.concat(parts, " · ")
    end

    local function describe_range(key, prefab, center, target, rules, preview)
        rules = rules or Ranges.describe(G.TUNING, prefab, target, ctx.player)
        if #rules == 0 then return end
        if rules[1].owner and not preview then center = point(ctx.player) end
        if rules[1].point and not preview then center = rules[1].point end
        return { key = key, point = center, rules = rules, label = label(rules) }
    end

    local function add_range(frame, ...)
        local record = describe_range(...)
        if record then frame.ranges[#frame.ranges + 1] = record end
        return record
    end

    local function add_pin(target, rules)
        if #pins == 64 then table.remove(pins, 1) end
        pin_serial = pin_serial + 1
        local record = { target = target, rules = rules, key = "range:pin:" .. pin_serial }
        local described = rules or Ranges.describe(G.TUNING, target.prefab, target, ctx.player)
        record.snapshot = describe_range(record.key, target.prefab, point(target), target, described)
        if described[1] and described[1].anchor == "pinned" then
            record.point = point(target)
            record.platform = target.GetCurrentPlatform and target:GetCurrentPlatform()
            if record.platform then
                local x, _, z = record.platform.entity:WorldToLocalSpace(record.point.x, 0, record.point.z)
                record.point = { x = x, z = z }
            end
        end
        pins[#pins + 1] = record
        if suppressed_hover == target then suppressed_hover = nil end
    end

    function self:can_pin(target)
        return ctx.config.range_hints and M.observed(ctx, target)
            and #Ranges.describe(G.TUNING, target.prefab, target, ctx.player) > 0
    end

    function self:toggle_pin(target)
        if not ctx.config.range_hints then return false, ctx:text("范围提示已关闭", "Range hints are disabled") end
        target = target or G.TheInput:GetWorldEntityUnderMouse()
        for i, pinned in ipairs(pins) do
            if pinned.target == target and not pinned.rules then
                table.remove(pins, i)
                suppressed_hover = target
                return true, nil, false
            end
        end
        if not self:can_pin(target) then
            return false, ctx:text("目标没有可用的原生范围规则", "No native range rule for this target")
        end
        add_pin(target)
        return true, nil, true
    end

    function self:toggle_group(target, show)
        if not self:can_pin(target) then return false end
        local prefab = Ranges.alias(target.prefab)
        local found = false
        for i = #pins, 1, -1 do
            local pin = pins[i]
            if not pin.rules and Ranges.alias(pin.target.prefab) == prefab then
                found = true
                table.remove(pins, i)
            end
        end
        if show == nil then show = not found end
        if show then
            local matching = Query.nearby(ctx, 80, nil, nil, nil, function(entity)
                return Ranges.alias(entity.prefab) == prefab and self:can_pin(entity)
            end, 64)
            for _, entity in ipairs(matching) do add_pin(entity) end
        else suppressed_hover = target end
        return true
    end

    function self:custom(radius, target)
        radius = tonumber(radius)
        target = target or ctx.player
        if not ctx.config.range_hints or not radius or radius ~= radius or radius < .1 or radius > 128
            or not M.observed(ctx, target) then return false end
        add_pin(target, { { radius = radius, zh = "测量范围", en = "Measured radius", precision = "custom" } })
        return true
    end

    function self:custom_group(radius, target)
        if not target or not M.observed(ctx, target) then return false end
        local prefab = Ranges.alias(target.prefab)
        for _, entity in ipairs(Query.nearby(ctx, 80, nil, nil, nil, function(entity)
            return M.observed(ctx, entity) and Ranges.alias(entity.prefab) == prefab
        end, 64)) do
            if not self:custom(radius, entity) then return false end
        end
        return true
    end

    function self:clear_pins()
        pins = {}
        suppressed_hover = G.TheInput:GetWorldEntityUnderMouse()
        return true
    end
    function self:status()
        return #pins > 0 and ctx:text("固定范围 ", "Pinned ranges ") .. #pins .. "/64" or ""
    end

    local function update_ranges(frame)
        if not ctx.config.range_hints then return end
        local pinned = {}
        for i = #pins, 1, -1 do
            local record, target = pins[i], pins[i].target
            local item = target.replica and target.replica.inventoryitem
            if not Query.visible(ctx, target) or item and item:IsHeld()
                or record.platform and not record.platform:IsValid() then
                table.remove(pins, i)
                if G.TheInput:GetWorldEntityUnderMouse() == target then suppressed_hover = target end
            else
                local observed = M.observed(ctx, target)
                if observed then
                    local center = record.point or point(target)
                    if record.platform then
                        local x, _, z = record.platform.entity:LocalToWorldSpace(center.x, 0, center.z)
                        center = { x = x, z = z }
                    end
                    record.snapshot = describe_range(record.key, target.prefab, center, target, record.rules)
                end
                -- Keep visible ring edges after the centre leaves the screen, without following unseen movement.
                local snapshot = record.snapshot
                if snapshot then
                    frame.ranges[#frame.ranges + 1] = { key = snapshot.key, point = snapshot.point, rules = snapshot.rules,
                        label = snapshot.label .. (not observed and ctx:text(" · 最后观察位置", " · last observed position") or "") }
                    pinned[target] = true
                end
            end
        end
        if ctx.config.range_preview ~= false then
            local placement = ctx.placement and ctx.placement:context()
            if placement and placement.kind ~= "till" then
                local prefab = placement.recipe and placement.recipe.product or placement.item and placement.item.prefab
                if add_range(frame, "range:placement", prefab, placement.point, nil, nil, true) then return end
            end
            -- Native placers still exist when geometry assistance is disabled.
            local controller = ctx.player.components and ctx.player.components.playercontroller
            local placer = controller and (controller.placer or controller.deployplacer)
            if placer and placer:IsValid() and placer.entity:IsVisible() then
                local component = placer.components and placer.components.placer
                local prefab = component and component.recipe and component.recipe.product or placer.prefab
                if add_range(frame, "range:placement", prefab, point(placer), nil, nil, true) then return end
            end
        end
        local modifier = ctx.config.range_hover_modifier or "none"
        local key = ({ ctrl = G.KEY_CTRL, alt = G.KEY_ALT, shift = G.KEY_SHIFT })[modifier]
        local target = G.TheInput:GetWorldEntityUnderMouse()
        if target ~= suppressed_hover then suppressed_hover = nil end
        if modifier ~= "off" and (not key or G.TheInput:IsKeyDown(key)) and not G.TheInput:GetHUDEntityUnderMouse()
            and target and not pinned[target] and target ~= suppressed_hover and M.observed(ctx, target) then
            if add_range(frame, "range:hover", target.prefab, point(target), target) then return end
        end
        if ctx.config.range_inventory == false then return end
        target = inventory_hover(ctx)
        local inventory = ctx.player.replica.inventory
        if not target and inventory then
            target = inventory:GetActiveItem() or inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
        end
        if target and target:IsValid() then
            add_range(frame, "range:tool", target.prefab, point(ctx.player), target, nil, true)
        end
    end

    local function scan(frame, now)
        local visible, observed = {}, {}
        local x, _, z = ctx.player.Transform:GetWorldPosition()
        -- Loot evidence includes visible death/rock FX; private and off-screen state remains excluded.
        local entities = G.TheSim:FindEntities(x, 0, z, 64, nil, { "INLIMBO", "CLASSIFIED", "DECOR" })
        local nearest_bosses = {}
        for _, entity in ipairs(entities) do
            if M.observed(ctx, entity, true) then
                observed[#observed + 1] = entity
                local boss_target = entity:HasTag("epic") or entity.prefab == "klaus_sack"
                    or entity.prefab == "toadstool_cap" and entity._state and entity._state:value() > 0
                if ctx.config.boss_indicators and boss_target and not entity:HasTag("NOCLICK") then
                    local id = entity.GUID
                    visible[id] = true
                    local health = entity.replica and entity.replica.health
                    if not health or not health:IsDead() then
                        nearest_bosses[#nearest_bosses + 1] = { entity = entity, distance = ctx.player:GetDistanceSqToInst(entity) }
                        table.sort(nearest_bosses, function(a, b)
                            return a.distance < b.distance or a.distance == b.distance and a.entity.GUID < b.entity.GUID
                        end)
                        nearest_bosses[5] = nil
                    else bosses[id] = nil end
                elseif bosses[entity.GUID] then
                    bosses[entity.GUID] = nil
                end
            end
        end
        frame.loot = loot:observe(ctx.config.loot_hints and observed or {}, now)
        for _, candidate in ipairs(nearest_bosses) do
            local entity = candidate.entity
            bosses[entity.GUID] = { entity = entity, point = point(entity), time = now, name = entity:GetDisplayName() }
        end
        local ordered = {}
        for id, record in pairs(bosses) do
            if not ctx.config.boss_indicators or not record.entity:IsValid() or now - record.time > 45 then
                bosses[id] = nil
            else
                ordered[#ordered + 1] = { id = id, record = record }
            end
        end
        table.sort(ordered, function(a, b) return a.record.time > b.record.time end)
        for i, entry in ipairs(ordered) do
            if i > 4 then bosses[entry.id] = nil
            elseif not visible[entry.id] then
                local record = entry.record
                frame.bosses[#frame.bosses + 1] = { key = "boss:" .. entry.id, point = record.point,
                    label = record.name .. ctx:text(" · 最后看到 ", " · last seen ")
                        .. math.floor(now - record.time) .. "s" }
            end
        end
    end

    local function update()
        local frame = { ranges = {}, bosses = {}, loot = {} }
        if ctx:ready() and (not G.TheFrontEnd or G.TheFrontEnd:GetActiveScreen() == ctx.hud) then
            update_ranges(frame)
            if ctx.config.boss_indicators or ctx.config.loot_hints then scan(frame, G.GetTime()) end
        else loot:observe({}, G.GetTime()) end
        self.frame = frame
        renderer:render(frame)
    end
    ctx.scope:defer(function() pins, bosses = {}, {}; renderer:close() end)
    ctx.scope:periodic(.25, update)
    update()
    return self
end

return M
