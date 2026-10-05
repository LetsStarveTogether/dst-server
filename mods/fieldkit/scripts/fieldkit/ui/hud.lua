local Widget = require("widgets/widget")
local Text = require("widgets/text")
local TextButton = require("widgets/textbutton")
local Status = require("fieldkit/ui/status")

local function number(value, quality)
    if value == nil or quality == "unknown" then return "?" end
    return (quality == "estimated" and "≈" or "") .. string.format("%.0f", value)
end

local function translated(ctx, names, value)
    local name = names[value]
    return name and ctx:text(name[1], name[2]) or "?"
end

local nightmares = { calm = { "平静", "Calm" }, warn = { "预警", "Warning" },
    wild = { "暴动", "Wild" }, dawn = { "消退", "Fading" } }

local Hud = Class(Widget, function(self, ctx)
    Widget._ctor(self, "fieldkit_status")
    self.ctx = ctx
    local G, scale = ctx.G, ctx.config.text_scale
    self:SetHAnchor(G.ANCHOR_LEFT)
    self:SetVAnchor(G.ANCHOR_TOP)
    self:SetScaleMode(G.SCALEMODE_PROPORTIONAL)
    self:SetPosition(180, -50)
    local function button(label, x, fn)
        local item = self:AddChild(TextButton())
        item:SetFont(G.BODYTEXTFONT)
        item:SetTextSize(23 * scale)
        item:SetText(label)
        item:SetPosition(x, 0)
        item:SetOnClick(fn)
        return item
    end
    self.open = button("Fieldkit", 0, function() ctx:open() end)
    self.stop = button(ctx:text("停止", "Stop"), 120, function()
        ctx.params.auto_repair = false
        ctx.executor:cancel("cancelled")
        ctx.selection:clear()
    end)
    self.status = Status.new(ctx, self)
    self.labels = {}
    for i = 1, 4 do
        self.labels[i] = self:AddChild(Text(G.BODYTEXTFONT, 20 * scale))
        self.labels[i].fieldkit_announcement = i == 1 and "nightmare" or i == 2 and "ride" or nil
        self.labels[i]:SetClickable(i <= 2)
    end
    ctx.scope:defer(function() self:Kill() end)
    ctx.scope:periodic(.25, function() self:Refresh() end)
    self:Refresh()
end)

function Hud.lines(ctx, snapshot)
    snapshot = snapshot or ctx.information:snapshot()
    local summary, detail, activity = {}, {}, {}
    local nightmare = snapshot.nightmare
    if nightmare.visible then
        local phase = ctx:text("梦魇 ", "Nightmare ") .. translated(ctx, nightmares, nightmare.phase)
        if nightmare.progress then phase = phase .. " " .. number(nightmare.progress * 100) .. "%" end
        summary[#summary + 1] = phase .. " " .. number(nightmare.remaining, nightmare.quality) .. "s"
    end
    local ride = snapshot.ride
    if ride then
        local remaining = "?"
        if ride.quality ~= "unknown" and ride.remaining_min and ride.remaining_max then
            remaining = number(ride.remaining_min, ride.quality) .. "–" .. number(ride.remaining_max)
        end
        detail[#detail + 1] = ctx:text("骑乘 ", "Riding ") .. number(ride.observed) .. "s / " .. remaining .. "s"
    end
    if ctx.executor.current then activity[#activity + 1] = ctx.executor.current.name end
    if ctx.message_until and ctx.G.GetTime() < ctx.message_until then
        activity[#activity + 1] = ctx.message
    end
    for _, service in ipairs({ ctx.selection, ctx.placement, ctx.awareness }) do
        local status = service:status()
        if status ~= "" then activity[#activity + 1] = status end
    end
    local timers = {}
    if ctx.timers and ctx.params.timer_hud ~= false then
        local state = ctx.timers:snapshot()
        for i = 1, math.min(2, #state.buffs) do
            local buff = state.buffs[i]
            timers[#timers + 1] = buff.label .. " ≈" .. ctx.timers:duration(buff.remaining)
        end
        timers[#timers + 1] = ctx.timers:weather_label()
    end
    return { table.concat(summary, " · "), table.concat(detail, " · "), table.concat(activity, " · "), table.concat(timers, " · ") }
end

function Hud:Refresh()
    local ctx = self.ctx
    if ctx.G.ThePlayer ~= ctx.player or ctx.player.HUD ~= ctx.hud then ctx.scope:close(); return end
    local snapshot = ctx.information:snapshot()
    self.status:Refresh(snapshot)
    local top = 42 + self.status.height
    for i, text in ipairs(Hud.lines(ctx, snapshot)) do
        local label = self.labels[i]
        if text ~= "" then
            label:Show()
            label:SetTruncatedString(text:gsub("%s+", " "), 600, 200, true)
            local width = label:GetRegionSize()
            label:SetPosition(-140 + width / 2, -top - (i - 1) * 28 * ctx.config.text_scale)
        else label:Hide() end
    end
    if ctx.minimap then ctx.minimap:layout(50 + top + #self.labels * 28 * ctx.config.text_scale + 24) end
    if ctx.G.TheFrontEnd:GetActiveScreen() == ctx.hud then self:Show() else self:Hide() end
end

function Hud.new(ctx)
    return ctx.hud.root:AddChild(Hud(ctx))
end

return Hud
