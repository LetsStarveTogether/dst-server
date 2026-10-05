local T = require("tests.client_mod.support")
T.case("selection ownership", function()
    local Selection = require("fieldkit/features/selection")
    local Scope = require("fieldkit/core/scope")
    local Actions = require("fieldkit/game/actions")

    local widget = require("tests.client_mod.support.widgets")
    widget.install()

    local function entity(id, x, z, action_id, prefab)
        local e = T.entity({ GUID = id, x = x, z = z, prefab = prefab or "tree", action_id = action_id or "CHOP" })
        e.entity.IsVisible = function() return not e.invisible end
        return e
    end

    local now, ready, hud_under_mouse, attached, current_target = 1, true, nil, false, nil
    local screen, keys, markers, enqueues, removals, areas = { x = 0, y = 0 }, {}, {}, {}, {}, {}
    local handlers = T.handlers()
    local player = entity(0, 0, 0)
    local tasks = T.tasks(player)
    local a, b, c = entity(1, 1, 1), entity(2, 2, 1), entity(3, 3, 1, "DIG")
    local d = entity(4, 2, 3, "PICKUP", "flint")
    local hidden = entity(5, 2, 2); hidden.invisible = true
    local foreign_player = entity(6, 2, 2); foreign_player.tags = { player = true }
    local found = { a, b, c, d, hidden, foreign_player }
    local controller = { IsControlPressed = function(_, control) return keys[control], nil, "native" end }
    player.components = { playercontroller = controller }
    local input = {
        IsKeyDown = function(_, key) return keys[key] end,
        GetScreenPosition = function() return { x = screen.x, y = screen.y } end,
        GetWorldEntityUnderMouse = function() return current_target end,
        GetHUDEntityUnderMouse = function() return hud_under_mouse end,
        ControllerAttached = function() return attached end,
        AddMoveHandler = function(_, fn) return handlers:add("move", 0, fn) end,
        AddMouseButtonHandler = function(_, fn) return handlers:add("mouse", 0, fn) end,
    }
    local map = { GetPlatformAtPoint = function() return nil end }
    local G = { TheInput = input, TheWorld = { Map = map }, GetTime = function() return now end,
        KEY_ALT = 400, KEY_CTRL = 401, CONTROL_PRIMARY = 1, CONTROL_SECONDARY = 2,
        MOUSEBUTTON_LEFT = 1000, MOUSEBUTTON_RIGHT = 1001,
        CONTROL_FORCE_INSPECT = 3, CONTROL_FORCE_ATTACK = 4, CONTROL_FORCE_TRADE = 5, CONTROL_FORCE_STACK = 6,
        CanEntitySeeTarget = function() return true end,
        TheSim = { FindEntities = function() return found end,
            ProjectScreenPos = function(_, x, y) return x / 10, 0, y / 10 end },
        SpawnPrefab = function(name)
            assert(name == "axisalignedplacement_outline")
            local marker = entity(100 + #markers, 0, 0)
            marker.Transform.SetPosition = function(_, x, _, z) marker.x, marker.z = x, z end
            marker.AnimState = { PlayAnimation = function() end, SetScale = function() end, SetMultColour = function() end }
            function marker:Remove() self.removed = true end
            function marker:Show() self.shown = true end
            function marker:Hide() self.shown = false end
            markers[#markers + 1] = marker
            return marker
        end,
    }
    local ctx = { G = G, player = player, hud = widget(), config = {}, params = require("fieldkit/core/parameters").new(), work = {}, construction = {} }
    function ctx:ready() return ready end
    function ctx:text(_, en) return en end
    function ctx:notify(message) self.message = message end
    ctx.scope = Scope.new(player, error)
    Actions.attach(ctx)
    function ctx.work:preview(target, button)
        return Actions.query(ctx, function()
            assert(not controller:IsControlPressed(G.CONTROL_FORCE_INSPECT), "selection modifiers must not turn work into inspect")
            if target.action_id then return { action = { id = target.action_id }, button = button } end
        end)
    end
    function ctx.work:revise() return true end
    function ctx.work:enqueue(entries) enqueues[#enqueues + 1] = entries; return true end
    function ctx.work:remove(target) removals[#removals + 1] = target end
    function ctx.construction:area(area) areas[#areas + 1] = area; return true end
    local selection = Selection.new(ctx)
    local function control(id, down) return selection:handle_control(controller, id, down) end
    local function click(target, button)
        current_target = target
        local id = button == "right" and 2 or 1
        assert(control(id, true))
        assert(control(id, false))
    end
    local function move(x, y)
        screen.x, screen.y = x, y
        handlers:dispatch("move", 0, x, y)
    end
    local function tick() tasks[1].fn() end

    -- Only matching selection-owned press/release pairs are consumed.
    assert(not control(1, true) and not control(1, false))
    keys[G.KEY_ALT], keys[G.CONTROL_FORCE_INSPECT] = true, true
    assert(not control(1, false), "a native mouse-up stays native even if Alt is now held")
    assert(controller:IsControlPressed(G.CONTROL_FORCE_INSPECT), "outside the query native modifiers are untouched")
    local value, middle, tail = controller:IsControlPressed(999)
    assert(value == nil and middle == nil and tail == "native", "unknown controls preserve all return values")
    current_target = a
    assert(control(1, true))
    keys[G.KEY_ALT] = false
    assert(not controller:IsControlPressed(1), "captured click cannot chain native combat")
    assert(control(1, false) and selection.selected[a] and #enqueues == 1)
    assert(not control(1, false), "duplicate mouse-up is not owned")
    assert(markers[1].Network == nil and markers[1].shown)
    keys[G.KEY_ALT] = true

    -- Same prefab with a different native action is not a same-kind selection.
    now = 1.2
    click(a)
    assert(selection.selected[a] and selection.selected[b] and not selection.selected[c])
    assert(#selection.entries == 2 and #enqueues[2] == 2)
    now = 2
    click(c, "right")
    assert(selection.selected[c].button == "right" and enqueues[3][3].button == "right")
    assert(enqueues[3][1].button == "left", "adding a right action does not rewrite existing left intentions")

    keys[G.KEY_CTRL] = true
    a.action_id = nil
    click(a)
    assert(not selection.selected[a] and removals[#removals] == a, "remove works after the target loses its action")
    keys[G.KEY_CTRL] = nil
    a.action_id = "CHOP"
    selection:clear()

    -- First drag works without any earlier mouse-move event; live preview has no gameplay effect.
    current_target = nil
    move(0, 0)
    assert(control(1, true))
    move(40, 40)
    tick()
    assert(#selection.entries == 0 and #selection.preview == 4 and selection:area() == nil)
    assert(ctx.hud.children[1].shown and ctx.hud.children[1].clickable == false)
    local before = #enqueues
    assert(control(1, false))
    assert(#selection.entries == 4 and #enqueues == before + 1)
    assert(selection:area() and not ctx.hud.children[1].shown)
    assert(not selection.selected[hidden] and not selection.selected[foreign_player])

    -- Region deletion is membership-based, not dependent on having a currently usable tool.
    keys[G.KEY_CTRL] = true
    for _, target in ipairs({ a, b, c, d }) do target.action_id = nil end
    move(0, 0); control(1, true); move(40, 40); control(1, false)
    assert(#selection.entries == 0 and #enqueues == before + 1)
    keys[G.KEY_CTRL] = nil
    a.action_id, b.action_id, c.action_id, d.action_id = "CHOP", "CHOP", "DIG", "PICKUP"

    -- Empty ground regions become native deploy/till/build intents rather than a walk request.
    move(100, 100); control(2, true); move(120, 120); control(2, false)
    assert(#areas == 1 and areas[1].button == "right")
    ctx.config.selection_auto_execute = false
    move(0, 0); control(1, true); move(40, 40); control(1, false)
    assert(#enqueues == before + 1 and selection:commit() and #enqueues == before + 2)

    -- HUD focus, AOE targeting, controller mode and already-started native dragging stay native.
    hud_under_mouse = {}; assert(not control(1, true)); hud_under_mouse = nil
    ready = false; assert(not control(1, true)); ready = true
    attached = true; assert(not control(1, true)); attached = false
    controller.IsAOETargeting = function() return true end
    assert(not control(1, true)); controller.IsAOETargeting = nil
    controller.draggingonground = true; assert(not control(1, true)); controller.draggingonground = nil
    move(0, 0); control(1, true); move(40, 40)
    ready = false; tick()
    assert(selection.drag == nil and control(1, false), "focus loss cancels the gesture but consumes its owned release")
    ready = true

    -- Run the native Input dispatch and TextEdit consumption, without renderer/widget construction.
    do
        local base = { OnControl = function() return false end, IsEnabled = function() return true end }
        local env = setmetatable({
            require = function() return base end,
            ValidateLineNumber = function() end,
            IsSteamDeck = function() return false end,
            CONTROL_ACCEPT = 20, CONTROL_CANCEL = 21, CONTROL_OPEN_DEBUG_CONSOLE = 22,
            Class = T.Class,
        }, { __index = function(_, key) return G[key] ~= nil and G[key] or _G[key] end })
        env.IsNotConsole = function() return false end
        env.TheInputProxy = env.TheInputProxy or {}
        env.TheInputProxy.GetInputDeviceCount = function() return 0 end
        T.native("events", env)
        local function native(path) return T.native(path, env) end
        local NativeInput, TextEdit = native("input"), native("widgets/textedit")
        local editor = setmetatable({ editing = true, ignore_controls = {}, pass_controls_to_screen = {} }, TextEdit)
        local gui, native_controls = false, 0
        env.TheFrontEnd = {
            OnControl = function(_, id, down) return gui and editor:OnControl(id, down) end,
            OnMouseButton = function() return true end,
        }
        local dispatch = { mouse_enabled = true,
            oncontrol = { HandleEvent = function(_, id, down)
                if type(id) == "number" and not control(id, down) then native_controls = native_controls + 1 end
            end },
            onmousebutton = { HandleEvent = function(_, _, button, down)
                handlers:dispatch("mouse", 0, button, down)
            end },
        }
        local function pointer(id, down, raw_first)
            local button = id == G.CONTROL_PRIMARY and G.MOUSEBUTTON_LEFT or G.MOUSEBUTTON_RIGHT
            T.pointer(function(value) NativeInput.OnMouseButton(dispatch, button, value, 0, 0) end,
                function(value) NativeInput.OnControl(dispatch, id, value) end, down, raw_first)
        end
        T.each("selection pointer orders", T.pointer_orders, function(order)
            for _, id in ipairs({ G.CONTROL_PRIMARY, G.CONTROL_SECONDARY }) do
                selection:clear()
                keys[G.KEY_ALT], current_target = true, a
                local before_controls = native_controls
                pointer(id, true, order.down)
                pointer(id, false, order.up)
                assert(selection.selected[a] and selection.owned[id] == nil,
                    "both raw/control release orders preserve the completed selection")
                assert(native_controls == before_controls, "an owned release cannot reach native player controls")
            end

            selection:clear()
            keys[G.KEY_ALT], current_target = true, a
            pointer(G.CONTROL_SECONDARY, true, order.down)
            gui, ready = true, false
            tick()
            local before_controls = native_controls
            pointer(G.CONTROL_SECONDARY, false, order.up)
            assert(native_controls == before_controls and selection.owned[G.CONTROL_SECONDARY] == "released",
                "native TextEdit consumes control-up, while raw-up still records the release")
            gui, ready, keys[G.KEY_ALT] = false, true, false
            -- No tick or deferred task: the immediately following ordinary click stays native.
            pointer(G.CONTROL_SECONDARY, true, order.down)
            assert(native_controls == before_controls + 1 and selection.drag == nil)
            pointer(G.CONTROL_SECONDARY, false, order.up)
            assert(native_controls == before_controls + 2 and selection.owned[G.CONTROL_SECONDARY] == nil)

            keys[G.KEY_ALT] = true
            pointer(G.CONTROL_SECONDARY, true, order.down)
            gui = true
            pointer(G.CONTROL_SECONDARY, false, order.up)
            gui, current_target = false, b
            pointer(G.CONTROL_SECONDARY, true, order.down)
            assert(selection.drag and selection.drag.target == b, "the next modified press replaces the abandoned gesture")
            pointer(G.CONTROL_SECONDARY, false, order.up)
            assert(selection.selected[b] and not selection.selected[a])
        end)
        selection:clear()
    end

    -- The non-mouse two-corner entry uses the same selection policy.
    selection:clear()
    assert(selection:mark_start({ x = 0, z = 0 }, "right"))
    assert(selection:mark_end({ x = 4, z = 4 }))
    assert(#selection.entries == 4 and selection.entries[1].button == "right")
    assert(not selection:mark_end({ x = 4, z = 4 }))

    local boat = entity(200, 0, 0)
    boat.entity.WorldToLocalSpace = function(_, x, y, z) return x - boat.x, y, z - boat.z end
    boat.entity.LocalToWorldSpace = function(_, x, y, z) return x + boat.x, y, z + boat.z end
    map.GetPlatformAtPoint = function() return boat end
    selection:mark_start({ x = 0, z = 0 })
    boat.x = 10
    assert(selection:mark_end({ x = 14, z = 4 }))
    assert(selection:area().corners[1].x == 10, "first corner tracks a moving boat before confirmation")
    boat.x = 20
    assert(selection:area().corners[1].x == 20 and selection:area().corners[3].x == 24)
    boat.removed = true
    assert(selection:area() == nil)
    boat.removed = false
    map.GetPlatformAtPoint = function(_, x) return x > 2 and boat or nil end
    selection:mark_start({ x = 0, z = 0 })
    local marked, reason = selection:mark_end({ x = 4, z = 4 })
    assert(not marked and reason == "Select an area on one surface or platform" and ctx.message == nil)

    local wrapped = controller.IsControlPressed
    controller.IsControlPressed = function(self, ...) return wrapped(self, ...) end
    local later = controller.IsControlPressed
    ctx.scope:close()
    for _, handle in ipairs(handlers.list) do assert(handle.removed) end
    assert(tasks[1].cancelled and ctx.hud.children[1].killed)
    for _, marker in ipairs(markers) do assert(marker.removed) end
    assert(controller.IsControlPressed == later and controller:IsControlPressed(G.CONTROL_FORCE_INSPECT))
    assert(not selection:toggle(a), "closed owners cannot recreate resources")

    -- Apply semantic/spatial filters before the budget, even in a crowded scene.
    ctx.scope = Scope.new(player, error)
    Actions.attach(ctx)
    map.GetPlatformAtPoint = function() return nil end
    selection = Selection.new(ctx)
    local function crowded_scene(count)
        found = {}
        for i = 1, 160 do found[#found + 1] = entity(1000 + i, .01 * i, .1, "PICKUP", "unrelated") end
        for i = 1, count do found[#found + 1] = entity(2000 + i, 10 + .01 * i, 1, "CHOP", "selected_tree") end
    end
    crowded_scene(160)
    assert(selection:select_same(found[161], "left") and #selection.entries == 128,
        "160 unrelated nearer entities cannot hide the nearest eligible targets")
    assert(ctx.message == "Target limit: 128; narrow the selection", "truncated selection reports its fixed limit")
    selection:clear()
    crowded_scene(300)
    assert(selection:mark_start({ x = 9, z = 0 }, "right"))
    assert(selection:mark_end({ x = 14, z = 2 }) and #selection.entries == 128,
        "the fixed 128-target budget applies after rectangle filtering")
    assert(selection.entries[1].target.GUID == 2001 and selection.entries[128].target.GUID == 2128,
        "eligible targets retain deterministic nearest-first ordering")
    assert(selection:commit() and #enqueues[#enqueues] == 128)
    assert(#require("fieldkit/game/query").nearby(ctx, 20) == 128, "unmodified callers keep their original budget")

    -- A displayed button only changes after the queue accepts the new intent.
    selection:clear()
    assert(selection:toggle(a, "left"))
    function ctx.work:revise(target, button)
        assert(target == a and button == "right")
        return false, "Queued action could not be changed"
    end
    local accepted, reason = selection:toggle(a, "right")
    assert(not accepted and reason == "Queued action could not be changed")
    assert(selection.selected[a].button == "left", "a rejected revision preserves the accepted intent")
    function ctx.work:revise(target, button) return true, { target = target, button = button } end
    assert(selection:toggle(a, "right") and selection.selected[a].button == "right")
    function ctx.work:revise() return true end

    -- Real selection release owns command feedback, including the construction rejection.
    selection:clear()
    ctx.config.selection_auto_execute = true
    local messages = {}
    function ctx:notify(message) messages[#messages + 1] = message end
    function ctx.work:enqueue() return false, "Queue admission rejected" end
    keys[G.KEY_ALT], current_target = true, found[161]
    click(current_target)
    assert(#messages == 1 and messages[1] == "Queue admission rejected")
    player.replica, ctx.executor = {}, { is_running = function() return true end }
    ctx.construction = require("fieldkit/features/construction").new(ctx)
    current_target = nil
    screen = { x = 150, y = 100 }
    assert(control(2, true))
    screen = { x = 170, y = 120 }
    assert(control(2, false))
    assert(#messages == 2 and messages[2] == "Stop the current operation first",
        "an empty-ground construction failure reaches the player exactly once")
    ctx.scope:close()
end)
T.done()
