local T = require("tests.client_mod.support")
local PointerInput = require("fieldkit/game/pointer_input")
local Scope = require("fieldkit/core/scope")
T.case("native_range_gesture_lifecycle", function()
    local now, held, hover, hudhit, mouse, forwarded, world_controls, camera = 0, false, nil, nil, nil, 0, 0, 0
    local keys, pins, groups = {}, {}, {}
    local ctx = { player = { components = {} }, config = { range_hints = true }, hud = {} }
    local hud = ctx.hud
    local frontend = { screen = hud }
    function frontend:GetActiveScreen() return self.screen end
    function hud:OnMouseButton() forwarded = forwarded + 1; return "native" end
    function hud:OnControl() return false end
    local controller = {}
    function controller:IsControlPressed(control) return ctx.G.TheInput:IsControlPressed(control) end
    function controller:DoCameraControl()
        if ctx.G.TheInput:IsControlPressed(ctx.G.CONTROL_ROTATE_LEFT) then camera = camera + 1 end
    end
    ctx.player.components.playercontroller = controller
    ctx.scope = Scope.new(ctx.player, error)
    function ctx:ready() return not self.blocked end
    ctx.awareness = {
        can_pin = function(_, target) return target and target.supported end,
        toggle_pin = function(_, target)
            target.pinned = not target.pinned
            pins[#pins + 1] = target
            return true, nil, target.pinned
        end,
        toggle_group = function(_, target, show) groups[#groups + 1] = { target = target, show = show } end,
    }
    local G = { TheFrontEnd = frontend, GetTime = function() return now end,
        KEY_CTRL = 306, KEY_ALT = 308, KEY_SHIFT = 304, MOUSEBUTTON_MIDDLE = 1002, MOUSEBUTTON_LEFT = 1000,
        CONTROL_PRIMARY = 1, CONTROL_ACCEPT = 2, CONTROL_ZOOM_IN = 3, CONTROL_ROTATE_LEFT = 4, CONTROL_ACTION = 7,
        CONTROL_ZOOM_OUT = 5, CONTROL_ROTATE_RIGHT = 6,
    }
    local mappings = { [G.CONTROL_PRIMARY] = G.MOUSEBUTTON_LEFT }
    G.TheInputProxy = { GetLocalizedControl = function(_, _, control)
        return 1, 1, mappings[control]
    end }
    G.TheInput = {
        mouse_enabled = true,
        ControllerAttached = function() return false end,
        IsMouseDown = function(_, button) return button == G.MOUSEBUTTON_MIDDLE and held end,
        IsControlPressed = function() return true end,
        IsKeyDown = function(_, key) return keys[key] end,
        GetWorldEntityUnderMouse = function() return hover end,
        GetHUDEntityUnderMouse = function() return hudhit end,
        AddMouseButtonHandler = function(_, fn) mouse = fn; return { Remove = function() mouse = nil end } end,
        oncontrol = { HandleEvent = function() world_controls = world_controls + 1 end },
        onmousebutton = { HandleEvent = function(_, _, ...) if mouse then mouse(...) end end },
    }
    ctx.G = G
    -- Use native input dispatch: HUD handles raw buttons before the global broadcast, and owns consumed controls.
    local env = setmetatable({ Class = T.Class, IsNotConsole = function() return false end, ValidateLineNumber = function() end,
        require = function() return {} end }, { __index = function(_, key) return G[key] or _G[key] end })
    G.TheInputProxy.GetInputDeviceCount = function() return 0 end
    T.native("events", env)
    local NativeInput = T.native("input", env)
    env.TheInput = G.TheInput
    function frontend:OnControl(control, down)
        self.isprimary = control == G.CONTROL_PRIMARY
        local result = hud:OnControl(self.isprimary and G.CONTROL_ACCEPT or control, down)
        self.isprimary = false
        return result
    end
    function frontend:OnMouseButton(...) return hud:OnMouseButton(...) end
    local function raw(down)
        held = down
        NativeInput.OnMouseButton(G.TheInput, G.MOUSEBUTTON_MIDDLE, down, 10, 20)
    end
    local function control(down)
        held = down
        NativeInput.OnControl(G.TheInput, G.CONTROL_PRIMARY, down)
    end
    ctx.travel = { handle_pointer = function() return false end }
    PointerInput.attach(ctx)
    hover = { prefab = "lightning_rod", supported = true }
    raw(true); raw(true); raw(false)
    assert(#pins == 1 and forwarded == 0 and ctx.pointer_event.down == false, "a whole middle gesture belongs to range input once")
    now = .2; raw(true); raw(false)
    assert(#pins == 1 and #groups == 1 and groups[1].show == true, "double click enables all matching circles after a first enable")
    now = 1; raw(true); raw(false)
    now = 1.2; raw(true); raw(false)
    assert(#pins == 2 and #groups == 2 and groups[2].show == false, "double click disabling follows the first click's intent")
    ctx.config.range_double_click = .6
    now = 2; raw(true); raw(false)
    now = 2.55; raw(true); raw(false)
    assert(#pins == 3 and #groups == 3, "configured slower double clicks still share the first click's intent")
    ctx.config.range_double_click = .3
    now = 3.5; raw(true); raw(false)
    now = 4.05; raw(true); raw(false)
    assert(#pins == 5 and #groups == 3, "clicks outside the configured interval remain independent")

    local count = #pins
    T.each("input_blocker", {
        { name = "hud", set = function(value) hudhit = value end },
        { name = "editor", set = function(value) frontend.textProcessorWidget = value end },
        { name = "focus", set = function(value) hud.modfocus = value end },
        { name = "player", set = function(value) ctx.blocked = value end },
    }, function(block)
        block.set(true); now = now + 1; raw(true); raw(false); block.set(nil)
        assert(#pins == count, "inventory, editing and input focus retain middle clicks")
    end)
    ctx.config.range_modifier = "ctrl"
    now = now + 1; raw(true); raw(false); assert(#pins == count)
    keys[G.KEY_CTRL] = true
    raw(true); keys[G.KEY_CTRL] = nil; raw(false)
    assert(#pins == count + 1, "modifier release cannot leak the accepted pointer-up")
    ctx.config.range_modifier = "none"
    ctx.config.range_modifier = "off"
    now = now + 1; raw(true); raw(false)
    assert(#pins == count + 1, "disabling range clicks leaves native middle-click input available")
    ctx.config.range_modifier = "none"

    -- A remapped gameplay action must not execute before/after the corresponding raw event.
    mappings[G.CONTROL_PRIMARY] = G.MOUSEBUTTON_MIDDLE
    T.each("dispatch_order", T.pointer_orders, function(order)
        now = now + 1; count = #pins
        T.pointer(raw, control, true, order.down)
        assert(#pins == count + 1 and not controller:IsControlPressed(G.CONTROL_PRIMARY))
        T.pointer(raw, control, false, order.up)
        assert(world_controls == 0 and controller:IsControlPressed(G.CONTROL_PRIMARY),
            "both native control and raw paths are consumed without duplicate toggle")
    end)
    held = false
    NativeInput.OnControl(G.TheInput, G.CONTROL_ACCEPT, true)
    NativeInput.OnControl(G.TheInput, G.CONTROL_ACCEPT, false)
    assert(#pins == count + 1 and world_controls == 4, "keyboard accept stays native despite the primary action remapping")

    mappings[G.CONTROL_ROTATE_LEFT] = G.MOUSEBUTTON_MIDDLE
    mappings[G.CONTROL_ACTION] = G.MOUSEBUTTON_MIDDLE
    now = now + 1; raw(true); controller:DoCameraControl(); assert(camera == 0)
    local repeats = 0
    -- Native PlayerController.OnUpdate polls this method directly before repeating CONTROL_ACTION.
    if G.TheInput:IsControlPressed(G.CONTROL_ACTION) then repeats = repeats + 1 end
    assert(repeats == 0, "held action-repeat polling shares the owned gesture boundary")
    -- An overlay can receive the raw release without routing it through PlayerHud.
    frontend.screen = {}; held = false; mouse(G.MOUSEBUTTON_MIDDLE, false)
    controller:DoCameraControl(); assert(camera == 1 and controller:IsControlPressed(G.CONTROL_PRIMARY))
    frontend.screen = hud
    now = now + 1; count = #pins; raw(true); raw(false); assert(#pins == count + 1)
    ctx.scope:close(); assert(mouse == nil)
    count = #pins; raw(true); raw(false); assert(#pins == count, "closed contexts leave native input behavior")
end)

T.done()
