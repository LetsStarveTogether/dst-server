local Widget = require("widgets/widget")
local Image = require("widgets/image")
local Text = require("widgets/text")
local Information = require("fieldkit/game/information")
local Metrics = require("fieldkit/game/metrics")
local M = {}
local order = { "autumn", "winter", "spring", "summer" }
local seasons = { autumn = { "秋", "Autumn", .78, .38, .12 }, winter = { "冬", "Winter", .38, .66, .9 },
    spring = { "春", "Spring", .42, .7, .3 }, summer = { "夏", "Summer", .95, .72, .18 } }
local moons = { new = { "新月", "New moon" }, quarter = { "眉月", "Crescent" },
    half = { "半月", "Half moon" }, threequarter = { "凸月", "Gibbous" }, full = { "满月", "Full moon" } }

local function number(value)
    return value == nil and "?" or string.format("%.0f", value)
end
local function season(ctx, name)
    local label = seasons[name]
    return label and ctx:text(label[1], label[2]) or "?"
end
function M.rows(ctx, snapshot)
    if not ctx.config.information_hud then return {} end
    local world, own = snapshot.world, snapshot.own
    local rows = {}
    local left = world.endless and ctx:text("无尽", "Endless") or number(world.remaining_days) .. ctx:text(" 天", "d")
    rows[#rows + 1] = { key = "world", text = ctx:text("第 ", "Day ") .. number(world.day)
        .. " · " .. season(ctx, world.season) .. " " .. number(world.season_day) .. " · " .. left }
    if world.next_season then
        rows[#rows + 1] = { key = "world", text = ctx:text("下一季：", "Next: ") .. season(ctx, world.next_season) }
    end
    if own.temperature.value ~= nil then
        rows[#rows + 1] = { key = "temperature", icon = "heat_rock3.tex",
            text = ctx:text("体温 ", "Body ") .. Information.temperature(own.temperature.value) }
    end
    if world.temperature ~= nil then
        rows[#rows + 1] = { key = "world_temperature", icon = "winterometer.tex",
            text = ctx:text("环境 ", "Air ") .. Information.temperature(world.temperature) }
    end
    local label = moons[world.moonphase]
    local text = label and ctx:text(label[1], label[2]) or "?"
    if label and world.moonphase ~= "full" and world.moonphase ~= "new" then
        text = text .. (world.waxing == nil and " ?" or world.waxing and " ↑" or " ↓")
    end
    local full = world.fullmoon or {}
    local estimate = full.min_day and "≈" .. number(full.min_day)
        .. (full.max_day ~= full.min_day and "–" .. number(full.max_day) or "") or "?"
    rows[#rows + 1] = { key = "moon", text = text .. " · " .. ctx:text("满月日 ", "Full moon day ") .. estimate }
    if snapshot.naughtiness then
        rows[#rows + 1] = { key = "naughtiness", text = ctx:text("淘气 ", "Naughtiness ")
            .. number(snapshot.naughtiness.actions) .. "/" .. number(snapshot.naughtiness.threshold) }
    end
    return rows
end

local function native(ctx)
    if not ctx.config.information_hud then return end
    local controls = ctx.hud.controls
    if not controls then return end
    local clock = controls.clock
    if not clock then return end
    local function moon()
        if not clock._moonanim then return end
        clock._moonanim:Show()
        clock:ShowMoon()
    end
    for _, method in ipairs({ "OnPhaseChanged", "OnMoonPhaseChanged2", "OnMoonPhaseStyleChanged" }) do
        ctx.scope:wrap(clock, method, function(original)
            return function(widget, ...)
                original(widget, ...)
                moon()
            end
        end)
    end
    if clock._moonanim then
        ctx.scope:defer(function()
            if clock.inst and not clock.inst:IsValid() then return end
            clock._moonanim:Show()
            if clock._phase == "night" then clock:ShowMoon()
            else clock._moonanim:GetAnimState():PlayAnimation("hidden") end
        end)
    end
    if clock._text then clock._text:Show() end
    moon()
end

function M.install_numbers(ctx)
    if not ctx.config.status_numbers then return end
    local status = ctx.hud and ctx.hud.controls and ctx.hud.controls.status
    if not status then return end
    local installed = setmetatable({}, { __mode = "k" })
    local expanded = false
    local function bind(name)
        local meter = status[name]
        local number = meter and meter.num
        if number and not installed[number] then
            installed[number] = true
            local requested = number.shown
            local show, hide = number.Show, number.Hide
            local function available()
                return (name ~= "moisturemeter" or meter.active)
                    and (name ~= "boatmeter" or meter.boat ~= nil)
            end
            local rawtext, maxlabel
            local detailed = ctx.config.status_numbers == "detailed"
            local native_string, native_setscale = number.SetString, number.SetScale
            local native_scale = detailed and number.GetLooseScale and { number:GetLooseScale() }
            local key = Metrics.widget(ctx, meter)
            rawtext = number.GetString and number:GetString()
            if meter.AddChild then
                maxlabel = meter:AddChild(require("widgets/text")(ctx.G.NUMBERFONT, 19 * (ctx.config.text_scale or 1)))
                maxlabel:SetPosition(0, -38)
                maxlabel:SetClickable(false)
                ctx.scope:defer(function()
                    if not maxlabel.inst or maxlabel.inst:IsValid() then maxlabel:Kill() end
                end)
            end
            local function update_number()
                local state = key and Metrics.get(ctx, key)
                local maximum = state and state.max
                if rawtext then native_string(number, detailed and maximum
                    and tostring(rawtext) .. "/" .. tostring(math.ceil(maximum)) or rawtext) end
                if maxlabel then
                    maxlabel:SetString(ctx:text("最大值 ", "Max ")
                        .. (maximum and tostring(math.ceil(maximum)) or "?"))
                    if (meter.focus or expanded) and available() then maxlabel:Show() else maxlabel:Hide() end
                end
            end
            installed[number] = update_number
            ctx.scope:wrap(number, "SetString", function()
                return function(_, value) rawtext = value; update_number() end
            end)
            for _, method in ipairs({ "SetPercent", "SetValue", "RefreshHealth" }) do
                if meter[method] then
                    ctx.scope:wrap(meter, method, function(original)
                        return function(widget, value, max, ...)
                            local function finish(...)
                                update_number()
                                return ...
                            end
                            return finish(original(widget, value, max, ...))
                        end
                    end)
                end
            end
            for _, method in ipairs({ "OnGainFocus", "OnLoseFocus" }) do
                if meter[method] then
                    ctx.scope:wrap(meter, method, function(original)
                        return function(widget, ...)
                            local result = original(widget, ...)
                            update_number()
                            return result
                        end
                    end)
                end
            end
            if native_scale then
                ctx.scope:wrap(number, "SetScale", function()
                    return function(_, x, y, z)
                        native_scale = type(x) == "number" and { x, y or x, z or x } or { x.x, x.y, x.z }
                        native_setscale(number, native_scale[1] * .72, native_scale[2] * .72, native_scale[3] * .72)
                    end
                end)
                native_setscale(number, native_scale[1] * .72, native_scale[2] * .72, native_scale[3] * .72)
            end
            ctx.scope:defer(function()
                if number.inst and not number.inst:IsValid() then return end
                if rawtext then native_string(number, rawtext) end
                if native_scale then native_setscale(number, unpack(native_scale)) end
            end)
            update_number()
            local function refresh()
                update_number()
                if available() then return show(number) end
                return hide(number)
            end
            ctx.scope:wrap(number, "Show", function()
                return function()
                    requested = true
                    return refresh()
                end
            end)
            ctx.scope:wrap(number, "Hide", function()
                return function()
                    requested = false
                    return refresh()
                end
            end)
            local methods = name == "moisturemeter" and { "SetValue" }
                or name == "boatmeter" and { "Enable", "Disable" } or {}
            for _, method in ipairs(methods) do
                ctx.scope:wrap(meter, method, function(original)
                    return function(...)
                        local function finish(...)
                            refresh()
                            return ...
                        end
                        return finish(original(...))
                    end
                end)
            end
            ctx.scope:defer(function()
                if not number.inst or number.inst:IsValid() then
                    if requested then show(number) else hide(number) end
                end
            end)
            refresh()
        end
    end
    for _, key in ipairs(Metrics.order) do
        local definition = Metrics.definition(key)
        if definition.widget and not definition.group then bind(definition.widget) end
    end
    for method, name in pairs({ AddWereness = "wereness", AddPetHunger = "pethungerbadge",
        AddInspiration = "inspirationbadge", AddMightiness = "mightybadge" }) do
        if status[method] then
            ctx.scope:wrap(status, method, function(original)
                return function(widget, ...)
                    local result = original(widget, ...)
                    bind(name)
                    return result
                end
            end)
        end
    end
    for _, method in ipairs({ "ShowStatusNumbers", "HideStatusNumbers" }) do
        if status[method] then
            ctx.scope:wrap(status, method, function(original)
                return function(widget, ...)
                    expanded = method == "ShowStatusNumbers"
                    local result = original(widget, ...)
                    for number, refresh in pairs(installed) do
                        if not number.inst or number.inst:IsValid() then refresh() end
                    end
                    return result
                end
            end)
        end
    end
end


function M.new(ctx, parent)
    M.install_numbers(ctx)
    native(ctx)
    local self = parent:AddChild(Widget("fieldkit_calendar"))
    local scale = ctx.config.text_scale or 1
    self:SetScale(scale)
    self:SetPosition(-130, -42)
    self.rows = {}
    for i = 1, 6 do
        local row = self:AddChild(Widget("status_row"))
        row.label = row:AddChild(Text(ctx.G.BODYTEXTFONT, 21))
        row.icon = row:AddChild(Image())
        row.icon:SetPosition(-13, 0)
        row.icon:SetClickable(false)
        self.rows[i] = row
    end
    self.clock = self:AddChild(Widget("season_clock"))
    self.clock.fieldkit_announcement = "world"
    self.clock:SetPosition(42, -56)
    self.wedges = {}
    for i = 1, 16 do
        local wedge = self.clock:AddChild(Image("images/hud.xml", "clock_wedge.tex"))
        wedge:SetScale(.32)
        wedge:SetHRegPoint(ctx.G.ANCHOR_LEFT)
        wedge:SetVRegPoint(ctx.G.ANCHOR_BOTTOM)
        wedge:SetRotation((i - 1) * 22.5)
        wedge:SetClickable(false)
        self.wedges[i] = wedge
    end
    local rim = self.clock:AddChild(Image("images/hud.xml", "clock_rim.tex"))
    rim:SetScale(.4)
    self.hand = self.clock:AddChild(Image("images/hud.xml", "clock_hand.tex"))
    self.hand:SetScale(.4)
    self.hand:SetClickable(false)
    self.clocktext = self.clock:AddChild(Text(ctx.G.BODYTEXTFONT, 21))
    self.clocktext:SetPosition(0, -12)
    function self:Refresh(snapshot)
        local world = snapshot.world
        local graph = ctx.config.information_hud
        local rows = M.rows(ctx, snapshot)
        for i, row in ipairs(self.rows) do
            local data = rows[i]
            if data then
                row:Show()
                row.fieldkit_announcement = data.key
                local hasicon = data.icon ~= nil
                row.label:SetTruncatedString(data.text, 430, 100, true)
                local width = row.label:GetRegionSize()
                row.label:SetPosition(width / 2 + (hasicon and 13 or 0), 0)
                row:SetPosition(108, -(i - 1) * 27)
                if hasicon then
                    if row.icon.texture ~= data.icon then
                        row.icon:SetTexture(ctx.G.GetInventoryItemAtlas(data.icon), data.icon)
                        row.icon:SetSize(22, 22)
                    end
                    row.icon:Show()
                else row.icon:Hide() end
            else row:Hide() end
        end
        if graph then
            self.clock:Show()
            local lengths, total = world.season_lengths, world.year_length
            local before, angle = 0, nil
            if lengths and total and total > 0 and world.season_progress then
                for _, name in ipairs(order) do
                    if name == world.season then angle = 360 * (before + lengths[name] * world.season_progress) / total end
                    before = before + lengths[name]
                end
            end
            for i, wedge in ipairs(self.wedges) do
                local name, offset = world.endless and world.season or nil, 0
                if not name and lengths and total and total > 0 then
                    for _, entry in ipairs(order) do
                        offset = offset + lengths[entry]
                        if (i - .5) * total / 16 < offset then name = entry; break end
                    end
                end
                local tint = seasons[name]
                wedge:SetTint(tint and tint[3] or .35, tint and tint[4] or .35, tint and tint[5] or .35, 1)
            end
            if angle and not world.endless then self.hand:Show(); self.hand:SetRotation(angle) else self.hand:Hide() end
            self.clocktext:SetString(season(ctx, world.season))
            self.clocktext:Show()
        else self.clock:Hide() end
        self.height = math.max(#rows * 27, graph and 116 or 0) * scale
    end
    return self
end

return M
