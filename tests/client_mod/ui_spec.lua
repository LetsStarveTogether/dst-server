local T = require("tests.client_mod.support")
T.case("panel and HUD", function()

    -- Run the actual panel with geometry/focus stand-ins; rendered fonts remain a GUI check.
    local Base = require("tests.client_mod.support.widgets")
    Base.install()
    local node = Base
    package.loaded["widgets/screen"] = Base
    ANCHOR_MIDDLE, SCALEMODE_PROPORTIONAL = 1, 1
    MOVE_UP, MOVE_DOWN, MOVE_LEFT, MOVE_RIGHT = 1, 2, 3, 4
    CONTROL_CANCEL, KEY_ESCAPE = 10, 27
    local popped, ran = false, nil
    TheFrontEnd = { PopScreen = function(_, screen) popped = true; screen:OnDestroy() end }
    local ctx = { config = { text_scale = 1.3, gestures = true }, scope = {}, player = {},
        params = require("fieldkit/core/parameters").new() }
    function ctx:text(_, en) return en end
    function ctx:report(err) error(err) end
    function ctx:notify(message) self.message = message end
    function ctx:open_emotes(hold_key)
        assert(popped and self.screen == nil and hold_key == nil, "close the panel before opening the independent wheel")
        ran = "emotes"
        return true
    end
    local Panel = require("fieldkit/ui/panel")
    local panel = Panel(ctx, {})
    ctx.screen = panel
    assert(panel.page == 1 and #panel.buttons == 3)
    assert(panel.buttons[1].text.width == 650)
    assert(panel.buttons[1].flow[MOVE_DOWN] == panel.buttons[2])
    assert(panel:OnControl(CONTROL_CANCEL, true) and not popped)
    panel.buttons[1].onclick()
    assert(ran == "emotes" and popped)
    assert(panel.closed and panel.killed and ctx.screen == nil)

    -- Native screen removal does not call the panel's Close method.
    panel = Panel(ctx, {})
    ctx.screen = panel
    TheFrontEnd:PopScreen(panel)
    assert(panel.closed and panel.killed and ctx.screen == nil, "native removal releases the panel owner")
    local older = Panel(ctx, {})
    ctx.screen = Panel(ctx, {})
    TheFrontEnd:PopScreen(older)
    assert(ctx.screen ~= nil and not ctx.screen.closed, "destroying an older panel preserves the new screen")

    popped, ran = false, nil
    panel = Panel(ctx, {})
    ctx.screen = panel
    ctx.scope.closed = true
    panel.buttons[1].onclick()
    assert(popped and not ran, "a destroyed player cannot execute a stale entry")
    ctx.scope.closed = false
    panel = Panel(ctx, {})
    ctx.screen = panel
    assert(panel.buttons[1].text.width == 650)
    assert(panel:OnControl(CONTROL_CANCEL, false) and ctx.screen == nil)

    -- Each travel entry retains the explicit target; the nearby lists stay separate.
    local chosen, creature = {}, { GetDisplayName = function() return "Beefalo" end }
    ctx.config.travel, ctx.params.search_radius = true, 20
    ctx.travel = {
        can_follow = function(_, target) return target ~= nil end,
        can_push = function(_, target) return target == chosen end,
        follow = function(_, target) assert(popped); ran = target; return true end,
        push = function(_, target) assert(popped); ran = target; return true end,
    }
    panel = Panel(ctx, { target = chosen })
    ctx.screen = panel
    panel.section = "travel"
    local rows = panel:Rows()
    local follow, push, creatures
    for _, row in ipairs(rows) do
        if row.label == "Follow selected target · Recorded trail" then follow = row end
        if row.label == "Push selected target (up to 30 seconds)" then push = row end
        if row.label == "Follow → choose nearby creature" then creatures = row end
    end
    assert(follow and push and creatures)
    popped, ran = false, nil
    push.fn()
    assert(popped and ran == chosen)
    require("fieldkit/game/query").nearby = function(_, radius, tags, excluded, any)
        assert(radius == 20 and tags == nil and excluded[3] == "player" and any[1] == "locomotor")
        return { creature }
    end
    panel = Panel(ctx, {})
    ctx.screen = panel
    panel.section = "creatures"
    rows = panel:Rows()
    assert(#rows == 1 and rows[1].label == "Beefalo")
    popped, ran = false, nil
    rows[1].fn()
    assert(popped and ran == creature)

    -- Categories expose complete workflows without turning the root into a long action list.
    for _, name in ipairs({ "work", "cooking", "ground", "grid", "announcements" }) do ctx.config[name] = true end
    ctx.params.craft_count, ctx.params.cooking_count = 5, 10
    local area, point = {}, { x = 5, z = 9 }
    local function invoke_row(section, label, expected)
        T.case(label, function()
            panel = Panel(ctx, { target = chosen, position = { GetPosition = function() return point end } })
            ctx.screen, panel.section, popped, ran = panel, section or panel.section, false, nil
            local selected
            for _, row in ipairs(panel:Rows()) do if row.label == label then selected = row; break end end
            assert(selected, "missing panel action")
            selected.fn()
            assert(popped and ran == expected, "action must close the panel and preserve its intent")
        end)
    end
    ctx.selection = { area = function() return area end }
    ctx.construction = {
        area = function(_, selected, kind) assert(popped and selected == area); ran = kind; return true end,
        repeat_recipe = function(_, count) assert(popped and count == 5); ran = "craft"; return true end,
    }
    ctx.cooking = { batch = function(_, target, count)
        assert(popped and target == chosen and count == 10); ran = "cooking"; return true
    end, remember_inventory = function(_, source, slot)
        assert(popped and slot == 1); ran = source; return true
    end, quick_repeat = function(_, target)
        assert(popped and target == chosen); ran = "quick_cooking"; return true
    end }
    ctx.placement = { set_origin = function(_, selected)
        assert(popped and selected == point); ran = "origin"; return true
    end }
    ctx.travel.add_waypoint = function(_, selected)
        assert(popped and selected == point); ran = "waypoint"; return true
    end
    ctx.fishing = { start = function(_, selected)
        assert(popped and selected == point); ran = "fishing"; return true
    end }
    panel = Panel(ctx, { target = chosen, point = point })
    assert(#panel:Rows() == 11)
    for _, case in ipairs({
        { "construction", "Deploy held item across area", "deploy" },
        { "construction", "Terraform selected area", "terraform" },
        { "construction", "Drop one held item per grid point", "drop" },
        { "construction", "Water selected farm tiles", "water" },
        { "construction", "Fertilize selected farm tiles", "fertilize" },
        { "construction", "Repeat last recipe ×5", "craft" },
        { "cooking", "Cook nearby pots ×10", "cooking" },
        { "cooking", "Remember recipe from first inventory slots", "inventory" },
        { "cooking", "Remember recipe from first backpack slots", "backpack" },
        { "cooking", "Quick repeat selected or open pot", "quick_cooking" },
        { "placement", "Set origin at selected point", "origin" },
        { "travel", "Add selected point to route", "waypoint" },
        { "fishing", "Fish repeatedly at selected water", "fishing" },
    }) do
        invoke_row(case[1], case[2], case[3])
    end

    -- Editing a task parameter preserves the open page, focus and captured target.
    panel = Panel(ctx, { target = chosen })
    ctx.screen, panel.section, panel.page = panel, "construction", 2
    panel:Rebuild()
    popped = false
    for _, button in ipairs(panel.buttons) do
        if button.value:match("Crafting repetitions:") then button.onclick(); break end
    end
    assert(not popped and not panel.closed and ctx.screen == panel and panel.page == 2)
    assert(ctx.params.craft_count == 10 and panel.intent.target == chosen)
    assert(panel.default_focus.value == "Crafting repetitions: 10 ›" and panel.default_focus.focus)
    ctx.params.craft_count = 5

    -- A configured minimap can be unavailable in this world or game mode.
    for _, no_minimap in ipairs({ false, true }) do
        local unavailable = { config = { text_scale = 1, minimap = true, information_hud = true },
            text = ctx.text, scope = {}, G = {
                TheWorld = no_minimap and { minimap = { MiniMap = {} } } or {},
                GetGameModeProperty = function() return no_minimap end,
            } }
        unavailable.minimap = require("fieldkit/ui/minimap").new(unavailable,
            require("fieldkit/game/map_view").new(unavailable))
        assert(unavailable.minimap == nil)
        local screen = Panel(unavailable, {})
        assert(#screen:Rows() == 0, "unavailable minimap and passive HUD cannot create an empty category")
        screen.section = "information"
        assert(#screen:Rows() == 0, "unavailable minimap cannot expose callable actions")
        unavailable.config.range_hints = true
        screen.section = "tools"
        local entries = screen:Rows()
        assert(#entries == 1 and entries[1].label == "Information and display")
        entries[1].fn()
        assert(screen.section == "information" and screen:Rows()[1].label == "Pin / unpin selected target's range",
            "other available display controls remain accessible without a minimap")
    end

    -- Display controls are useful with public announcements disabled.
    ctx.config.announcements, ctx.config.minimap, ctx.config.range_hints = false, true, true
    ctx.awareness = {
        toggle_pin = function(_, target) assert(popped and target == chosen); ran = "pin"; return true end,
        toggle_group = function(_, target) assert(popped and target == chosen); ran = "group"; return true end,
        clear_pins = function() assert(popped); ran = "clear" end,
        status = function() return "Pins 1" end,
    }
    ctx.minimap = {
        layout = function(self, top) self.top = top end,
        set_open = function(_, open) assert(popped); ran = open and "expand" or "collapse"; return true end,
        recenter = function() assert(popped); ran = "center"; return true end,
        focus = function() assert(popped); ran = "focus"; return false, "Map is unavailable" end,
    }
    panel = Panel(ctx, { target = chosen })
    ctx.screen = panel
    panel.section = "information"
    rows = panel:Rows()
    assert(not rows[1].label:match("Announce"))
    for _, case in ipairs({ { "Pin / unpin selected target's range", "pin" },
        { "Pin / unpin nearby matching ranges", "group" }, { "Clear pinned ranges", "clear" },
        { "Expand minimap", "expand" }, { "Collapse minimap", "collapse" },
        { "Recenter minimap", "center" }, { "Control minimap", "focus" } }) do
        invoke_row("information", case[1], case[2])
    end
    assert(ctx.message == "Map is unavailable", "display actions preserve native service failures")
    -- Radius entry closes the menu first, retains its target, and owns the dialog lifetime.
    do
        local shown
        package.loaded["screens/redux/inputdialog"] = function(_, buttons, modal)
            assert(modal and ctx.screen == nil)
            local dialog = node("radius")
            dialog.buttons, dialog.edit_text = buttons, { SetTextLengthLimit = function() end }
            function dialog:SetValidChars(chars) self.chars = chars end
            function dialog:OverrideText(value) self.value = value end
            function dialog:GetActualString() return self.value end
            function dialog:Close() TheFrontEnd:PopScreen(self) end
            return dialog
        end
        TheFrontEnd.PushScreen = function(_, screen) shown = screen end
        local calls = 0
        ctx.awareness.custom_group = function(_, radius, target)
            assert(radius == 12.5 and target == chosen and ctx.screen == nil)
            calls = calls + 1
            return true
        end
        panel = Panel(ctx, { target = chosen })
        ctx.screen = panel
        panel:CustomRange(true)
        assert(panel.closed and shown == ctx.screen and shown.value == "4" and shown.chars == "0123456789.")
        panel.intent.target, shown.value = creature, "12.5"
        shown.buttons[1].cb()
        assert(calls == 1 and shown.killed and ctx.screen == nil)
        shown.buttons[1].cb()
        assert(calls == 1, "stale dialog cannot repeat the command")
        panel = Panel(ctx, { target = chosen })
        ctx.screen = panel
        panel:CustomRange(true)
        ctx.scope.closed = true
        shown.value = "12.5"
        shown.buttons[1].cb()
        assert(calls == 1 and shown.killed and ctx.screen == nil, "closed player scope cannot pin a range")
        ctx.scope.closed = false
    end
    ctx.config.announcements = true
    panel = Panel(ctx, {})
    panel.section = "information"
    panel:Rebuild()
    assert(panel.title.value:match("1/3"), "display and announcements paginate together")
    for _, button in ipairs(panel.buttons) do if button.value == "Next" then button.onclick(); break end end
    assert(panel.page == 2 and panel.title.value:match("2/3"))
    assert(panel.buttons[1].value == "Announce season and world time")
    for _, button in ipairs(panel.buttons) do if button.value == "Previous" then button.onclick(); break end end
    assert(panel.page == 1 and panel.default_focus == panel.buttons[1])

    ctx.announcements = {
        metric = function(_, key) assert(popped); ran = key; return true end,
        nightmare = function() assert(popped); ran = "nightmare"; return true end,
        ride = function() assert(popped); ran = "ride"; return true end,
    }
    for _, case in ipairs({ { "Announce body temperature", "temperature" }, { "Announce moisture", "moisture" },
        { "Announce nightmare phase", "nightmare" }, { "Announce riding status", "ride" } }) do
        invoke_row("information", case[1], case[2])
    end

    -- Rack scope and ingredient choice form one intent, submitted after the menu closes.
    ctx.drying = {
        ingredients = function() return { { prefab = "meat", item = { GetDisplayName = function() return "Meat" end }, count = 7 } } end,
        start = function(_, targets, prefab) assert(popped and targets[1] == chosen); ran = { "target", prefab }; return true end,
        area = function(_, selected, prefab) assert(popped and selected == area); ran = { "area", prefab }; return true end,
        nearby = function(_, prefab) assert(popped); ran = { "nearby", prefab }; return true end,
    }
    for i, scope in ipairs({ "target", "area", "nearby" }) do
        for _, harvest_only in ipairs({ true, false }) do
            panel = Panel(ctx, { target = chosen })
            ctx.screen, panel.section, popped, ran = panel, "drying", false, nil
            panel:Rows()[i].fn()
            assert(not popped and panel.section == "drying_food" and panel.drying_scope == scope)
            local choices = panel:Rows()
            assert(choices[2].label == "Harvest and refill: Meat ×7")
            choices[harvest_only and 1 or 2].fn()
            assert(popped and ran[1] == scope and ran[2] == (not harvest_only and "meat" or nil))
        end
    end

    -- Only applicable, synchronized character metrics appear; click rechecks through the service.
    ctx.information = { snapshot = function() return { own = {
        health = { quality = "known", label = { "生命", "Health" } },
        boat_health = { quality = "known", label = { "船体耐久", "Boat health" } },
        energy = { quality = "unknown", label = { "电量", "Energy" } },
    } } end }
    panel = Panel(ctx, {})
    ctx.screen, panel.section, popped = panel, "metrics", false
    rows = panel:Rows()
    assert(#rows == 1 and rows[1].label == "Boat health")
    rows[1].fn()
    assert(popped and ran == "boat_health")

    -- The toolkit hands focus to the persistent list after its own screen closes.
    ctx.ground_dock = {
        focus = function() assert(popped and ctx.screen == nil); ran = "ground_focus"; return true end,
        toggle = function() assert(popped and ctx.screen == nil); ran = "ground_toggle"; return true end,
    }
    for _, case in ipairs({ { "Control nearby item list", "ground_focus" }, { "Show / hide nearby item list", "ground_toggle" } }) do
        invoke_row(nil, case[1], case[2])
    end

    local Hud = require("fieldkit/ui/hud")
    local snapshot = {
        own = { temperature = { value = 12, quality = "known" }, moisture = { quality = "unknown" } },
        world = { day = 25, season = "autumn", season_day = 3, remaining_days = nil, endless = true,
            moonphase = "half", waxing = false, temperature = 22 },
        nightmare = { phase = "warn", progress = .25, remaining = 35, quality = "estimated", visible = true },
        ride = { observed = 12, remaining_min = 5, remaining_max = 15, quality = "estimated" },
    }
    local now, cancelled, cleared = 1, nil, false
    ctx.hud, ctx.player = node("gamehud"), {}
    ctx.hud.root = ctx.hud:AddChild(node("hudroot"))
    ctx.player.HUD = ctx.hud
    ctx.G = { ThePlayer = ctx.player, GetTime = function() return now end,
        GetInventoryItemAtlas = function() return "images/inventoryimages.xml" end,
        TheFrontEnd = { GetActiveScreen = function() return ctx.active_screen end } }
    ctx.active_screen = ctx.hud
    ctx.scope = require("fieldkit/core/scope").new(ctx.player, error)
    function ctx.scope:periodic(_, fn) self.refresh = fn end
    ctx.executor = { current = { name = "Crafting" }, cancel = function(_, reason) cancelled = reason end }
    ctx.information = { snapshot = function() return snapshot end }
    ctx.selection = { status = function() return "Selected 5" end, clear = function() cleared = true end }
    ctx.placement.status = function() return "Grid square" end
    ctx.config.information_hud = true
    ctx.message, ctx.message_until = "First line\nSecond line", 5
    function ctx:open() ran = "toolbox" end
    local clock_env = setmetatable({ require = function() return Base end,
        Vector3 = function(x, y, z) return { x = x, y = y, z = z } end }, { __index = _G })
    local NativeClock = T.native("widgets/uiclock", clock_env)
    local clock = setmetatable(node("clock"), { __index = NativeClock })
    local animation = { PlayAnimation = function(self, name) self.animation = name end,
        PushAnimation = function() end, OverrideSymbol = function(self, _, _, name) self.symbol = name end }
    clock._moonanim, clock._anim, clock._text = node("moon"), node("transition"), node("daytext")
    clock._moonanim.GetAnimState, clock._anim.GetAnimState = function() return animation end, function() return animation end
    clock._phase, clock._moonphase, clock._mooniswaxing, clock._moonphasebuild = "night", "half", false, "moon_phases"
    ctx.hud.controls = { clock = clock, status = node("native_status") }
    ctx.hud.controls.status:SetScale(1.4)
    local hud = Hud.new(ctx)
    local lines = Hud.lines(ctx)
    local Status = require("fieldkit/ui/status")
    assert(ctx.hud.controls.status:GetScale().x == 1.4 and hud.status:GetScale().x == ctx.config.text_scale,
        "Fieldkit applies its one text scale without multiplying the native HUD scale")
    assert(clock._moonanim:GetScale().x == 1 and clock._text.shown)
    clock:OnGainFocus()
    assert(clock._text.shown)
    clock:OnLoseFocus()
    assert(clock._text.shown)
    clock:OnPhaseChanged("day")
    assert(clock._moonanim.shown and animation.symbol == "moon_half", "native moon remains visible through daylight")
    clock:OnPhaseChanged("dusk")
    assert(clock._moonanim.shown)
    clock:OnMoonPhaseChanged2({ moonphase = "half", waxing = true })
    assert(animation.symbol == "moon_half_wax", "daytime moon preserves the native waxing orientation")
    clock:OnPhaseChanged("night")
    assert(clock._moonanim.shown)
    clock:OnPhaseChanged("day")
    local status_rows = Status.rows(ctx, snapshot)
    assert(#lines == 4 and status_rows[1].text:match("Endless") and not status_rows[1].text:match("10000"))
    assert(status_rows[2].key == "temperature" and status_rows[2].text:match("Body 12"))
    assert(status_rows[3].key == "world_temperature" and status_rows[3].text:match("Air 22"))
    assert(status_rows[4].text:match("Half moon ↓") and status_rows[4].key == "moon")
    snapshot.world.fullmoon = { min_day = 29, max_day = 31 }
    snapshot.naughtiness = { actions = 4, threshold = 30 }
    status_rows = Status.rows(ctx, snapshot)
    assert(status_rows[4].text:match("Full moon day ≈29–31") and status_rows[5].text == "Naughtiness 4/30")
    snapshot.own.temperature.value, snapshot.world.temperature = nil, nil
    status_rows = Status.rows(ctx, snapshot)
    assert(#status_rows == 3 and status_rows[2].key == "moon" and status_rows[3].key == "naughtiness",
        "unavailable temperature data does not leave meaningless rows")
    snapshot.own.temperature.value, snapshot.world.temperature, snapshot.naughtiness = 12, 22, nil
    assert(lines[1]:match("Warning 25%% ≈35s") and lines[2]:match("12s / ≈5–15s"))
    assert(lines[3]:match("Crafting") and lines[3]:match("Selected 5") and lines[3]:match("Pins 1"))
    assert(hud.shown and #hud.labels == 4 and hud.labels[3].position.y < -150)
    assert(hud.labels[3].width == 600 and not hud.labels[3].value:match("\n"), "each HUD row stays within its width")
    assert(ctx.minimap.top > 50 - hud.labels[3].position.y, "minimap begins below the information and activity area")
    local native_env = setmetatable({ require = function() return Base end }, { __index = _G })
    local NativeHud = T.native("screens/playerhud", native_env)
    local function rendered(widget)
        return widget.shown ~= false and (widget.parent == nil or rendered(widget.parent))
    end
    ctx.hud.controls = { votedialog = { CloseControllerVoteScreen = function() end } }
    NativeHud.Hide(ctx.hud)
    assert(not rendered(hud), "native HUD hiding immediately hides Fieldkit through the native root")
    ctx.scope.refresh()
    assert(not rendered(hud), "periodic refresh cannot reveal Fieldkit while its HUD root is hidden")
    NativeHud.Show(ctx.hud)
    assert(rendered(hud), "native HUD show restores Fieldkit without a refresh delay")
    hud.open.onclick()
    assert(ran == "toolbox")
    hud.stop.onclick()
    assert(cancelled == "cancelled" and cleared)
    now = 6
    snapshot.nightmare.quality, snapshot.ride.quality = "unknown", "unknown"
    lines = Hud.lines(ctx)
    assert(lines[1]:match("Warning 25%% %?s") and lines[2]:match("12s / %?s"))
    assert(not lines[3]:match("First line"), "messages expire instead of accumulating on the HUD")
    ctx.config.information_hud = false
    snapshot.nightmare.visible, snapshot.ride = false, nil
    lines = Hud.lines(ctx)
    assert(lines[1] == "" and lines[2] == "" and lines[3]:match("Crafting"))
    ctx.active_screen = {}
    ctx.scope.refresh()
    assert(not hud.shown)
    ctx.player.HUD = nil
    ctx.scope.refresh()
    assert(ctx.scope.closed and hud.killed, "replaced HUD disposes its widget and owned refresh")
    assert(clock:GetScale().x == 1 and clock._moonanim:GetScale().x == 1 and clock._text.shown,
        "native clock scale and moon orientation remain owned by the game")
    assert(animation.animation == "hidden", "teardown restores native daytime moon visibility")
    local disabled = { config = { information_hud = false, text_scale = 1 }, G = ctx.G,
        hud = { controls = { clock = {} } }, scope = { wrap = function() error("disabled information changed native clock") end } }
    local passive = Status.new(disabled, node("passive_hud"))
    passive:Refresh(snapshot)
    assert(passive.height == 0 and not passive.clock.shown)

    -- Panel actions own feedback, including a real ground command forwarding work failure.
    do
        local messages = {}
        local feedback = { config = { text_scale = 1 }, scope = {},
            params = require("fieldkit/core/parameters").new(),
            G = { CanEntitySeeTarget = function() return true end }, player = { GetDistanceSqToInst = function() return 1 end } }
        function feedback:text(_, en) return en end
        function feedback:notify(message) messages[#messages + 1] = message end
        function feedback:report(err) error(err) end
        local item = { prefab = "flint", Transform = {}, IsValid = function() return true end,
            replica = { inventoryitem = { CanBePickedUp = function() return true end, IsHeld = function() return false end } },
            HasTag = function() return false end, GetSkinName = function() return "" end,
            entity = { IsVisible = function() return true end } }
        feedback.work = { pickup = function() return false, "Queue admission rejected" end }
        feedback.ground = require("fieldkit/features/ground").new(feedback)
        feedback.ground.groups["flint:"] = { entities = { item } }
        local screen = Panel(feedback, {})
        feedback.screen = screen
        screen:Execute(function() return feedback.ground:pickup("flint:") end)
        assert(#messages == 1 and messages[1] == "Queue admission rejected")
        screen = Panel(feedback, {})
        feedback.screen = screen
        screen:Execute(function() return true, "Waypoint added: 1" end)
        assert(#messages == 2 and messages[2] == "Waypoint added: 1")
    end
end)
T.done()
