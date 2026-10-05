local T = require("tests.client_mod.support")
T.case("native map input", function()
    local Scope = require("fieldkit/core/scope")
    local MapInput = require("fieldkit/game/map_input")
    local Input = require("fieldkit/game/input")
    local Executor = require("fieldkit/core/executor")
    package.loaded["fieldkit/ui/map_overlay"] = { new = function(ctx)
        local overlay = { root = { SetHAnchor = function() end, SetVAnchor = function() end } }
        function overlay:update() end
        function overlay:close() self.closed = true end
        ctx.overlay = overlay
        return overlay
    end }
    local function fixture(native_frontend)
        local state = { keys = {}, appended = {}, navigated = {}, popped = 0, native = 0 }
        local player = T.entity({ replica = { inventory = { GetActiveItem = function() end } } })
        state.tasks = T.tasks(player)
        local world = T.events({ minimap = { MiniMap = { IsVisible = function() return false end } } })
        local hud = { controls = {} }
        player.HUD = hud
        function hud:IsMapScreenOpen() return state.active == state.screen and state.screen ~= nil end
        function hud:HasInputFocus() return state.active ~= hud end
        function hud:OnBecomeInactive() end
        player.components.playercontroller = {
            OnMapAction = function() state.native = state.native + 1 end,
            IsEnabled = function() return state.active == hud end,
            classified = { iscontrollerenabled = { value = function() return state.enabled ~= false end } },
        }
        local G = {
            KEY_SHIFT = 304, ThePlayer = player,
            GetGameModeProperty = function() return false end,
            TheNet = { GetServerGameMode = function() return "survival" end,
                IsServerPaused = function(_, explicit) return state.manual_pause or not explicit and state.paused end },
            TheInput = {
                ControllerAttached = function() return state.gamepad end,
                IsKeyDown = function(_, key) return state.keys[key] end,
                GetControllerID = function() return 1 end,
                GetLocalizedControl = function(_, _, key) return tostring(key) end,
                AddMouseButtonHandler = function(_, fn) state.mouse = fn; return { Remove = function() state.mouse = nil end } end,
                GetHUDEntityUnderMouse = function() return true end,
            },
            TheSim = { GetPosition = function() return 750, 125 end, GetScreenSize = function() return 1000, 500 end },
            SetAutopaused = function(value)
                state.paused = value
                world:PushEvent("serverpauseddirty", { autopause = value })
            end,
            Vector3 = T.Vector3,
            TheFrontEnd = { PopScreen = function()
                state.popped = state.popped + 1
                state.screen:OnDestroy()
                state.active = hud
            end, PushScreen = function(_, screen) state.active = screen end, tracking_mouse = true, GetActiveScreen = function() return state.active end },
            TheWorld = world,
            STRINGS = { UI = { HELP = { ROTATE_LEFT = "left", ROTATE_RIGHT = "right", ZOOM_IN = "in", ZOOM_OUT = "out", BACK = "back" } } },
        }
        for index, name in ipairs({ "CONTROL_SECONDARY", "CONTROL_MENU_MISC_2", "CONTROL_MENU_MISC_3",
            "CONTROL_CONTROLLER_ATTACK", "CONTROL_ACCEPT", "CONTROL_MAP", "CONTROL_CANCEL", "CONTROL_ROTATE_LEFT",
            "CONTROL_ROTATE_RIGHT", "CONTROL_MAP_ZOOM_IN", "CONTROL_MAP_ZOOM_OUT", "CONTROL_CONTROLLER_ACTION" }) do G[name] = index end
        local ctx = { G = G, player = player, hud = hud, config = { map_modifier = 400, travel = true } }
        function ctx:text(_, en) return en end
        state.messages = {}
        function ctx:notify(message) state.messages[#state.messages + 1] = message end
        ctx.scope = Scope.new(player, error)
        ctx.executor = Executor.new(function() return 0 end, function() return true end, function() end, error)
        local cancel = ctx.executor.cancel
        function ctx.executor:cancel(reason)
            state.cancelled = reason; state.running, state.owner = false, nil
            return cancel(self, reason)
        end
        ctx.selection = { handle_control = function() return false end }
        ctx.travel = {
            pause_route = function(_, owner)
                if not state.running then return false end
                state.owner = owner
                return true
            end,
            resume_route = function(_, owner) if state.owner == owner then state.owner = nil end end,
            is_editing = function(_, job, owner)
                return state.running and state.owner ~= nil and (job == nil or job == ctx.executor.current)
                    and (owner == nil or owner == state.owner)
            end,
            add_waypoint = function(_, point) state.appended[#state.appended + 1] = point; return true end,
            navigate = function(_, point, append)
                assert(not append and state.active == hud, "navigation starts only after synchronous HUD restoration")
                state.navigated[#state.navigated + 1] = point
                return true
            end,
            route_status = function() return "Planned waypoints: " .. #state.appended end,
        }
        -- Execute the checked-out native class, retaining its input priority and cursor projection methods.
        local base = { OnControl = function() state.native_controls = (state.native_controls or 0) + 1; return false end,
            OnBecomeInactive = function() end, OnDestroy = function() state.destroyed = true end }
        local env = setmetatable({ require = function() return base end, Class = T.Class },
            { __index = function(_, key) return G[key] ~= nil and G[key] or _G[key] end })
        local NativeMap = T.native("screens/mapscreen", env)
        local callback
        MapInput.install({ GLOBAL = G, AddClassPostConstruct = function(name, fn)
            assert(name == "screens/mapscreen"); callback = fn
        end }, function() return state.gone and nil or ctx end)
        function state:new_screen()
            local screen = setmetatable({ owner = player, shown = true, decorationdata = {}, hover = { Hide = function() end }, minimap = {
                centerreticle = { Hide = function() end },
                MapPosToWorldPos = function(_, x, y) return x * 100 + 20, y * 80 + 10 end,
            } }, NativeMap)
            function screen:AutoAimToStaticDecorations(x, y, z) state.aimed = { x = x, z = z }; return x + 2, y, z end
            function screen:UpdateMapActions(x, _, z) state.native_query = { x = x, z = z }; return nil, state.right end
            function screen:RemoveDecorations() end
            callback(screen)
            self.screen, self.active = screen, screen
            return screen
        end
        function hud.controls:ShowMap()
            assert(MapInput.editing(ctx, ctx.executor.current), "opening ownership precedes native constructor autopause")
            G.SetAutopaused(true)
            assert(not state.cancelled, "native map autopause cannot cancel the owned route")
            local screen = state:new_screen()
            hud:OnBecomeInactive()
            assert(not state.cancelled, "HUD losing focus to its map cannot cancel the owned route")
            return screen, nil, "native tail"
        end
        hud.controls.ToggleMap = hud.controls.ShowMap
        if native_frontend then
            local source = T.source("frontend")
            local push = assert(source:match("(function FrontEnd:PushScreen%(screen%).-)\nfunction FrontEnd:ClearScreens%("))
            local frontend = G.TheFrontEnd
            env.FrontEnd, env.TheInputProxy = {}, { FlushInput = function() end }
            env.Print, env.VERBOSITY = function() end, { DEBUG = 0 }
            frontend.PushScreen = T.eval(push .. "\nreturn FrontEnd.PushScreen", env)
            frontend.screenroot = { AddChild = function() end }
            frontend.consoletext = { MoveToFront = function() end }
            frontend.serverpausewidget = { MoveToFront = function() end, SetOffset = function() end }
            function frontend:SetForceProcessTextInput() end
            function frontend:Update() state.active = self.screenstack[#self.screenstack] end
            local textedit = T.source("widgets/textedit")
            local control = assert(textedit:match("(function TextEdit:OnControl%(control, down%).-)\nfunction TextEdit:OnFocusMove%("))
            env.TextEdit = { _base = { OnControl = function() return false end } }
            state.text_control = T.eval(control .. "\nreturn TextEdit.OnControl", env)
        end
        MapInput.attach(ctx)
        Input.attach(ctx)
        return ctx, state:new_screen(), state, G
    end

    -- Only completing the map gesture reports the synchronous command result.
    T.each("command feedback", { true, false }, function(append)
        local ctx, screen, state, G = fixture()
        state.keys[400], state.keys[G.KEY_SHIFT] = true, append
        local message = append and "Waypoint added: 1" or "Destination unavailable"
        ctx.travel[append and "add_waypoint" or "navigate"] = function() return append, message end
        assert(screen:OnControl(G.CONTROL_SECONDARY, true) and #state.messages == 0)
        assert(screen:OnControl(G.CONTROL_SECONDARY, false))
        assert(#state.messages == 1 and state.messages[1] == message)
        ctx.scope:close()
    end)

    do
        local ctx, screen, state, G = fixture()
        screen:OnControl(G.CONTROL_SECONDARY, true)
        assert(#state.tasks == 0 and #state.appended == 0, "unmodified input retains native semantics")
        state.keys[400], state.keys[G.KEY_SHIFT] = true, true
        assert(screen:OnControl(G.CONTROL_SECONDARY, true))
        assert(#state.appended == 0, "press captures intent without dispatch")
        assert(screen:OnControl(G.CONTROL_SECONDARY, false), "paired release is consumed")
        assert(#state.appended == 1 and state.popped == 0)
        assert(state.appended[1].x == 70 and state.appended[1].z == -30, "native normalized cursor and map projection")
        assert(state.native_query.x == 72, "native static-icon aiming is used when checking existing map actions")
        assert(screen:GetHelpText():match("Planned waypoints: 1"))
        ctx.scope:close()
    end

    do
        local ctx, screen, state, G = fixture()
        state.keys[400] = true
        assert(screen:OnControl(G.CONTROL_SECONDARY, true))
        assert(screen:OnControl(G.CONTROL_SECONDARY, false))
        assert(state.popped == 1 and #state.navigated == 1 and state.paused == false)
        assert(state.destroyed and rawget(screen, "OnControl") == nil, "native map destruction releases screen hooks")
        assert(ctx.overlay.closed, "native screen destruction releases its route overlay")
        assert(ctx.player.components.playercontroller._hack_ignore_ups_for[G.CONTROL_SECONDARY])
        assert(#state.tasks == 0, "no delayed navigation survives cancellation")
        assert(#state.navigated == 1 and state.navigated[1].x == 70)
        ctx.scope:close()
    end

    do
        local ctx, screen, state, G = fixture()
        state.keys[400] = true
        state.right = { action = { code = 8 } }
        screen:OnControl(G.CONTROL_SECONDARY, true)
        assert(state.native == 1 and #state.tasks == 0 and #state.appended == 0, "native map action has priority")
        ctx.scope:close()
    end

    do
        local ctx, screen, state, G = fixture()
        state.gamepad = true
        screen:OnControl(G.CONTROL_MENU_MISC_2, true)
        assert(screen:OnControl(G.CONTROL_MENU_MISC_2, false))
        assert(state.appended[1].x == 20 and state.appended[1].z == 10, "controller uses native map centre")
        screen:OnControl(G.CONTROL_MENU_MISC_3, true)
        screen:OnControl(G.CONTROL_MENU_MISC_3, false)
        assert(state.popped == 1)
        ctx.scope:close()
        assert(#state.tasks == 0 and #state.navigated == 1)
        assert(not MapInput.request(ctx, { x = 1, z = 1 }, false), "closed player cannot start a new route")
    end

    do
        local ctx, screen, state, G = fixture()
        state.gamepad, ctx.config.map_modifier = true, 0
        screen:OnControl(G.CONTROL_MENU_MISC_3, true)
        assert(#state.tasks == 0 and state.popped == 0, "zero binding disables interception")
        ctx.scope:close()
    end

    do
        local ctx, screen, state = fixture()
        screen:OnDestroy()
        local cleanups = #ctx.scope.cleanups
        for _ = 1, 100 do
            local opened = state:new_screen()
            opened:OnDestroy()
            assert(rawget(opened, "OnControl") == nil and rawget(opened, "GetHelpText") == nil)
        end
        assert(#ctx.scope.cleanups == cleanups, "reopening maps cannot retain old screens in player scope")
        ctx.scope:close()
    end

    -- The native map constructor autopause precedes HUD deactivation; keep only this exact edit surface.
    T.each("native map opening", { "ShowMap", "ToggleMap" }, function(method)
        local ctx, first, state, G = fixture()
        first:OnDestroy()
        state.active = ctx.hud
        state.running, state.cancelled = true, nil
        local job = { input = { mode = "world" } }
        ctx.executor.current = job
        local screen, middle, last = ctx.hud.controls[method](ctx.hud.controls)
        assert(screen == state.screen and middle == nil and last == "native tail")
        assert(MapInput.editing(ctx, job) and Input.ready(ctx, nil, job))
        state.mouse(1, true)
        assert(not state.cancelled, "raw map interactions belong to the editing screen")
        state.keys[400], state.keys[G.KEY_SHIFT] = true, true
        screen:OnControl(G.CONTROL_SECONDARY, true)
        screen:OnControl(G.CONTROL_SECONDARY, false)
        assert(#state.appended == 1 and state.owner ~= nil)
        state.enabled = false
        assert(not Input.ready(ctx, nil, job), "map focus never overrides native gameplay disabling")
        state.enabled = true
        G.TheFrontEnd.textProcessorWidget = {}
        assert(not MapInput.editing(ctx, job) and not Input.ready(ctx, nil, job), "chat / console takes ownership")
        G.TheFrontEnd.textProcessorWidget = nil
        state.active = {}
        assert(not Input.ready(ctx, nil, job), "an overlay above the map is not a route editor")
        state.active = screen
        state.manual_pause = true
        assert(not Input.ready(ctx, nil, job), "explicit pause still cancels")
        state.manual_pause = false
        G.TheFrontEnd:PopScreen(screen)
        assert(state.owner == nil and Input.ready(ctx, nil, job), "closing the map returns the same job to its HUD")
        ctx.scope:close()
    end)

    -- Overlay takeover must be synchronous even when autopause prevents executor ticks.
    T.each("overlay takeover", { "ConsoleScreen", "PauseScreen", "ChatInputScreen" }, function(overlay)
        local ctx, first, state, G = fixture()
        first:OnDestroy()
        state.active, state.running, state.cancelled = ctx.hud, true, nil
        ctx.executor.current = { input = { mode = "world" } }
        local map = ctx.hud.controls:ShowMap()
        assert(state.paused and state.owner ~= nil)
        G.TheFrontEnd:PushScreen({ name = overlay })
        assert(state.cancelled == "focus" and state.owner == nil,
            "foreign screen cancels before a paused simulation can resume")
        state.active = map
        map:OnDestroy()
        assert(not state.running, "closing a transient overlay cannot revive the previous route")
        ctx.scope:close()
    end)

    T.each("map gesture loses focus", { "map release", "console release" }, function(release)
        T.each("append", { false, true }, function(append)
            local ctx, screen, state, G = fixture(true)
            local frontend, control = G.TheFrontEnd, G.CONTROL_SECONDARY
            frontend.screenstack = { screen }
            state.keys[400], state.keys[G.KEY_SHIFT] = true, append
            assert(screen:OnControl(control, true))
            -- With automatic pause disabled, only screen ownership can invalidate the press.
            local generation = ctx.executor.generation
            frontend:PushScreen({ name = "ConsoleScreen", OnBecomeActive = function() end })
            if release == "console release" then
                local edit = { editing = true, ignore_controls = {}, pass_controls_to_screen = {},
                    IsEnabled = function() return true end }
                assert(state.text_control(edit, control, false), "native console editing consumes the release")
            end
            frontend.screenstack[#frontend.screenstack] = nil
            state.active = screen
            assert(ctx.executor.generation == generation, "this case has no running route to cancel")
            state.keys[400], state.keys[G.KEY_SHIFT] = nil, nil
            if release == "map release" then
                local calls = state.native_controls
                assert(screen:OnControl(control, false), "the original release remains owned after focus loss")
                assert(state.native_controls == calls, "the cancelled release cannot reach native map input")
            else
                screen:OnControl(control, true)
                assert(not screen:OnControl(control, false), "a new ordinary click keeps both native edges")
            end
            assert(#state.navigated == 0 and #state.appended == 0 and state.popped == 0,
                "returning to the map cannot execute a press captured before focus loss")
            state.right = { action = { code = 8 } }
            screen:OnControl(control, true)
            assert(state.native == 1, "a fresh native map action still takes priority")
            screen.quitting, state.right = nil, nil
            state.keys[400], state.keys[G.KEY_SHIFT] = true, append
            assert(screen:OnControl(control, true) and screen:OnControl(control, false))
            assert(#state.navigated + #state.appended == 1, "a fresh modified click remains usable")
            ctx.scope:close()
        end)
    end)

    -- Engine control/raw callback order cannot make the same navigation gesture cancel its new job.
    T.each("map pointer orders", T.pointer_orders, function(order)
        local ctx, screen, state, G = fixture()
        local next_job = { input = { mode = "world" } }
        state.keys[400] = true
        ctx.travel.navigate = function()
            assert(state.active == ctx.hud)
            ctx.executor.current = next_job
            return true
        end
        local function raw(down) state.mouse(2, down) end
        local function mapped(down) screen:OnControl(G.CONTROL_SECONDARY, down) end
        T.pointer(raw, mapped, true, order.down)
        assert(ctx.executor.current == nil and state.popped == 0)
        T.pointer(raw, mapped, false, order.up)
        assert(ctx.executor.current == next_job and state.cancelled == nil,
            "the raw broadcast over inventory cannot cancel navigation completed on release")
        ctx.scope:close()
    end)

    T.each("stop held gesture", { false, true }, function(running)
        T.each("append", { false, true }, function(append)
            local ctx, screen, state, G = fixture()
            state.keys[400], state.keys[G.KEY_SHIFT], state.running = true, append, running
            if running then ctx.executor.current = { input = { mode = "world" } } end
            assert(screen:OnControl(G.CONTROL_SECONDARY, true))
            ctx.executor:cancel("cancelled")
            assert(screen:OnControl(G.CONTROL_SECONDARY, false))
            assert(#state.navigated == 0 and #state.appended == 0 and state.popped == 0,
                "stop consumes the held release without navigating or appending, even with no current job")
            assert(screen:OnControl(G.CONTROL_SECONDARY, true))
            assert(screen:OnControl(G.CONTROL_SECONDARY, false))
            assert(#state.navigated + #state.appended == 1, "a fresh map gesture remains usable after stop")
            ctx.scope:close()
        end)
    end)
end)
T.done()
