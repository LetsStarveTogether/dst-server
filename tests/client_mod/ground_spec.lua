local T = require("tests.client_mod.support")
local Ground = require("fieldkit/features/ground")
local get_skin_name = T.eval("local EntityScript = {}\n"
    .. assert(T.source("entityscript"):match("function EntityScript:GetSkinName%b().-\nend"))
    .. "\nreturn EntityScript.GetSkinName")

local function entity(id, prefab, name, x, skin, count, surface)
    local e = T.entity({ GUID = id, prefab = prefab, name = name, x = x, skin = skin, skinname = skin,
        count = count or 1, surface = surface })
    e.GetSkinName = get_skin_name
    e.AnimState = { GetSkinBuild = function() return e.skin end }
    e.platform = surface == "platform" and {} or nil
    function e:GetCurrentPlatform() return self.surface == "platform" and self.platform or nil end
    function e:IsOnOcean() return self.surface == "ocean" end
    e.replica = {
        inventoryitem = { CanBePickedUp = function() return not e.unpickable end, IsHeld = function() return e.held end,
            GetAtlas = function() return "images/inventoryimages.xml" end, GetImage = function() return prefab .. ".tex" end },
        stackable = { StackSize = function() return e.count end, IsFull = function() return e.count >= 40 end },
    }
    return e
end
T.case("ground_search_and_native_ui", function()
    local player = entity(0, "wilson", "Wilson", 0)
    local log = entity(1, "log", "木头", 2, nil, 5)
    local log2 = entity(2, "log", "木头", 4, nil, 3, "platform")
    local skinned = entity(3, "log", "木头", 1, "log_fancy", 2)
    local flint = entity(4, "flint", "燧石", 3)
    local ocean = entity(5, "log", "木头", 5, nil, 2, "ocean")
    local literal = entity(6, "strange_item", "[独特]", 6)
    local hidden = entity(7, "log", "木头", 1); hidden.hidden = true
    local held = entity(8, "log", "木头", 1); held.held = true
    local found, active, equipped, picked = { log, log2, skinned, flint, ocean, literal, hidden, held }
    player.replica.inventory = {
        GetActiveItem = function() return active end, GetEquippedItem = function() return equipped end,
    }
    local ctx = { player = player, config = { text_scale = 1, work = true, announcements = true },
        params = require("fieldkit/core/parameters").new(),
        G = { EQUIPSLOTS = { HANDS = "hands" }, STRINGS = { SKIN_NAMES = { log_fancy = "Fancy" } },
            CanEntitySeeTarget = function() return true end, TheSim = { FindEntities = function() return found end } },
        work = { enqueue = function(_, entries) picked = entries; return true end,
            pickup = function(_, target) picked = target; return true end },
        announcements = { target = function(_, target) picked = target; return true end },
    }
    function ctx:text(_, en) return en end
    ctx.ground = Ground.new(ctx)
    local rows = ctx.ground:search("木")
    assert(#rows == 2 and ctx.ground.groups["log:"].count == 10)
    assert(ctx.ground.groups["log:"].nearest == log and #ctx.ground.groups["log:"].entities == 3)
    assert(ctx.ground.groups["log:log_fancy"].skin_name == "Fancy")
    T.each("literal_query", { { name = "LOG", count = 2 }, { name = "[", count = 1 } }, function(query)
        assert(#ctx.ground:search(query.name) == query.count, "search is case-insensitive and literal")
    end)
    T.each("surface", { { name = "land", count = 5, nearest = log }, { name = "platform", count = 3, nearest = log2 },
        { name = "ocean", count = 2, nearest = ocean } }, function(surface)
        ctx.ground:search("log", { surface = surface.name })
        local group = assert(ctx.ground.groups["log:"])
        assert(group.count == surface.count and group.nearest == surface.nearest)
    end)
    rows = ctx.ground:search("log", { surface = "platform" })
    assert(#rows == 1 and rows[1].count == 3 and rows[1].nearest == log2)
    assert(ctx.ground:pickup(rows[1].key, true) and #picked == 1 and picked[1].target == log2,
        "group pickup cannot broaden a surface or skin filter")
    active = skinned
    rows = ctx.ground:search("", { held = true })
    assert(#rows == 1 and rows[1].key == "log:log_fancy")
    active, equipped = nil, flint
    assert(ctx.ground:search("", { held = true })[1].prefab == "flint")
    equipped = nil
    assert(#ctx.ground:search("", { held = true }) == 0, "empty hands do not widen the filter")
    rows = ctx.ground:search("木", { surface = "land" })
    assert(ctx.ground:pickup("log:", false) and picked == log)
    T.each("stale_pickup", { { name = "skin", value = "changed" }, { name = "surface", value = "ocean" },
        { name = "hidden", value = true }, { name = "removed", value = true }, { name = "unpickable", value = true } }, function(change)
        ctx.ground:search("log", { surface = "land" })
        local before = log[change.name]
        log[change.name] = change.value
        log.skinname = log.skin
        assert(not ctx.ground:pickup("log:", true), "stale snapshots cannot pick a newly changed group")
        log[change.name] = before
        log.skinname = log.skin
    end)

    -- Explicit selections freeze membership across searches and reject changed terrain/skin.
    ctx.ground:search("log", { surface = "land" })
    assert(ctx.ground:toggle("log:"))
    ctx.ground:search("flint")
    assert(ctx.ground:toggle("flint:") and ctx.ground:selected_count() == 2)
    local newcomer = entity(9, "log", "木头", 3)
    found[#found + 1] = newcomer
    ctx.ground:search("")
    assert(ctx.ground:pickup_selected() and #picked == 2 and picked[1].target == log and picked[2].target == flint,
        "refresh and changed search cannot add new group members or drop earlier selected groups")
    assert(ctx.ground:selected_count() == 0, "successful dispatch consumes the explicit selection")
    ctx.ground:search("log", { surface = "platform" })
    ctx.ground:toggle("log:")
    log2.surface = "land"
    assert(not ctx.ground:pickup_selected(), "moving off the selected surface invalidates the target")
    log2.surface = "platform"
    ctx.params.ground_ignore_full, log2.count = true, 40
    assert(not ctx.ground:pickup_selected() and ctx.ground:selected_count() == 1,
        "ignore-full preserves selection when no selected item qualifies")
    log2.count = 3
    assert(ctx.ground:pickup_selected() and #picked == 1 and picked[1].target == log2)
    ctx.params.ground_ignore_full = false
    found[#found] = nil
    ctx.ground:search("log", { surface = "land" })
    assert(ctx.ground:location("log:").point.x == log.x)
    log.hidden = true
    assert(ctx.ground:location("log:") == nil, "hidden targets cannot retain world markers")
    log.hidden = false

    T.case("current platform filtering captures the selected boat", function()
        local previous = found
        local boat, other_boat = {}, {}
        local aboard = entity(91, "log", "Log", 1, nil, 2, "platform")
        local other = entity(92, "log", "Log", 2, nil, 3, "platform")
        local ashore = entity(93, "log", "Log", 3)
        local floating = entity(94, "log", "Log", 4, nil, 1, "ocean")
        aboard.platform, other.platform = boat, other_boat
        player.surface, player.platform, found = "platform", boat, { aboard, other, ashore, floating }
        local groups = ctx.ground:search("", { surface = "current" })
        assert(#groups == 1 and groups[1].count == 2 and groups[1].nearest == aboard)
        assert(ctx.ground:pickup("log:", true) and #picked == 1 and picked[1].target == aboard,
            "another boat, shore and floating items are excluded")
        assert(ctx.ground:toggle("log:"))
        player.platform, aboard.platform = other_boat, other_boat
        assert(not ctx.ground:location("log:") and not ctx.ground:pickup_selected(),
            "a captured selection cannot move to a different boat even if player and item move together")
        ctx.ground:search("", { surface = "current" })
        assert(not ctx.ground:pickup_selected(), "a refresh cannot replace the selection's original boat")
        ctx.ground:clear()
        player.surface, player.platform = nil, nil
        groups = ctx.ground:search("", { surface = "current" })
        assert(#groups == 1 and groups[1].count == 1 and groups[1].nearest == ashore,
            "on land the current-surface filter excludes boats and floating items")
        found = previous
        ctx.ground:search("log", { surface = "land" })
    end)

    T.case("remote_skin_builds", function()
        local previous = found
        local first = entity(101, "backpack", "Backpack", 1, "backpack_butterfly")
        local second = entity(102, "backpack", "Backpack", 2, "backpack_catcoon")
        local plain = entity(103, "backpack", "Backpack", 3)
        first.skinname, second.skinname = nil, nil
        assert(first:GetSkinName() == nil and second:GetSkinName() == nil,
            "native client entities have animation skins without Lua skin names")
        found = { first, second, plain }
        local groups = ctx.ground:search("")
        assert(#groups == 3, "remote skins and the default build remain distinct")
        local key = "backpack:backpack_butterfly"
        assert(ctx.ground:pickup(key, true) and #picked == 1 and picked[1].target == first)
        active = entity(104, "backpack", "Backpack", 0, "backpack_butterfly")
        active.skinname = nil
        groups = ctx.ground:search("", { held = true })
        assert(#groups == 1 and groups[1].key == key and groups[1].count == 1,
            "held filters compare client-visible skins")
        active = nil
        assert(ctx.ground:toggle(key))
        first.skin = "backpack_catcoon"
        assert(not ctx.ground:pickup_selected() and not ctx.ground:location(key),
            "reskinning invalidates captured members and their markers")
        ctx.ground:clear()
        found = previous
        ctx.ground:search("log", { surface = "land" })
    end)

    -- Exercise the widget with the game's real TextEdit input and cancellation methods.
    Class = T.Class
    local Base = {}
    Base.__index = Base
    function Base._ctor(self, name)
        self.name, self.children, self.flow, self.shown, self.enabled = name, {}, {}, true, true
        self.inst = { entity = { AddTextEditWidget = function() end }, TextWidget = {
            ShowEditCursor = function() end, SetEditCursorColour = function() end,
        } }
        self.inst.TextEditWidget = {
            SetString = function(_, value) self.value = value end,
            OnTextInput = function(_, value) self.value = (self.value or "") .. value end,
            OnKeyDown = function() end, OnKeyUp = function() end,
        }
    end
    function Base:AddChild(child) self.children[#self.children + 1] = child; child.parent = self; return child end
    function Base:SetString(value) self.value = value end
    function Base:GetString() return self.value or "" end
    function Base:SetText(value) self.value = value end
    function Base:SetTruncatedString(value) self.value = value end
    function Base:SetMultilineTruncatedString(value) self.value = value end
    function Base:SetTexture(atlas, image) self.texture = { atlas, image } end
    function Base:SetOnClick(fn) self.onclick = fn end
    function Base:SetOnGainFocus(fn) self.ongainfocusfn = fn end
    function Base:SetOnLoseFocus(fn) self.onlosefocusfn = fn end
    function Base:SetFocusChangeDir(dir, target) self.flow[dir] = target end
    function Base:Show() self.shown = true end
    function Base:Hide() self.shown = false end
    function Base:IsEnabled() return self.enabled end
    function Base:Enable() self.enabled = true end
    function Base:Disable() self.enabled = false end
    function Base:StartUpdating() self.updating = true end
    function Base:StopUpdating() self.updating = false end
    function Base:OnGainFocus() end
    function Base:OnLoseFocus() end
    function Base:GetRegionSize() return 500, 34 end
    for _, name in ipairs({ "SetSize", "SetTint", "SetFont", "SetTextSize", "SetClickable", "SetRegionSize", "SetColour", "SetHAlign" }) do
        Base[name] = function() end
    end
    local NativeWidget = T.native("widgets/widget", { Class = T.Class, ValidateLineNumber = function() end,
        FunctionOrValue = function(value, ...)
            if type(value) == "function" then return value(...) end
            return value
        end })
    for _, method in ipairs({ "SetFocus", "SetFocusFromChild", "ClearFocus", "IsVisible", "OnControl", "OnRawKey" }) do
        Base[method] = NativeWidget[method]
    end
    function Base:SetPosition(x, y) self.position = { x = x, y = y } end
    function Base:GetWorldPosition() return self.position or { x = 0, y = 0 } end
    function Base:SetScale(value) self.scale = value end
    function Base:GetWorldScale() return { x = self.scale or 1, y = self.scale or 1 } end
    function Base:SetHAnchor() end
    function Base:SetVAnchor() end
    function Base:SetScaleMode() end
    function Base:Kill()
        self.killed = true
        for _, child in ipairs(self.children) do child:Kill() end
    end
    local Widget = Class(Base, Base._ctor)
    local Text = Class(Base, function(self, font, size, value)
        Base._ctor(self, "text"); self.font, self.size, self.value = font, size, value or ""
    end)
    package.loaded["widgets/widget"], package.loaded["widgets/text"] = Widget, Text
    package.loaded["widgets/image"] = Widget
    package.loaded["widgets/textbutton"] = function()
        local button = Widget("button"); button.text, button.image = Text(), Widget(); return button
    end
    local env = setmetatable({ IsSteamDeck = function() return false end,
        require = function(name) return package.loaded[name] or {} end }, { __index = _G })
    package.loaded["widgets/textedit"] = T.native("widgets/textedit", env)
    STRINGS = { UI = { HELP = {} } }
    TheInput = { EnableDebugToggle = function() end, IsPasteKey = function() return false end }
    TheInputProxy = { FlushInput = function() end }
    CONTROL_ACCEPT, CONTROL_CANCEL, CONTROL_MOVE_UP, KEY_ENTER = 1, 2, 3, 13
    MOVE_UP, MOVE_DOWN, MOVE_LEFT, MOVE_RIGHT = 1, 2, 3, 4
    function string:utf8len() local _, length = self:gsub("[^\128-\191]", ""); return length end
    package.loaded["fieldkit/ui/indicators"] = { new = function()
        return { locate = function(self, record) self.record = record end,
            close = function(self) self.closed, self.record = true, nil end }
    end }
    local GroundList = require("fieldkit/ui/ground_list")
    local executed
    local screen = Widget("screen"); screen.is_screen = true
    local list = screen:AddChild(GroundList(ctx, function(fn) executed = true; return fn() end))
    local exit = screen:AddChild(Widget("toolbox"))
    list:SetExitFocus(exit)
    assert(list.search.flow[MOVE_UP] == exit and list.updating)
    list:FocusSearch()
    assert(list:IsEditing() and list.search:OnTextInput("木"))
    assert(list.search:GetString() == "木" and list.page_count == 1)
    assert(list.search:OnRawKey(289, true), "native text input consumes unrelated hotkeys")
    assert(list.search:OnControl(CONTROL_MOVE_UP, true), "editing consumes gameplay movement")
    assert(list.search:OnControl(CONTROL_CANCEL, false) and not list:IsEditing())
    list:FocusSearch()
    assert(list.search:OnControl(CONTROL_ACCEPT, false) and list.selected == "log:")
    assert(list.indicators.record and list.indicators.record.point.x == log.x)
    exit:SetFocus()
    assert(not list.indicators.record, "leaving the item clears its world marker immediately")
    list.tiles[1]:SetFocus()
    local selected = list.selected
    log.x, skinned.x = 15, .5
    list:OnUpdate(.5)
    assert(list.selected == selected and list.one.enabled, "distance changes do not replace the selected group")
    list.all.onclick()
    assert(executed and #picked == 3 and picked[1].target == log2, "pick group uses its precise visible members")
    log.removed, log2.removed, ocean.removed = true, true, true
    list:OnUpdate(.5)
    assert(list.selected == nil and not list.one.enabled and not list.announce.enabled,
        "disappearing selection cannot silently select its former grid neighbour")
    list.search:SetString("")
    list:OnUpdate(0)
    local first_tile = list.tiles[1]
    first_tile.onclick()
    assert(list.selected and not list:IsEditing())
    ctx.config.work = false
    local independent = screen:AddChild(GroundList(ctx, function(fn) return fn() end))
    independent.tiles[1].onclick()
    assert(independent.announce.enabled, "announcement works when whole-group automation is disabled")
    found = {}
    for index = 1, 12 do found[index] = entity(100 + index, "item" .. index, string.format("Item %02d", index), index) end
    list.search:SetString("Item")
    list:OnUpdate(0)
    assert(list.page_count == 2)
    list:ChangePage(1)
    assert(list.page == 2 and list.selected == "item10:")
    table.insert(found, entity(200, "before", "Item 00", 2))
    list:OnUpdate(.5)
    assert(list.selected == "item10:" and list.tiles[2].focus, "insertion preserves selection and keyboard focus by key")
    assert(list.tiles[1].flow[MOVE_DOWN] == list.tiles[4] and list.tiles[2].flow[MOVE_DOWN] == list.one
        and list.search.flow[MOVE_UP] == exit)
    list:FocusSearch()
    list.held_button.onclick()
    assert(not list:IsEditing() and list.selected == nil and not list.one.enabled)
    list:FocusSearch()
    list:Kill()
    assert(list.killed and not list.updating and not list:IsEditing(), "closing releases text editing and updates")
    assert(list.indicators.closed and not list.indicators.record, "closing releases the locator")

    -- The persistent dock releases native input ownership without closing after a command.
    do
        ctx.hud = Widget("hud")
        ctx.hud.is_screen = true
        ctx.hud.root = ctx.hud:AddChild(Widget("root"))
        function ctx.hud:SetModFocus(_, _, value) self.modfocus = value end
        function ctx.hud:OnBecomeInactive() end
        player.HUD = ctx.hud
        local active_screen, controller, cancelled = ctx.hud, false
        ctx.G.ThePlayer = player
        ctx.G.TheFrontEnd = { GetActiveScreen = function() return active_screen end, GetHUDScale = function() return 1 end }
        ctx.G.TheInput = { ControllerAttached = function() return controller end,
            GetScreenPosition = function() return { x = 0, y = 0 } end }
        ctx.G.CONTROL_CANCEL, ctx.G.KEY_ESCAPE = CONTROL_CANCEL, 27
        ctx.executor = { cancel = function(self, reason) cancelled, self.current = reason, nil end }
        T.tasks(player)
        ctx.scope = require("fieldkit/core/scope").new(player, error)
        function ctx:report(err) error(err) end
        function ctx:notify(message) self.message = message end
        ctx.config.work = true
        log.removed, log2.removed, ocean.removed = nil, nil, nil
        found = { log, flint }
        local dock = GroundList.dock(ctx)
        assert(dock.open and dock.root.shown)
        assert(dock.radius_button.value == "Nearby items · 20m ›")
        dock.radius_button.onclick()
        assert(ctx.params.search_radius == 30 and dock.radius_button.value == "Nearby items · 30m ›"
            and dock.radius_button.flow[MOVE_DOWN] == dock.list.exit_focus,
            "the title edits the task radius through the same controller focus chain")
        assert(dock:focus() and dock.focused and ctx.hud.modfocus and dock.list:IsEditing())
        assert(ctx.hud.focus and ctx.hud.root.focus and dock.root.focus and dock.list.focus and dock.list.search.focus,
            "native child focus propagation reaches the owning HUD")
        local job = {}
        assert(dock:execute(function() ctx.executor.current = job; return true end))
        assert(dock.open and not dock.focused and not ctx.hud.modfocus and not dock.list:IsEditing(),
            "executing hands control back while the browsing dock remains visible")
        dock.list.tiles[1]:SetFocus()
        assert(dock.list.indicators.record)
        local unrelated = ctx.hud.root:AddChild(Widget("native_inventory"))
        unrelated:SetFocus()
        assert(not dock.root.focus and not dock.list.indicators.record and not ctx.hud.modfocus,
            "native sibling focus clears the whole dock branch and locator")
        dock.list.tiles[1]:SetFocus()
        ctx.hud:Hide()
        assert(not dock.list.indicators.record, "native HUD hide synchronously removes world markers")
        ctx.hud:Show()
        controller = true
        assert(dock:focus() and not dock.list:IsEditing())
        assert(dock:handle_control(CONTROL_CANCEL, false) and not dock.focused and dock.open,
            "controller cancel releases navigation, retaining the list")
        dock.list.tiles[1]:SetFocus()
        active_screen = {}
        dock:refresh()
        assert(not dock.root.shown and not dock.list.indicators.record and not ctx.hud.modfocus,
            "another screen hides the dock and releases its focus")
        active_screen = ctx.hud
        dock:refresh()
        assert(dock.root.shown)
        ctx.executor.current, dock.job = job, job
        dock:set_open(false)
        assert(cancelled == "cancelled" and not ctx.executor.current, "closing cancels this dock's pickup job")
        dock:close()
        assert(dock.root.killed and dock.list.indicators.closed)
        ctx.scope:close()
    end
end)
T.done()
