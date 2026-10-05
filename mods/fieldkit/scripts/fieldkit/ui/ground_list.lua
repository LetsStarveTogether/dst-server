local Widget = require("widgets/widget")
local Text = require("widgets/text")
local TextEdit = require("widgets/textedit")
local TextButton = require("widgets/textbutton")
local Image = require("widgets/image")
local Indicators = require("fieldkit/ui/indicators")
local Parameters = require("fieldkit/core/parameters")

local PAGE_SIZE = 9
local SURFACES = { "all", "current", "land", "platform", "ocean" }
local SURFACE_NAMES = {
    all = { "全部地形", "All surfaces" }, land = { "陆地", "Land" },
    current = { "当前船／陆地", "Current boat / land" },
    platform = { "船与平台", "Platforms" }, ocean = { "海面", "Ocean" },
}

local GroundList = Class(Widget, function(self, ctx, execute)
    Widget._ctor(self, "fieldkit_ground_list")
    self.ctx, self.execute = ctx, execute
    self.indicators = Indicators.new(ctx)
    self.page, self.surface, self.held, self.query = 1, 1, false, ""
    self.buttons, self.tiles = {}, {}
    local search_background = self:AddChild(Image("images/global.xml", "square.tex"))
    search_background:SetPosition(-20, 158)
    search_background:SetSize(265, 34)
    search_background:SetTint(.15, .17, .19, 1)
    search_background:SetClickable(false)
    self.search = self:AddChild(TextEdit(BODYTEXTFONT, 20, ""))
    self.search:SetPosition(-20, 158)
    self.search:SetRegionSize(250, 30)
    self.search:SetTextLengthLimit(80)
    self.search:SetIdleTextColour(1, 1, 1, 1)
    self.search:SetEditTextColour(1, 1, 1, 1)
    self.search:SetEditCursorColour(1, 1, 1, 1)
    self.search:SetTextPrompt(ctx:text("搜索名称或 prefab", "Search name or prefab"), { .7, .7, .7, 1 })
    local set_editing = self.search.SetEditing
    self.search.SetEditing = function(search, editing)
        local changed = search.editing ~= editing
        set_editing(search, editing)
        if changed and self.on_editing then self.on_editing(editing) end
    end
    self.search.OnTextEntered = function()
        self:Refresh()
        if self.tiles[1].key then self.tiles[1]:SetFocus() end
    end
    self.buttons[1] = self.search
    self:Button(ctx:text("清空", "Clear"), 140, 158, function()
        self.search:SetString("")
        self:Refresh()
        self:FocusSearch()
    end)
    self.held_button = self:Button("", -85, 122, function()
        self.held, self.page, self.selected = not self.held, 1, nil
        self:Refresh()
    end)
    self.surface_button = self:Button("", 80, 122, function()
        self.surface, self.page, self.selected = self.surface % #SURFACES + 1, 1, nil
        self:Refresh()
    end)
    self.previous = self:Button("‹", -75, -68, function() self:ChangePage(-1) end)
    self.pages = self:AddChild(Text(BODYTEXTFONT, 20))
    self.pages:SetPosition(0, -68)
    self.next = self:Button("›", 75, -68, function() self:ChangePage(1) end)
    for index = 1, PAGE_SIZE do
        local tile = self:Button("", ((index - 1) % 3 - 1) * 108, 80 - math.floor((index - 1) / 3) * 52, function()
            local key = self.tiles[index].key
            self:Select(key)
            if key and ctx.config.work then ctx.ground:toggle(key); self:Refresh() end
        end)
        tile.image:SetSize(102, 46)
        tile.text:SetPosition(16, 0)
        tile.text:SetSize(18 * ctx.config.text_scale)
        tile.icon = tile:AddChild(Image("images/global.xml", "square.tex"))
        tile.icon:SetPosition(-35, 0)
        tile.icon:SetSize(26, 26)
        tile.icon:SetClickable(false)
        tile:SetOnGainFocus(function() self:Select(tile.key); self:Locate(tile.key) end)
        tile:SetOnLoseFocus(function() if self.located == tile.key then self:Locate(nil) end end)
        self.tiles[index] = tile
    end
    self.summary = self:AddChild(Text(BODYTEXTFONT, 18))
    self.summary:SetPosition(0, -99)
    self.one = self:Button(ctx:text("拾取一堆", "Pick one"), -108, -134, function() self:Pickup(false) end)
    if ctx.config.work then
        self.all = self:Button(ctx:text("拾取同组", "Pick group"), 0, -134, function() self:Pickup(true) end)
    end
    if ctx.config.announcements then
        self.announce = self:Button(ctx:text("宣告", "Announce"), 108, -134, function()
            local group = self.ctx.ground.groups[self.selected]
            if group then self.execute(function() return ctx.announcements:target(group.nearest) end) end
        end)
    end
    if ctx.config.work then
        self.ignore = self:Button("", -85, -171, function()
            ctx.params.ground_ignore_full = not ctx.params.ground_ignore_full
            self:Refresh()
        end)
        self.run = self:Button("", 80, -171, function()
            self.execute(function() return ctx.ground:pickup_selected() end)
            self:Refresh()
        end)
        self.clear = self:Button(ctx:text("清空选择", "Clear selected"), -85, -205, function()
            ctx.ground:clear()
            self:Refresh()
        end)
    end
    self.default_focus = self.search
    self:Refresh()
    self:StartUpdating()
end)

function GroundList:Button(label, x, y, fn)
    local button = self:AddChild(TextButton())
    button:SetFont(BODYTEXTFONT)
    button:SetTextSize(18 * self.ctx.config.text_scale)
    button:SetText(label)
    button:SetPosition(x, y)
    button:SetOnClick(function()
        self.search:SetEditing(false)
        return fn()
    end)
    self.buttons[#self.buttons + 1] = button
    return button
end

function GroundList:IsEditing() return self.search.editing end

function GroundList:FocusSearch()
    self.search:SetFocus()
    self.search:SetEditing(true)
end

function GroundList:Select(key)
    self.selected = key
    local group = key and self.ctx.ground.groups[key]
    local label = self.ctx:text("选择一个物品组", "Select an item group")
    if group then
        label = string.format("%s%s ×%d · %.1fm", group.name,
            group.skin_name and " · " .. group.skin_name or "", group.count, group.distance)
    end
    if not group then self:Locate(nil) end
    self.summary:SetTruncatedString(label, 320, nil, true)
    for _, button in pairs({ self.one, self.all, self.announce }) do
        if group then button:Enable() else button:Disable() end
    end
    self:LinkFocus()
end

function GroundList:Locate(key)
    self.located = key
    self.indicators:locate(key and self.ctx.ground:location(key) or nil)
end

function GroundList:Pickup(all)
    local key = self.selected
    if key then self.execute(function() return self.ctx.ground:pickup(key, all) end) end
end

function GroundList:ChangePage(delta)
    self.search:SetEditing(false)
    self.page, self.selected = math.max(1, math.min(self.page + delta, self.page_count)), nil
    self:Refresh()
    if self.tiles[1].key then self.tiles[1]:SetFocus() end
end

function GroundList:Refresh()
    if not self.summary then return end
    local query = self.search:GetString()
    if query ~= self.query then
        self.query, self.page, self.selected = query, 1, nil
    end
    local focused_key
    for _, tile in ipairs(self.tiles) do if tile.focus then focused_key = tile.key end end
    local groups = self.ctx.ground:search(query, { held = self.held, surface = SURFACES[self.surface] })
    self.page_count = math.max(1, math.ceil(#groups / PAGE_SIZE))
    self.page = math.min(self.page, self.page_count)
    if self.selected and not self.ctx.ground.groups[self.selected] then self.selected = nil end
    for index, group in ipairs(groups) do
        if group.key == self.selected then self.page = math.ceil(index / PAGE_SIZE); break end
    end
    self.held_button:SetText(self.ctx:text("手持过滤", "Held item") .. (self.held and " ✓" or ""))
    local surface_name = SURFACE_NAMES[SURFACES[self.surface]]
    self.surface_button:SetText(self.ctx:text(surface_name[1], surface_name[2]))
    self.pages:SetString(self.page .. "/" .. self.page_count)
    if self.run then
        local count = self.ctx.ground:selected_count()
        self.ignore:SetText(self.ctx:text("忽略满堆", "Ignore full stacks") .. (self.ctx.params.ground_ignore_full and " ✓" or ""))
        self.run:SetText(self.ctx:text("拾取所选 ", "Pick selected ") .. count)
        if count > 0 then self.run:Enable(); self.clear:Enable() else self.run:Disable(); self.clear:Disable() end
    end
    if self.page > 1 then self.previous:Enable() else self.previous:Disable() end
    if self.page < self.page_count then self.next:Enable() else self.next:Disable() end
    local focused_tile
    for index, tile in ipairs(self.tiles) do
        local group = groups[(self.page - 1) * PAGE_SIZE + index]
        tile.key = group and group.key
        if group then
            tile:Show()
            local label = (self.ctx.ground.selected[group.key] and "✓ " or "") .. group.name .. " ×" .. group.count
            tile:SetText(label)
            tile.text:SetMultilineTruncatedString(label, 2, 65, nil, true)
            tile.image:SetSize(102, 46)
            tile.icon:SetTexture(group.atlas, group.image)
            if group.key == focused_key then focused_tile = tile end
        else tile:Hide() end
    end
    self:Select(self.selected)
    if focused_tile then focused_tile:SetFocus()
    elseif focused_key then self.search:SetFocus() end
end

function GroundList:LinkFocus()
    local focusable = {}
    for _, button in ipairs(self.buttons) do
        if button:IsVisible() and button:IsEnabled() then focusable[#focusable + 1] = button end
    end
    for index, button in ipairs(focusable) do
        local previous, next_button = focusable[(index - 2) % #focusable + 1], focusable[index % #focusable + 1]
        button:SetFocusChangeDir(MOVE_UP, previous)
        button:SetFocusChangeDir(MOVE_LEFT, previous)
        button:SetFocusChangeDir(MOVE_DOWN, next_button)
        button:SetFocusChangeDir(MOVE_RIGHT, next_button)
    end
    for index, tile in ipairs(self.tiles) do
        if tile.key then
            local above, below = self.tiles[index - 3], self.tiles[index + 3]
            tile:SetFocusChangeDir(MOVE_UP, above and above.key and above or self.search)
            tile:SetFocusChangeDir(MOVE_DOWN, below and below.key and below
                or self.one.enabled and self.one or self.exit_focus or self.search)
        end
    end
    if self.exit_focus then
        self.search:SetFocusChangeDir(MOVE_UP, self.exit_focus)
        focusable[#focusable]:SetFocusChangeDir(MOVE_DOWN, self.exit_focus)
    end
end

function GroundList:SetExitFocus(button)
    self.exit_focus = button
    self:LinkFocus()
end

function GroundList:OnUpdate(dt)
    if not self:IsVisible() then self:Locate(nil); return end
    if self.located then self:Locate(self.located) end
    self.elapsed = (self.elapsed or 0) + dt
    if self.search:GetString() ~= self.query or self.elapsed >= .5 then self.elapsed = 0; self:Refresh() end
end

function GroundList:Kill()
    self.search:SetEditing(false)
    self:StopUpdating()
    self.indicators:close()
    GroundList._base.Kill(self)
end

function GroundList.dock(ctx)
    local G = ctx.G
    local self = { open = true, focused = false }
    local root = ctx.hud.root:AddChild(Widget("fieldkit_ground_dock"))
    self.root = root
    root:SetHAnchor(G.ANCHOR_RIGHT)
    root:SetVAnchor(G.ANCHOR_MIDDLE)
    root:SetScaleMode(G.SCALEMODE_PROPORTIONAL)
    local function layout(scale)
        root:SetScale(scale)
        root:SetPosition(-190 * scale, 0)
    end
    layout(G.TheFrontEnd:GetHUDScale())
    ctx.scope:listen("refreshhudsize", function(_, scale) layout(scale) end, ctx.hud.inst)
    local background = root:AddChild(Image("images/global.xml", "square.tex"))
    background:SetSize(340, 460)
    background:SetTint(.055, .065, .075, .94)

    function self:active()
        return not self.closed and not ctx.scope.closed and G.ThePlayer == ctx.player
            and ctx.player.HUD == ctx.hud and ctx.hud.shown ~= false
            and ctx.hud.root:IsVisible() and G.TheFrontEnd:GetActiveScreen() == ctx.hud
            and not ctx.player:HasTag("playerghost") and ctx.player:IsValid()
    end

    function self:release_focus()
        self.focused = false
        if self.list then self.list.search:SetEditing(false); self.list:Locate(nil) end
        ctx.hud:SetModFocus("fieldkit", "ground", false)
        root:ClearFocus()
    end

    function self:execute(fn)
        self:release_focus()
        if not self:active() then return false end
        local before = ctx.executor.current
        local ok, success, reason = pcall(fn)
        if not ok then ctx:report(success)
        elseif reason then ctx:notify(reason) end
        if ok and success and ctx.executor.current ~= before then self.job = ctx.executor.current end
        return ok and success
    end

    local list = root:AddChild(GroundList(ctx, function(fn) return self:execute(fn) end))
    self.list = list
    self.radius_button = list:Button("", 0, 198, function()
        Parameters.cycle(ctx.params, "search_radius")
        list:Refresh()
        self:refresh()
    end)
    self.radius_button:SetTextSize(22 * ctx.config.text_scale)
    list.on_editing = function(editing)
        if editing then
            ctx.executor:cancel("focus")
            self.focused = true
            ctx.hud:SetModFocus("fieldkit", "ground", true)
        elseif not G.TheInput:ControllerAttached() then
            self.focused = false
            ctx.hud:SetModFocus("fieldkit", "ground", false)
        end
    end

    function self:set_open(open)
        self.open = open == true
        if not self.open then
            self:release_focus()
            ctx.ground:clear()
            if self.job and ctx.executor.current == self.job then ctx.executor:cancel("cancelled") end
        end
        self:refresh()
        return true
    end

    function self:toggle() return self:set_open(not self.open) end

    function self:focus()
        if not self:active() then return false end
        self:set_open(true)
        ctx.executor:cancel("focus")
        self.focused = true
        ctx.hud:SetModFocus("fieldkit", "ground", true)
        if G.TheInput:ControllerAttached() then list.default_focus:SetFocus() else list:FocusSearch() end
        return true
    end

    function self:consumes_pointer()
        if not self.open or not self:active() then return false end
        local mouse, origin, scale = G.TheInput:GetScreenPosition(), root:GetWorldPosition(), root:GetWorldScale()
        return math.abs((mouse.x - origin.x) / scale.x) <= 170 and math.abs((mouse.y - origin.y) / scale.y) <= 230
    end

    function self:handle_control(control, down)
        if not self.open or not self:active() then return false end
        if self.focused then
            if list:IsEditing() and list.search:OnControl(control, down) then return true end
            if control == G.CONTROL_CANCEL then
                if not down then self:release_focus() end
                return true
            end
            Widget.OnControl(root, control, down)
            return true
        end
        return false
    end

    local close = list:Button(ctx:text("关闭", "Close"), 80, -205, function() self:set_open(false) end)
    list:SetExitFocus(close)
    root.OnControl = function(_, control, down)
        if self:handle_control(control, down) then return true end
        return Widget.OnControl(root, control, down)
    end
    root.OnRawKey = function(_, key, down)
        if list:IsEditing() then return Widget.OnRawKey(root, key, down) end
        if self.focused and key == G.KEY_ESCAPE then
            if not down then self:release_focus() end
            return true
        end
        return Widget.OnRawKey(root, key, down)
    end
    root:SetOnLoseFocus(function() self:release_focus() end)
    function self:refresh()
        self.radius_button:SetText(ctx:text("附近物品 · ", "Nearby items · ") .. ctx.params.search_radius .. "m ›")
        if self.open and self:active() then root:Show()
        else self:release_focus(); root:Hide() end
    end
    function self:close()
        if self.closed then return end
        self:set_open(false)
        self.closed = true
        root:Kill()
    end
    -- World markers do not inherit native HUD visibility, so release them synchronously.
    for _, method in ipairs({ "Hide", "OnBecomeInactive" }) do
        ctx.scope:wrap(ctx.hud, method, function(original)
            return function(...)
                self:release_focus()
                return original(...)
            end
        end)
    end
    ctx.scope:periodic(.1, function() self:refresh() end)
    ctx.scope:defer(function() self:close() end)
    self:refresh()
    return self
end

return GroundList
