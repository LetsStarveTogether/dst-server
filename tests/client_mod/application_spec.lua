local T = require("tests.client_mod.support")
T.case("application ownership", function()

    -- Rendering and feature internals are covered separately; exercise the real application scope/executor.
    local widget = require("tests.client_mod.support.widgets")
    widget.install()
    for _, name in ipairs({ "work", "travel", "cooking", "drying", "fishing", "repair", "wormholes", "timers", "announcements", "emotes", "ground", "presentation", "visual", "placement", "construction", "selection", "storage", "awareness" }) do
        package.loaded["fieldkit/features/" .. name] = { new = function()
            return { status = function() return "" end, clear = function() end, context = function() end,
                handle_control = function() return false end }
        end }
    end
    for _, name in ipairs({ "map_input", "map_view", "inventory_input", "pointer_input", "information", "journal" }) do
        package.loaded["fieldkit/game/" .. name] = {
            install = function() end, attach = function() end, new = function() return {} end,
            editing = function() return false end,
        }
    end
    package.loaded["fieldkit/game/equipment"] = { new = function()
        return { busy = function() return false end }
    end }
    package.loaded["fieldkit/ui/announcement_controls"] = { install = function() end, attach = function() end }
    package.loaded["fieldkit/ui/minimap"] = { new = function()
        return { handle_control = function() return false end, consumes_pointer = function() return false end }
    end }
    package.loaded["fieldkit/ui/hud"] = { new = function(ctx)
        local root = ctx.hud:AddChild(widget("fieldkit_status"))
        root:AddChild(widget("open"))
        root:AddChild(widget("stop")):SetOnClick(function() ctx.executor:cancel("cancelled"); ctx.selection:clear() end)
        ctx.scope:defer(function() root:Kill() end)
        return root
    end }
    local function screen(ctx, intent, hold_key)
        local screen = { intent = intent, hold_key = hold_key }
        function screen:Close(commit)
            self.closed, self.commit = true, commit
            ctx.screen = nil
            ctx.G.TheFrontEnd.active = ctx.hud
        end
        return screen
    end
    package.loaded["fieldkit/ui/panel"] = function(ctx, intent) return screen(ctx, intent) end
    package.loaded["fieldkit/ui/emote_wheel"] = function(ctx, hold_key) return screen(ctx, nil, hold_key) end

    local function fixture()
        local state = { now = 0, cancelled = {}, native_calls = 0, reports = {} }
        local player, handlers = T.entity(), T.handlers()
        function player:HasTag(tag) return tag == "playerghost" and state.ghost end
        state.tasks, state.handlers = T.tasks(player), handlers.list
        function state:emit(source, ...) source:PushEvent(...) end
        local function handler(...) return handlers:add(...) end
        function state:input(...) handlers:dispatch(...) end
        local hud = widget("HUD")
        player.HUD = hud
        local G = { ThePlayer = player, TheWorld = T.events({ Map = { GetPlatformAtPoint = function() return state.platform end } }),
            GetTime = function() return state.now end,
            IsPaused = function() return state.paused end,
            print = function(message) state.reports[#state.reports + 1] = message end,
            Vector3 = T.Vector3,
            TheNet = { IsServerPaused = function() return state.serverpaused end },
            TheInput = {
                AddKeyUpHandler = function(_, key, fn) return handler("key", key, fn) end,
                AddKeyDownHandler = function(_, key, fn) return handler("keydown", key, fn) end,
                AddMouseButtonHandler = function(_, fn) return handler("mouse", 0, fn) end,
                GetHUDEntityUnderMouse = function() return state.over_hud and {} end,
                GetWorldEntityUnderMouse = function() return state.target end,
                GetWorldPosition = function() return state.point end,
                ControllerAttached = function() return state.controller_attached end,
            },
        }
        G.DynamicPosition = T.eval(assert(T.source("util"):match("(DynamicPosition = Class.-)\n%-+\n%-%- Class LinkedList"))
            .. "\nreturn DynamicPosition", { Class = T.Class, Vector3 = G.Vector3, TheWorld = G.TheWorld })
        for index, name in ipairs({ "CONTROL_MOVE_UP", "CONTROL_MOVE_DOWN", "CONTROL_MOVE_LEFT", "CONTROL_MOVE_RIGHT",
            "CONTROL_PRIMARY", "CONTROL_SECONDARY", "CONTROL_ACTION", "CONTROL_ATTACK", "CONTROL_CONTROLLER_ACTION",
            "CONTROL_CONTROLLER_ALTACTION", "CONTROL_CONTROLLER_ATTACK", "CONTROL_INVENTORY_USEONSELF",
            "CONTROL_INVENTORY_USEONSCENE", "CONTROL_INVENTORY_DROP" }) do G[name] = index end
        G.TheFrontEnd = { active = hud, GetActiveScreen = function(self) return self.active end,
            PushScreen = function(self, screen) self.active:OnBecomeInactive(); self.active = screen end }
        function hud:HasInputFocus() return G.TheFrontEnd.active ~= self end
        function hud:OnBecomeInactive()
            assert(not state.ctx.executor:is_running(), "focus loss must cancel before native screen handling")
            state.inactive = true
            state.preview = nil
        end
        local controller = { IsEnabled = function() return not state.disabled end }
        function controller:OnControl(control, down)
            if state.must_cancel then
                assert(not state.ctx.executor:is_running(), "stop old action before native user action")
            end
            state.native_calls = state.native_calls + 1
            return "native", nil, control, down
        end
        function controller:GetActionButtonAction() return { target = state.controller_target } end
        for _, method in ipairs({ "DoControllerUseItemOnSelfFromInvTile", "DoControllerUseItemOnSceneFromInvTile",
            "DoControllerDropItemFromInvTile", "DoControllerInspectItemFromInvTile" }) do
            controller[method] = function(_, item)
                assert(not state.ctx.executor:is_running(), "controller item use must take over regardless of key mapping")
                return item, nil, "inventory action"
            end
        end
        player.components.playercontroller = controller
        player.replica.health = { IsDead = function() return state.dead end }
        player.replica.inventory = { GetActiveItem = function() return state.cursor_item end,
            classified = { IsBusy = function() return false end } }
        state.original_control, state.original_inactive = controller.OnControl, hud.OnBecomeInactive
        state.config = { language = "en", text_scale = 1, panel_key = 287, cancel_key = 288,
            gesture_key = 289, cooking_key = 291, gestures = true }
        state.ctx = assert(require("fieldkit/application").attach(G, player, state.config, {}))
        state.ctx.placement.context = function() return state.preview end
        state.G, state.player, state.controller, state.hud = G, player, controller, hud
        function state:start()
            return self.ctx.executor:start({ name = "walk", timeout = 10, step = function() return "running" end,
                cancel = function(_, reason) self.cancelled[#self.cancelled + 1] = reason end })
        end
        return state
    end

    local state = fixture()
    assert(state:start())
    state.hud:OnBecomeInactive()
    assert(state.inactive and state.cancelled[#state.cancelled] == "focus")
    T.each("pause events", { "pause", "autopause", "gameautopause" }, function(flag)
        assert(state:start())
        state:emit(state.G.TheWorld, "serverpauseddirty", { [flag] = true })
        assert(not state.ctx.executor:is_running(), "pause cancellation must not wait for a frozen sim tick")
    end)
    state.serverpaused = true
    assert(not state:start())
    state.serverpaused = false

    assert(state:start())
    state.must_cancel = true
    local a, b, c, d = state.controller:OnControl(state.G.CONTROL_SECONDARY, true)
    assert(a == "native" and b == nil and c == state.G.CONTROL_SECONDARY and d == true)
    assert(state.cancelled[#state.cancelled] == "manual")
    T.each("native inventory takeover", { "DoControllerUseItemOnSelfFromInvTile", "DoControllerUseItemOnSceneFromInvTile",
        "DoControllerDropItemFromInvTile", "DoControllerInspectItemFromInvTile" }, function(method)
        assert(state:start())
        local item = {}
        local result, middle, tail = state.controller[method](state.controller, item)
        assert(result == item and middle == nil and tail == "inventory action")
        assert(state.cancelled[#state.cancelled] == "manual")
    end)
    assert(state:start())
    state.over_hud = false
    state:input("mouse", 0, 1, true)
    assert(state.ctx.executor:is_running(), "raw world mouse event must not discard the ticket before native routing")
    state.controller:OnControl(state.G.CONTROL_SECONDARY, true)
    assert(not state.ctx.executor:is_running())
    state.must_cancel = false
    assert(state:start())
    state.controller:OnControl(state.G.CONTROL_MOVE_UP, false)
    assert(state.ctx.executor:is_running(), "key release does not replace an action")
    state.over_hud = true
    state:input("mouse", 0, 1, true)
    assert(state.cancelled[#state.cancelled] == "focus")
    state.hud.children[1].children[2].onclick()
    assert(not state.ctx.executor:is_running(), "Stop click must retain the stopping cancellation from mouse down")

    assert(state:start())
    state.cursor_item = {}
    state:emit(state.player, "newactiveitem", { item = state.cursor_item })
    assert(not state.ctx.executor:is_running() and not state:start())
    state.cursor_item = nil
    T.each("native placer ownership", { "placer", "deployplacer" }, function(field)
        state.controller[field] = {}
        assert(not state:start(), "automated actions cannot compete with a native placer")
        state.controller[field] = nil
    end)
    state.target, state.point = {}, { x = 7, y = 3, z = 9 }
    local preview = { rotation = 37 }
    state.preview = preview
    state:input("key", 287)
    local screen = assert(state.ctx.screen)
    state.point.x = 99
    assert(screen.intent.target == state.target and screen.intent.position:GetPosition().x == 7
        and screen.intent.position:GetPosition().y == 0)
    assert(screen.intent.placement == preview and state.preview == nil,
        "capture placement before native focus loss destroys its preview")
    state:input("key", 288)
    assert(screen.closed and not state.ctx.screen)
    state.controller_attached = true
    state.controller_target = { GetPosition = function() return { x = 4, y = 0, z = 5 } end }
    state:input("key", 287)
    assert(state.ctx.screen.intent.position:GetPosition().x == 4, "controller intent uses its selected target, not stale mouse coordinates")
    state:input("key", 288)

    local boat = T.entity({ x = 100, z = 50, rotation = 90, entity = {} })
    function boat.entity:WorldToLocalSpace(x, _, z)
        local c, s = math.cos(boat.rotation * math.pi / 180), math.sin(boat.rotation * math.pi / 180)
        return (x - boat.x) * c + (z - boat.z) * s, 0, -(x - boat.x) * s + (z - boat.z) * c
    end
    function boat.entity:LocalToWorldSpace(x, _, z)
        local c, s = math.cos(boat.rotation * math.pi / 180), math.sin(boat.rotation * math.pi / 180)
        return boat.x + x * c - z * s, 0, boat.z + x * s + z * c
    end
    state.platform, state.point, state.controller_attached = boat, T.Vector3(101, 0, 52), false
    state:input("key", 287)
    boat.x, boat.rotation = 110, 180
    local captured = state.ctx.screen.intent.position
    assert(math.abs(captured:GetPosition().x - 108) < 1e-8 and math.abs(captured:GetPosition().z - 51) < 1e-8,
        "native captured menu points follow platform translation and rotation")
    boat:Remove()
    assert(captured:GetPosition() == nil, "losing a platform invalidates its menu point")
    state:input("key", 288)
    state.platform = nil

    -- Hold-open belongs to key-down; native Screen owns the matching release.
    state.controller_attached = false
    state:input("key", 289)
    assert(not state.ctx.screen, "a release must not reopen the wheel")
    assert(state:start())
    state:input("keydown", 289)
    local wheel = assert(state.ctx.screen)
    assert(wheel.hold_key == 289 and not wheel.intent and not state.ctx.executor:is_running())
    state:input("keydown", 289)
    assert(state.ctx.screen == wheel and not wheel.closed, "key repeat does not toggle or replace a held wheel")
    state:input("key", 288)
    assert(wheel.closed and not wheel.commit)
    assert(state.ctx:open_emotes() and state.ctx.screen.hold_key == nil, "menu entry opens a non-held wheel")
    state:input("key", 288)
    state.G.TheFrontEnd.textProcessorWidget = {}
    state:input("keydown", 289)
    state:input("key", 287)
    assert(not state.ctx.screen, "tool keys do not interrupt an active text editor")
    state.G.TheFrontEnd.textProcessorWidget = nil

    local cooking_calls = 0
    state.ctx.cooking.shortcut = function()
        cooking_calls = cooking_calls + 1
        return false, "No open or selected pot"
    end
    state:input("key", 291)
    assert(cooking_calls == 1 and state.ctx.message == "No open or selected pot")
    state.G.TheFrontEnd.textProcessorWidget = {}
    state:input("key", 291)
    assert(cooking_calls == 1, "cooking shortcut must not interrupt a text editor")
    state.G.TheFrontEnd.textProcessorWidget = nil

    assert(state:start())
    state:emit(state.player, "playerdeactivated")
    assert(state.ctx.scope.closed and not state.ctx.executor:is_running() and not state.ctx:ready())
    assert(state.controller.OnControl == state.original_control and state.hud.OnBecomeInactive == state.original_inactive)
    assert(state.hud.children[1].killed)
    for _, value in ipairs(state.handlers) do assert(value.removed) end
    for _, task in ipairs(state.tasks) do assert(task.cancelled); task.fn() end
    assert(#state.reports == 0)

    T.each("player exit", { "onremove", "death" }, function(event)
        state = fixture()
        assert(state:start())
        state:emit(state.player, event)
        assert(not state.ctx.executor:is_running())
        if event == "onremove" then assert(state.ctx.scope.closed) end
        state.ctx.scope:close()
    end)
    state = fixture()
    assert(state.ctx:open_emotes(289))
    wheel = state.ctx.screen
    state.ctx.scope:close()
    assert(wheel.closed and wheel.commit == false and not state.ctx.screen, "scope disposal cancels an open wheel")
    state = fixture()
    state.player.HUD = {}
    for _, task in ipairs(state.tasks) do if not task.cancelled then task.fn() end end
    assert(state.ctx.scope.closed and #state.reports == 0, "HUD replacement releases the old scope")

    -- Native pause-menu integration stays reachable after rebuilding and never duplicates its row.
    state = fixture()
    local App = require("fieldkit/application")
    local attach = App.attach
    local pause_hook
    App.attach = function() return state.ctx end
    package.loaded["fieldkit/features/placement"].install = function() end
    package.loaded["fieldkit/features/visual"].install = function() end
    package.loaded["fieldkit/features/cooking"].install = function() end
    package.loaded.json = { decode = function() return {} end, encode = function() return "{}" end }
    state.G.TheSim = { GetPersistentString = function(_, _, fn) fn(false, "") end }
    state.player.DoTaskInTime = function(_, _, fn) fn() end
    App.install({
        GLOBAL = state.G,
        modinfo = { configuration_options = {} },
        GetModConfigData = function() end,
        AddClassPostConstruct = function(path, fn)
            assert(path == "screens/redux/pausescreen"); pause_hook = fn
        end,
        AddPlayerPostInit = function(fn) fn(state.player) end,
    })
    App.attach = attach
    local function menu()
        local result = { items = {}, offset = -50 }
        function result:AddItem(label, fn)
            local item = widget(label)
            item.onclick, item.SetScale = fn, function() end
            self.items[#self.items + 1] = item
            return item
        end
        for i = 1, 5 do result:AddItem(tostring(i), function() end) end
        return result
    end
    local pause = { owner = state.player, menu = menu() }
    function pause:BuildMenu() self.menu = menu(); return true, nil, "rebuilt" end
    function pause:unpause() state.paused = false; state.G.TheFrontEnd.active = state.hud end
    pause_hook(pause)
    assert(#pause.menu.items == 6 and pause.menu.items[6].name == "Fieldkit")
    local ok, middle, tail = pause:BuildMenu()
    assert(ok and middle == nil and tail == "rebuilt" and #pause.menu.items == 6)
    assert(pause.menu.offset == -40)
    state.paused, state.serverpaused = true, true
    state.G.TheFrontEnd.active = pause
    pause.menu.items[6].onclick()
    assert(state.ctx.screen and not state.paused, "pause menu can open the toolkit while server autopause releases")
    assert(not state:start(), "server pause still blocks actions")
    state.ctx.scope:close()

    -- The cooking key reports successful recipe memory and failed admission once.
    do
        local state = fixture()
        local messages = {}
        function state.ctx:notify(message) messages[#messages + 1] = message end
        for _, success in ipairs({ true, false }) do
            local reason = success and "Recipe remembered" or "Cooking unavailable"
            state.ctx.cooking.shortcut = function() return success, reason end
            state:input("key", state.config.cooking_key)
            assert(#messages == (success and 1 or 2) and messages[#messages] == reason)
        end
        state.ctx.scope:close()
    end
end)
T.done()
