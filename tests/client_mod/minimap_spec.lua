local T = require("tests.client_mod.support")
T.case("native map lifecycle", function()
    local Scope = require("fieldkit/core/scope")
    local Executor = require("fieldkit/core/executor")
    local Store = require("fieldkit/core/store")
    local MapView = require("fieldkit/game/map_view")
    local Widget = require("tests.client_mod.support.widgets")
    Class = T.Class
    local Base = Class(Widget, function(self, name)
        Widget._ctor(self, name)
        self.inst = { ListenForEvent = function() end, ImageWidget = {
            SetBlendMode = function() end,
            SetTextureHandle = function(_, handle) self.texture = handle end,
        } }
    end)
    local node = Base
    package.loaded["widgets/widget"] = Base
    for _, name in ipairs({ "image", "text", "textbutton" }) do package.loaded["widgets/" .. name] = node end
    local Mini = require("fieldkit/ui/minimap")

    local function native_function(path, owner, name, globals)
        local source = T.source(path)
        local declaration = owner and owner .. ":" .. name or name
        local body = assert(source:match((owner and "function " or "local function ") .. declaration .. "%b().-\nend"))
        local env = setmetatable(owner and { [owner] = {} } or {}, { __index = globals })
        return T.eval(body .. "\nreturn " .. (owner and owner .. "." or "") .. name, env, path .. ":" .. declaration)
    end

    local function fixture(store)
        local state = { zoom = 3, visible = false, offset_x = 0, offset_y = 0, stack = {}, pointer = { x = -1000, y = -1000 },
            screen_width = 1600, screen_height = 900, keys = {}, pressed = {}, axis = {}, navigated = {}, appended = {},
            map_enabled = true, controller_enabled = true }
        local renderer = {
            IsVisible = function() return state.visible end,
            ToggleVisibility = function() state.visible = not state.visible end,
            GetZoom = function() return state.zoom end,
            Zoom = function(_, delta) state.zoom = state.zoom + delta end,
            ResetOffset = function() state.offset_x, state.offset_y = 0, 0 end,
            Offset = function(_, x, y) state.offset_x, state.offset_y = state.offset_x + x, state.offset_y + y end,
            GetTextureHandle = function() return "native_texture" end,
            MapPosToWorldPos = function(_, x, y) state.projection = { x, y }; return x * 100, y * 100 end,
            WorldPosToMapPos = function(_, x, z) return x / 100, z / 100 end,
        }
        local player = { components = {}, Transform = { GetWorldPosition = function() return 0, 0, 0 end } }
        function player:ListenForEvent() end
        function player:RemoveEventCallback() end
        local hud = node("PlayerHud")
        player.HUD = hud
        function hud:SetModFocus(_, _, focused) state.modfocus = focused end
        function hud:IsStatusScreenOpen() return false end
        function hud:IsMapScreenOpen() return state.stack[#state.stack].name == "MapScreen" end
        local G = {
            ThePlayer = player, TheWorld = { minimap = { MiniMap = renderer } },
            BLENDMODE = { Premultiplied = 1, Additive = 2 }, TUNING = { CONTROLLER_DEADZONE_RADIUS = .2 },
            Vector3 = function(x, y, z) return { x = x, y = y, z = z } end,
            TheSim = { GetScreenSize = function() return state.screen_width, state.screen_height end,
                GetPosition = function() return state.pointer.x, state.pointer.y end },
            TheInput = {
                ControllerAttached = function() return state.gamepad end,
                GetScreenPosition = function() return state.pointer end,
                IsKeyDown = function(_, key) return state.keys[key] end,
                IsControlPressed = function(_, key) return state.pressed[key] end,
                GetAnalogControlValue = function(_, key) return state.axis[key] or 0 end,
                GetControllerID = function() return 0 end,
                GetLocalizedControl = function(_, _, id) return tostring(id) end,
                AddMouseButtonHandler = function() return { Remove = function() end } end,
            },
            GetGameModeProperty = function() return state.no_map end,
            SetAutopaused = function() end,
            TheCamera = { GetHeadingTarget = function() return 0 end },
            TheNet = { GetServerGameMode = function() return "survival" end },
            Profile = { IsMinimapZoomCursorFollowing = function() return false end, GetMiniMapZoomSensitivity = function() return 15 end },
            EntityScript = { is_instance = function() return true end },
            IsConsole = function() return true end, Class = Class, PI = math.pi,
        }
        for i, name in ipairs({ "CONTROL_PRIMARY", "CONTROL_SECONDARY", "CONTROL_MAP_ZOOM_IN", "CONTROL_MAP_ZOOM_OUT",
            "CONTROL_CANCEL", "CONTROL_ACCEPT", "CONTROL_CONTROLLER_ACTION", "CONTROL_MOVE_RIGHT", "CONTROL_MOVE_LEFT",
            "CONTROL_MOVE_UP", "CONTROL_MOVE_DOWN", "CONTROL_MAP", "CONTROL_MENU_MISC_1", "CONTROL_MENU_MISC_2", "CONTROL_MENU_MISC_3",
            "MOUSEBUTTON_LEFT", "MOUSEBUTTON_RIGHT", "KEY_SHIFT" }) do G[name] = i end
        G.CONTROL_ACCEPT, G.CONTROL_CANCEL, G.CONTROL_CONTROLLER_ACTION, G.CONTROL_CONTROLLER_ALTACTION = 29, 30, 57, 58
        G.TheFrontEnd = {
            GetActiveScreen = function() return state.stack[#state.stack] end,
            GetOpenScreenOfType = function(_, name)
                for i = #state.stack, 1, -1 do if state.stack[i].name == name then return state.stack[i] end end
            end,
            GetHUDScale = function() return 1 end,
            PushScreen = function(_, screen)
                state.stack[#state.stack]:OnBecomeInactive()
                state.stack[#state.stack + 1] = screen
                screen:OnBecomeActive()
            end,
            PopScreen = function()
                local screen = table.remove(state.stack)
                screen:OnBecomeInactive(); screen:OnDestroy()
                state.stack[#state.stack]:OnBecomeActive()
            end,
        }
        state.stack = { hud }
        hud.inst.ListenForEvent = function() end
        local controller = {
            inst = player, ismastersim = true, can_use_map = true,
            classified = {
                iscontrollerenabled = { value = function() return state.controller_enabled end,
                    set = function(_, value) state.controller_enabled = value end },
                ismapcontrolsvisible = { value = function() return state.map_enabled end },
                EnableMapControls = function(_, value) state.map_enabled = value end,
            },
            OnControl = function() state.native_controls = (state.native_controls or 0) + 1 end,
            DoCameraControl = function() state.camera = (state.camera or 0) + 1 end,
            UpdateActionsToMapActions = function() return nil, state.native_right end,
        }
        player.components.playercontroller = controller
        for _, method in ipairs({ "IsMapControlsEnabled", "EnableMapControls", "Enable" }) do
            controller[method] = native_function("components/playercontroller", "PlayerController", method, G)
        end
        player.components.inventory = { Hide = function() end, Show = function() end }
        function player:OnSleepIn() end
        function player:OnWakeUp() end
        function player:PushEvent() end
        function player:ShowActions() end
        state.sleep = native_function("stategraphs/SGwilson", nil, "SetSleeperSleepState", G)
        state.wake = native_function("stategraphs/SGwilson", nil, "SetSleeperAwakeState", G)
        hud.root = node("hud_root")
        hud.Hide = native_function("screens/playerhud", "PlayerHud", "Hide", G)
        hud.Show = native_function("screens/playerhud", "PlayerHud", "Show", G)
        local native = { ["widgets/mapcontrols"] = function()
            local controls = node("mapcontrols"); controls.pauseBtn = node("pause"); return controls
        end }
        local function load_native(path)
            local env = setmetatable({ require = function(name) return native[name] or Base end },
                { __index = function(_, key) return G[key] ~= nil and G[key] or _G[key] end })
            return T.native(path, env)
        end
        native["widgets/mapwidget"] = load_native("widgets/mapwidget")
        local NativeMap = load_native("screens/mapscreen")
        local callbacks, ctx = {}, nil
        native["screens/mapscreen"] = function(owner)
            local screen = NativeMap(owner)
            for _, callback in ipairs(callbacks) do callback(screen) end
            return screen
        end
        local NativeControls = load_native("widgets/controls")
        hud.controls = setmetatable({ owner = player, votedialog = { CloseControllerVoteScreen = function() end } }, NativeControls)
        local env = { GLOBAL = G, AddClassPostConstruct = function(_, fn) callbacks[#callbacks + 1] = fn end }
        require("fieldkit/game/map_input").install(env, function() return ctx end)
        MapView.install(env, function() return ctx end)
        function state:context(replacement)
            if replacement then player.HUD = replacement; hud = replacement end
            ctx = { G = G, player = player, hud = hud, scope = Scope.new(player, error), store = store, config = {
                minimap = true, travel = true,
            } }
            ctx.executor = Executor.new(function() return 0 end, function() return true end, function() end, error)
            local cancel = ctx.executor.cancel
            function ctx.executor:cancel(reason)
                state.cancelled = true
                if self.current then state.cancelled_jobs = (state.cancelled_jobs or 0) + 1 end
                return cancel(self, reason)
            end
            function ctx:text(_, en) return en end
            function ctx:notify(message) state.message = message end
            function ctx:report(message) error(message) end
            ctx.travel = {
                update_preview = function() end,
                route_snapshot = function() return state.route or { points = {}, path = {}, completed = 0 } end,
                pause_route = function(_, owner)
                    local job = ctx.executor.current
                    if not job or job.kind ~= "route" then return false end
                    job.owner = owner
                    return true
                end,
                resume_route = function(_, owner)
                    local job = ctx.executor.current
                    if job and job.owner == owner then job.owner = nil end
                end,
                is_editing = function(_, job, owner)
                    return job ~= nil and job.owner ~= nil and (not owner or job.owner == owner)
                end,
                navigate = function(_, point)
                    state.navigated[#state.navigated + 1] = point; ctx.executor.current = { kind = "route" }; return true
                end,
                add_waypoint = function(_, point) state.appended[#state.appended + 1] = point; return true end,
            }
            require("fieldkit/game/map_input").attach(ctx)
            ctx.mapview = MapView.new(ctx)
            ctx.minimap = Mini.new(ctx, ctx.mapview)
            return ctx
        end
        local input = { mouse_enabled = true, oncontrol = { HandleEvent = function(_, event, control, down)
            if event == "oncontrol" then controller:OnControl(control, down) end
        end } }
        input.OnControl = native_function("input", "Input", "OnControl", G)
        function G.TheFrontEnd:OnControl(control, down)
            local screen = self:GetActiveScreen()
            if screen == hud then return ctx.minimap.root:OnControl(control, down) end
            return screen:OnControl(control, down)
        end
        function state:control(control, down) input:OnControl(control, down, down and 1 or 0) end
        return state:context(), state, G
    end

    -- Completing a minimap command reports its result once through the native input entry.
    T.each("command feedback", { true, false }, function(append)
        local ctx, state, G = fixture()
        local messages = {}
        function ctx:notify(message) messages[#messages + 1] = message end
        local reason = append and "Waypoint added: 1" or "Destination unavailable"
        ctx.travel[append and "add_waypoint" or "navigate"] = function() return append, reason end
        assert(ctx.minimap:focus())
        local control = append and G.CONTROL_MENU_MISC_2 or G.CONTROL_MENU_MISC_3
        state:control(control, true)
        assert(#messages == 0)
        state:control(control, false)
        assert(#messages == 1 and messages[1] == reason)
        ctx.scope:close()
    end)

    do
        T.native("json")
        local json = require("json")
        local function profile(synchronous)
            local read, writes = nil, {}
            local encoded = json.encode({ version = 1, data = {
                minimap = { open = false, zoom = 9 }, recipes = { cookpot = { "meat" } },
            } })
            local store = Store.new({
                GetPersistentString = function(_, _, callback)
                    read = callback
                    if synchronous then callback(true, encoded) end
                end,
                SetPersistentString = function(_, _, data, _, callback)
                    writes[#writes + 1] = json.decode(data).data
                    callback(true)
                end,
            }, json, error)
            return store, function() read(true, encoded) end, writes
        end

        T.each("profile completion", { "synchronous", "delayed", "fullscreen" }, function(timing)
            local store, read, writes = profile(timing == "synchronous")
            local ctx, state, G = fixture(store)
            if timing ~= "synchronous" then
                assert(ctx.mapview.open and state.zoom == 5 and state.visible)
                if timing == "fullscreen" then ctx.hud.controls:ShowMap(G.Vector3(20, 0, 40)) end
                read()
                if timing == "fullscreen" then
                    assert(state.zoom == 1 and state.visible, "profile completion cannot zoom or hide the native full map")
                    G.TheFrontEnd:PopScreen()
                end
            end
            assert(not ctx.mapview.open and ctx.mapview.level == 9 and state.zoom == 9 and not state.visible)
            assert(#writes == 0, "loading settings alone never writes defaults back to the profile")
            ctx.minimap:set_open(true)
            ctx.mapview:zoom(-1)
            assert(#writes == 2 and writes[2].minimap.open and writes[2].minimap.zoom == 8)
            assert(writes[2].recipes.cookpot[1] == "meat", "map changes preserve the rest of the real profile")
            ctx.minimap:set_open(true)
            assert(#writes == 2, "unchanged settings do not trigger another write")
            local previous = ctx
            ctx = state:context()
            ctx.mapview:zoom(-1)
            previous.scope:close()
            assert(#writes == 3 and store:get("minimap").zoom == 7, "old HUD cleanup cannot overwrite its replacement")
            ctx.scope:close()
            assert(#writes == 3, "configuration is saved on changes, not during scope destruction")
        end)

        T.each("profile user edits", { "open", "zoom", "both" }, function(edit)
            local store, read, writes = profile()
            local ctx, state = fixture(store)
            if edit ~= "zoom" then assert(ctx.minimap:focus()) end
            if edit ~= "open" then ctx.mapview:zoom(-1) end
            assert(#writes == 0, "user input remains available while the profile is loading")
            read()
            local open, zoom = edit ~= "zoom", edit == "open" and 9 or 4
            assert(ctx.mapview.open == open and state.zoom == zoom and state.visible == open)
            assert(#writes == 1 and writes[1].minimap.open == open and writes[1].minimap.zoom == zoom,
                "explicit user choices win; fields untouched by the user retain their loaded values")
            ctx.scope:close()
            assert(#writes == 1)
        end)

        T.each("late profile completion", { "closed", "replaced" }, function(lifecycle)
            local store, read, writes = profile()
            local ctx, state = fixture(store)
            ctx.mapview:zoom(-1)
            local previous = ctx
            if lifecycle == "closed" then ctx.scope:close() else ctx = state:context() end
            read()
            assert(previous.mapview.level == 4 and #writes == 0,
                "late reads cannot update or save a closed or superseded view")
            if lifecycle == "closed" then
                assert(not state.visible and state.zoom == 3 and previous.minimap.root.killed)
                ctx = state:context()
            else
                previous.scope:close()
            end
            assert(not ctx.mapview.open and ctx.mapview.level == 9 and state.zoom == 9)
            ctx.scope:close()
            assert(#writes == 0 and store:get("minimap").zoom == 9)
        end)
    end

    do
        local ctx, state, G = fixture()
        local initial_cleanups = #ctx.scope.cleanups
        state.route = { points = { { x = 1, z = 1 } }, path = {}, origin = { x = 0, z = 0 }, completed = 0 }
        ctx.mapview:pan(17, 29)
        for _ = 1, 20 do
            -- Native ShowMap is also the bottle/revealmapspot path; it bypasses ToggleMap.
            ctx.hud.controls:ShowMap(G.Vector3(20, 0, 40))
            local map = state.stack[#state.stack]
            assert(map.name == "MapScreen" and state.zoom == 1 and not ctx.minimap.root.shown)
            local route = map.minimap.children[#map.minimap.children]
            assert(route.name == "fieldkit_map_annotations" and route.shown)
            local console = node("ConsoleScreen")
            G.TheFrontEnd:PushScreen(console)
            assert(not route.shown, "native map becoming inactive hides its route immediately, without an update tick")
            ctx.minimap.root:OnUpdate(.1)
            assert(state.visible and not ctx.minimap.root.shown, "temporary overlay cannot release the full map")
            G.TheFrontEnd:PopScreen()
            assert(route.shown, "native map returning to focus redraws the current snapshot")
            G.TheFrontEnd:PopScreen()
            assert(state.visible and state.zoom == 5 and state.offset_x == 17 and state.offset_y == 29)
            assert(route.killed, "each native map owns and removes exactly one route overlay")
        end
        assert(#ctx.scope.cleanups == initial_cleanups, "repeated maps cannot accumulate context cleanups")
        ctx.minimap:set_open(false)
        ctx.hud.controls:ToggleMap()
        assert(state.visible and state.zoom == 1)
        ctx.hud.controls:ToggleMap()
        assert(not state.visible and not ctx.mapview.open, "map close must preserve collapsed intent")
        ctx.minimap:set_open(true)
        ctx.hud.controls:ToggleMap()
        local previous = ctx
        ctx = state:context()
        previous.scope:close()
        assert(state.visible, "old HUD cleanup cannot hide the renderer owned by its replacement")
        G.TheFrontEnd:PopScreen()
        assert(state.visible and state.zoom == 5)
        ctx.scope:close()
        assert(not state.visible and state.zoom == 1)
    end

    do
        local ctx, state, G = fixture()
        local mini, route = ctx.minimap, ctx.minimap.route
        mini.root.test_scale = 2
        state.screen_width, state.screen_height = 3440, 1440
        state.route = { points = { { x = 10, z = 10 }, { x = 20, z = 10 } },
            path = { { x = 10, z = 10 } }, origin = { x = 0, z = 0 }, completed = 3 }
        mini.root:OnUpdate(.1)
        local line = route.lines[1]
        assert(line.position.x == 43 and line.position.y == 18 and line.tint[1] == .25,
            "verified route uses full renderer dimensions divided by HUD scale")
        local projected = mini:world_position(line.position.x * 2, line.position.y * 2)
        assert(projected.x == 10 and projected.z == 10, "route projection is the inverse of native cursor projection")
        assert(route.markers[1].text == "4" and route.used_markers == 1,
            "remaining markers preserve route numbering; off-panel waypoints do not leak outside the crop")
        local widgets = #route.root.children
        mini.root:OnUpdate(.1)
        assert(#route.root.children == widgets, "drawing reuses the same line and marker widgets")
        state.route.path[1].x = 5
        mini.root:OnUpdate(.04)
        assert(line.position.x == 43, "small frame deltas cannot exceed the 10 Hz rendering budget")
        mini.root:OnUpdate(.06)
        assert(line.position.x == 21.5)
        state.route = { points = {}, path = {}, completed = 0 }
        mini.root:OnUpdate(.1)
        assert(not route.root.shown, "cancelling a route hides every existing line and marker")

        state.route = { points = { { x = 80, z = 0 } }, path = {}, origin = { x = -80, z = 0 }, completed = 0 }
        mini.root:OnUpdate(.1)
        assert(route.root.shown and route.used_lines > 0)
        for i = 1, route.used_lines do
            local dash = route.lines[i]
            assert(dash.tint[1] == 1 and math.abs(dash.position.x) + dash.width / 2 <= 120 + 1e-9,
                "unplanned connectors are dashed and geometrically clipped before creating widgets")
        end

        local renderer = ctx.mapview.renderer
        local half_w, half_h
        native_function("screens/mapscreen", "MapScreen", "ProcessStaticDecorations", G)({
            minimap = renderer, decorationdata = { staticdecorations = {} },
            ProcessStaticDecorations_Internal = function(_, _, _, w, h) half_w, half_h = w, h end,
        })
        local full = require("fieldkit/ui/map_overlay").new(ctx, node("map"), renderer, function()
            return state.screen_width, state.screen_height, state.screen_width, state.screen_height
        end)
        state.route = { points = { { x = 10, z = 10 } }, path = { { x = 10, z = 10 } },
            origin = { x = 0, z = 0 }, completed = 0 }
        full:update(.1, true)
        local x, y = renderer:WorldPosToMapPos(10, 10, 0)
        assert(full.lines[1].position.x * 2 == x * half_w and full.lines[1].position.y * 2 == y * half_h,
            "full map overlay shares native decoration coordinates, independent of HUD scaling")
        assert(full.markers[1].text == "End 1")
        state.route.truncated = true
        state.route.path = {}
        for i = 1, 300 do state.route.path[i] = { x = i % 2 == 0 and 10 or -10, z = 0 } end
        full:update(.1, true)
        assert(full.used_lines == 256 and full.legend.text:match("partly"), "complex paths have a visible, fixed draw budget")
        full:close(); full:close(); full:update(.1, true)
        assert(full.root.killed and full.closed)
        ctx.scope:close()
        assert(route.root.killed, "HUD cleanup removes the cropped overlay as well")
    end

    do
        local ctx, state, G = fixture()
        local mini, root = ctx.minimap, ctx.minimap.root
        root.test_scale = 2
        state.screen_width, state.screen_height = 3440, 1440
        root:OnUpdate(.1)
        local point = mini:world_position(100, 50)
        assert(math.abs(point.x - 100 * 200 / 1720) < 1e-9 and math.abs(point.z - 100 * 100 / 720) < 1e-9,
            "cropped maps project through full texture dimensions, including HUD scale and ultrawide")
        local origin = root:GetWorldPosition()
        state.pointer = { x = origin.x + 20, y = origin.y + 20 }
        assert(mini:consumes_pointer())
        ctx.player.components.playercontroller:DoCameraControl()
        assert(not state.camera, "minimap scrolling must not also zoom the world camera")
        root:OnMouseButton(G.MOUSEBUTTON_LEFT, true, state.pointer.x, state.pointer.y)
        state.pressed[G.CONTROL_PRIMARY] = true
        state.pointer.x = state.pointer.x + 90
        root:OnUpdate(.1)
        assert(state.offset_x == 20, "drag uses native screen pixel units, not clipped panel size")
        state.pressed[G.CONTROL_PRIMARY] = false
        root:OnUpdate(.1)
        assert(not mini.drag, "release outside the map cannot leave a drag active")
        assert(mini:focus() and state.modfocus)
        assert(mini:handle_control(G.CONTROL_MAP_ZOOM_IN, true))
        assert(mini:handle_control(G.CONTROL_MAP_ZOOM_IN, false) and state.zoom == 4)
        state.axis[G.CONTROL_MOVE_RIGHT] = 1
        root:OnUpdate(.1)
        assert(state.offset_x == 10)
        assert(mini:handle_control(G.CONTROL_MENU_MISC_2, true))
        mini:handle_control(G.CONTROL_MENU_MISC_2, false)
        assert(#state.appended == 1 and mini.focused, "adding a waypoint retains controller map focus")
        assert(mini:handle_control(G.CONTROL_MENU_MISC_3, true))
        assert(mini:handle_control(G.CONTROL_MENU_MISC_3, false), "navigation release cannot leak into gameplay")
        assert(#state.navigated == 1 and not state.modfocus)
        state.native_right = { action = {} }
        root:OnMouseButton(G.MOUSEBUTTON_RIGHT, true, origin.x, origin.y)
        assert(#state.navigated == 1 and state.stack[#state.stack].name == "MapScreen", "native skills retain priority")
        G.TheFrontEnd:PopScreen()
        mini:focus()
        ctx.scope:close()
        assert(not state.modfocus and root.killed)
    end

    do
        local ctx, state, G = fixture()
        local mini, controller = ctx.minimap, ctx.player.components.playercontroller
        ctx.selection = { handle_control = function() return false end }
        require("fieldkit/game/input").attach(ctx)
        assert(mini:focus())
        state:control(G.CONTROL_MAP, true)
        assert(G.TheFrontEnd:GetActiveScreen() == ctx.hud, "holding the map key cannot transfer its release into MapScreen")
        state:control(G.CONTROL_MAP, false)
        assert(G.TheFrontEnd:GetActiveScreen().name == "MapScreen", "one complete map-key gesture opens the native map")
        state:control(G.CONTROL_MAP, true)
        state:control(G.CONTROL_MAP, false)
        assert(G.TheFrontEnd:GetActiveScreen() == ctx.hud, "the next gesture closes it through native MapScreen.OnControl")
        assert(mini:focus())
        -- Native input dispatches both controls mapped to physical A; focus moves only after both downs.
        state:control(G.CONTROL_ACCEPT, true)
        state:control(G.CONTROL_CONTROLLER_ACTION, true)
        assert(#state.navigated == 0 and mini.focused)
        state:control(G.CONTROL_ACCEPT, false)
        state:control(G.CONTROL_CONTROLLER_ACTION, false)
        assert(#state.navigated == 1 and ctx.executor.current and not state.cancelled_jobs and not state.native_controls,
            "A aliases cannot cancel the new route, navigate twice, or reach a native world action")
        mini:focus()
        local route = ctx.executor.current
        assert(route.owner == mini and not state.cancelled_jobs, "controller editing pauses the active route")
        state:control(G.CONTROL_CANCEL, true)
        state:control(G.CONTROL_CONTROLLER_ALTACTION, true)
        state:control(G.CONTROL_CANCEL, false)
        state:control(G.CONTROL_CONTROLLER_ALTACTION, false)
        assert(not mini.focused and not state.native_controls, "B aliases stay owned through focus release")
        assert(ctx.executor.current == route and route.owner == nil, "leaving minimap editing resumes the same route")

        local pointer = mini.root:GetWorldPosition()
        mini.root:OnMouseButton(G.MOUSEBUTTON_LEFT, true, pointer.x, pointer.y)
        assert(route.owner == mini and mini.drag)
        mini.root:OnMouseButton(G.MOUSEBUTTON_LEFT, false, -1000, -1000)
        assert(ctx.executor.current == route and route.owner == nil and not mini.drag,
            "releasing a map drag outside the map resumes without cancelling or replacing the route")

        mini:focus()
        state.sleep(ctx.player)
        mini.root:OnUpdate(.1)
        assert(not ctx.mapview:active() and not state.visible and not mini.root.shown and not state.modfocus,
            "native tent/bedroll sleep disables the minimap and releases its input focus")
        assert(not mini:focus())
        mini.buttons[5].onclick()
        assert(G.TheFrontEnd:GetActiveScreen() == ctx.hud, "ShowMap button cannot bypass disabled map controls")
        state.wake(ctx.player)
        mini.root:OnUpdate(.1)
        assert(ctx.mapview:active() and state.visible and mini.root.shown)
        -- Gift interaction uses the same native controller gates without a sleep transition.
        controller:EnableMapControls(false)
        assert(not mini:focus())
        mini.buttons[5].onclick()
        assert(G.TheFrontEnd:GetActiveScreen() == ctx.hud)
        controller:EnableMapControls(true)
        ctx.hud:Hide()
        assert(not ctx.hud.shown and not ctx.hud.root.shown)
        assert(not mini:focus())
        mini.buttons[5].onclick()
        mini.root:OnUpdate(.1)
        assert(not mini.root.shown and not state.visible, "native HUD Hide only hides hud.root, so detached overlays must gate on shown")
        ctx.hud:Show()
        mini.root:OnUpdate(.1)
        assert(ctx.mapview:active() and state.visible)
        ctx.scope:close()
    end

    T.each("minimap pointer orders", T.pointer_orders, function(order)
        local ctx, state, G = fixture()
        local mini = ctx.minimap
        ctx.selection = { handle_control = function() return false end }
        require("fieldkit/game/input").attach(ctx)
        state.pointer = mini.root:GetWorldPosition()
        local function raw(down)
            local accepted = mini.root:OnMouseButton(G.MOUSEBUTTON_RIGHT, down, state.pointer.x, state.pointer.y)
            if down then assert(accepted) end
        end
        local function mapped(down) state:control(G.CONTROL_SECONDARY, down) end
        T.pointer(raw, mapped, true, order.down)
        state.pointer = { x = -2000, y = -2000 }
        T.pointer(raw, mapped, false, order.up)
        assert(#state.navigated == 1 and ctx.executor.current and not state.cancelled_jobs and not state.native_controls,
            "raw mouse and control channels cannot cancel their own navigation, regardless of delivery order")
        ctx.scope:close()
    end)


    T.each("stop held gesture", { false, true }, function(running)
        T.each("append", { false, true }, function(append)
            local ctx, state, G = fixture()
            if running then ctx.executor.current = { kind = "route" } end
            assert(ctx.minimap:focus())
            local control = append and G.CONTROL_MENU_MISC_2 or G.CONTROL_MENU_MISC_3
            state:control(control, true)
            ctx.executor:cancel("cancelled")
            state:control(control, false)
            assert(not ctx.executor.current and #state.navigated == 0 and #state.appended == 0 and not state.native_controls,
                "stop invalidates held minimap commands and still consumes their release")
            state:control(control, true)
            state:control(control, false)
            assert(#state.navigated + #state.appended == 1, "a fresh minimap gesture remains usable after stop")
            ctx.scope:close()
        end)
    end)
end)
T.done()
