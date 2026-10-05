local Screen = require("widgets/screen")
local Widget = require("widgets/widget")
local Text = require("widgets/text")
local Image = require("widgets/image")

local Wheel = Class(Screen, function(self, ctx, hold_key)
    Screen._ctor(self, "FieldkitEmotes")
    self.ctx, self.hold_key, self.world = ctx, hold_key, ctx.G.TheWorld
    self.appearance = ctx.emotes:appearance()
    self.page = 1
    self.entries, self.has_favourites = ctx.emotes:wheel_entries()
    self.root = self:AddChild(Widget("fieldkit_emotes"))
    self.root:SetHAnchor(ANCHOR_MIDDLE)
    self.root:SetVAnchor(ANCHOR_MIDDLE)
    self.root:SetScaleMode(SCALEMODE_PROPORTIONAL)
    self.background = self.root:AddChild(Image("images/global.xml", "square.tex"))
    self.background:SetSize(540, 540)
    self.background:SetTint(.055, .065, .075, .95)
    self.title = self.root:AddChild(Text(UIFONT, 28))
    self.title:SetPosition(0, 232)
    self.title:SetClickable(false)
    self.label = self.root:AddChild(Text(BODYTEXTFONT, 22 * ctx.config.text_scale))
    self.label:SetClickable(false)
    self.hint = self.root:AddChild(Text(BODYTEXTFONT, 18))
    self.hint:SetPosition(0, -238)
    self.hint:SetClickable(false)
    self.hint:SetString(ctx:text("指向后松开 · 回中取消 · 滚轮 / PgUp PgDn 翻页",
        "Point and release · Center cancels · Wheel / PgUp PgDn: page"))
    self.content = self.root:AddChild(Widget("emote_entries"))
    self:Rebuild()
end)

-- Eight stable compass sectors, including empty sectors on the final page.
function Wheel.sector(x, y, deadzone)
    if x * x + y * y < deadzone * deadzone then return nil end
    return math.floor((math.atan2(x, y) + math.pi / 8) % (2 * math.pi) / (math.pi / 4)) + 1
end

function Wheel:Choose(index)
    self.selected = index and self.entries[(self.page - 1) * 8 + index] and index or nil
    for i, slot in ipairs(self.slots) do
        local selected = self.selected == i
        slot.icon:SetTint(selected and 1 or .65, selected and 1 or .65, selected and 1 or .65, 1)
        slot.icon:SetScale(selected and .8 or .65)
    end
    local entry = self.selected and self.entries[(self.page - 1) * 8 + self.selected]
    local animation = entry and entry.animation
    if self.appearance and animation then
        local mode = self.appearance.mode
        -- Other banks have their own animations; keep that form's native resting pose.
        if mode.anim_bank and mode.anim_bank ~= "wilson" then animation = mode.idle_anim or "idle_loop" end
        if not self.puppet then
            self.puppet = self.root:AddChild(require("widgets/skinspuppet")())
            self.puppet:SetClickable(false)
            self.puppet.enable_idle_emotes = false
            self.puppet:SetPosition(0, -48)
            self.label:SetPosition(0, -76)
        end
        if self.preview_appearance ~= self.appearance then
            self.puppet:SetScale(.8 * (mode.scale or 1))
            self.puppet:SetSkins(self.appearance.prefab, self.appearance.base, self.appearance.clothes, true, mode, self.appearance.curse)
            self.preview_appearance, self.preview_animation = self.appearance, nil
        end
        if self.preview_animation ~= animation then
            self.puppet.animstate:SetPercent(animation, .5)
            self.preview_animation = animation
        end
        self.puppet:Show()
    elseif self.puppet then
        self.puppet:Hide()
    end
    self.label:SetTruncatedString(entry and entry.label or self.ctx:text("取消", "Cancel"), 170, nil, true)
end

function Wheel:Rebuild()
    self.content:KillAllChildren()
    self.slots, self.stick_active = {}, false
    local pages = math.max(1, math.ceil(#self.entries / 8))
    self.page = (self.page - 1) % pages + 1
    self.title:SetString((self.has_favourites and self.page == 1 and self.ctx:text("常用表情", "Favourite emotes")
        or self.ctx:text("表情", "Emotes")) .. " · " .. self.page .. "/" .. pages)
    for i = 1, 8 do
        local entry = self.entries[(self.page - 1) * 8 + i]
        if entry then
            local angle = (i - 1) * math.pi / 4
            local x, y = math.sin(angle) * 174, math.cos(angle) * 174
            local icon = self.content:AddChild(Image(entry.atlas or "images/global.xml", entry.texture or "square.tex"))
            if entry.disabled then icon:SetSize(34, 34) end
            icon:SetPosition(x, y + 8)
            icon:SetClickable(false)
            local label = self.content:AddChild(Text(BODYTEXTFONT, 19 * self.ctx.config.text_scale))
            label:SetPosition(x, y - 40)
            label:SetTruncatedString(entry.label, 135, nil, true)
            label:SetClickable(false)
            self.slots[i] = { icon = icon, label = label }
        end
    end
    self:Choose(nil)
end

function Wheel:ChangePage(delta)
    self.page = self.page + delta
    self:Rebuild()
    self.last_mouse = self.ctx.G.TheInput:GetScreenPosition()
end

function Wheel:OnBecomeActive()
    Wheel._base.OnBecomeActive(self)
    if self.cancelled then self:Close(); return end
    self:SetFocus()
    if self.positioned then return end
    self.positioned = true
    local G = self.ctx.G
    self.last_mouse = G.TheInput:GetScreenPosition()
    if G.TheInput:ControllerAttached() then
        self.hint:SetString(self.ctx:text("摇杆回中执行 · 方向键 / 确认 · 肩键翻页",
            "Release stick to run · D-pad / Accept · Shoulders: page"))
    elseif not self.hold_key then
        self.hint:SetString(self.ctx:text("点击执行 · 中心取消 · 滚轮 / PgUp PgDn 翻页",
            "Click to run · Center cancels · Wheel / PgUp PgDn: page"))
    end
    if not G.TheInput:ControllerAttached() then
        local width, height = G.TheSim:GetScreenSize()
        local scale = self.root:GetWorldScale()
        local function offset(value, extent, factor)
            local margin = math.min(275 * factor, extent / 2)
            return (math.max(margin, math.min(extent - margin, value)) - extent / 2) / factor
        end
        self.root:SetPosition(offset(self.last_mouse.x, width, scale.x), offset(self.last_mouse.y, height, scale.y))
    end
end

function Wheel:OnBecomeInactive()
    Wheel._base.OnBecomeInactive(self)
    if self.closed then return end
    self.cancelled = true
    self:Hide()
    -- Native PopScreen also calls this method while mutating its stack.
    self.inst:DoTaskInTime(0, function() self:Close() end)
end

function Wheel:OnDestroy()
    self.closed = true
    if self.ctx.screen == self then self.ctx.screen = nil end
    Wheel._base.OnDestroy(self)
end

function Wheel:Close(commit)
    if self.closed then return end
    local ctx, G = self.ctx, self.ctx.G
    local entry = commit and not self.cancelled and self.selected
        and self.entries[(self.page - 1) * 8 + self.selected]
    self.closed = true
    if ctx.screen == self then ctx.screen = nil end
    G.TheFrontEnd:PopScreen(self)
    if not entry or entry.disabled or ctx.scope.closed or G.TheWorld ~= self.world then return end
    local ok, success, reason = pcall(ctx.emotes.execute, ctx.emotes, entry.name)
    if not ok then ctx:report(success)
    elseif success == false and reason then ctx:notify(reason) end
end

function Wheel:ReadPointer(force)
    local pos = self.ctx.G.TheInput:GetScreenPosition()
    if not force and self.last_mouse and pos.x == self.last_mouse.x and pos.y == self.last_mouse.y then return end
    self.last_mouse = pos
    local origin, scale = self.root:GetWorldPosition(), self.root:GetWorldScale()
    self:Choose(Wheel.sector((pos.x - origin.x) / scale.x, (pos.y - origin.y) / scale.y, 52))
end

function Wheel:OnUpdate()
    if self.closed then return end
    local ctx, G = self.ctx, self.ctx.G
    local player = ctx.player
    if self.cancelled or ctx.scope.closed or G.ThePlayer ~= player or player.HUD ~= ctx.hud
        or G.TheWorld ~= self.world or not player:IsValid() or player:HasTag("playerghost")
        or player.replica.health and player.replica.health:IsDead()
        or G.TheFrontEnd:GetActiveScreen() ~= self or G.IsPaused() or G.TheNet:IsServerPaused() then
        self:Close(); return
    end
    if self.appearance and self.appearance.build ~= player.AnimState:GetBuild() then
        self.appearance = ctx.emotes:appearance()
        self:Choose(self.selected)
    end
    local input = G.TheInput
    if input:ControllerAttached() then
        local x = input:GetAnalogControlValue(CONTROL_MOVE_RIGHT) - input:GetAnalogControlValue(CONTROL_MOVE_LEFT)
        local y = input:GetAnalogControlValue(CONTROL_MOVE_UP) - input:GetAnalogControlValue(CONTROL_MOVE_DOWN)
        local index = Wheel.sector(x, y, G.TUNING.CONTROLLER_DEADZONE_RADIUS)
        if index then
            self.stick_active = true
            self:Choose(index)
        elseif self.stick_active then
            self.stick_active = false
            if not self.hold_key then self:Close(true) end
        end
    else
        self:ReadPointer()
    end
end

function Wheel:OnControl(control, down)
    if self.closed then return true end
    if control == CONTROL_CANCEL or control == CONTROL_CONTROLLER_ALTACTION then
        if not down then self:Close() end
    elseif control == CONTROL_ACCEPT or control == CONTROL_CONTROLLER_ACTION then
        if down then
            self.accept_armed = true
        elseif self.accept_armed then
            if self.ctx.G.TheFrontEnd.isprimary then self:ReadPointer(true) end
            self:Close(true)
        end
    elseif control == CONTROL_SCROLLBACK or control == CONTROL_SCROLLFWD then
        if down then self:ChangePage(control == CONTROL_SCROLLBACK and -1 or 1) end
    end
    return true
end

function Wheel:OnFocusMove(direction, down)
    -- FrontEnd polls focus separately from OnControl, including the same analog stick.
    if down and not self.closed and not self.stick_active then
        local count = #self.slots
        local delta = (direction == MOVE_LEFT or direction == MOVE_UP) and -1 or 1
        if count > 0 then self:Choose(((self.selected or (delta == 1 and 0 or 1)) - 1 + delta) % count + 1) end
    end
    return true
end

function Wheel:OnRawKey(key, down)
    if self.closed then return true end
    if key == KEY_ESCAPE then
        if not down then self:Close() end
    elseif key == self.hold_key then
        if not down then self:ReadPointer(); self:Close(true) end
    elseif down and (key == KEY_PAGEUP or key == KEY_PAGEDOWN) then
        self:ChangePage(key == KEY_PAGEUP and -1 or 1)
    end
    return true
end

function Wheel:OnMouseButton()
    -- The same wheel input also reaches OnControl; only that mapped event changes pages.
    return true
end

return Wheel
