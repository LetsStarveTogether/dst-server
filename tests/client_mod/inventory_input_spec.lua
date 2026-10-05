local T = require("tests.client_mod.support")
local Input = require("fieldkit/game/inventory_input")
local Policy = require("fieldkit/game/input")
local Scope = require("fieldkit/core/scope")
T.case("native_inventory_gesture_lifecycle", function()
    local state = { now = 0, keys = {}, native = 0, camera = 0, cancelled = 0, accepted = true, requests = 0 }
    local G = { CONTROL_SCROLLBACK = 1, CONTROL_SCROLLFWD = 2, CONTROL_FORCE_STACK = 3, CONTROL_FORCE_TRADE = 4,
        CONTROL_ACCEPT = 5, CONTROL_SECONDARY = 6, CONTROL_ZOOM_IN = 7, CONTROL_ZOOM_OUT = 8, MOUSEBUTTON_MIDDLE = 9,
        MOUSEBUTTON_SCROLLUP = 10, MOUSEBUTTON_SCROLLDOWN = 11 }
    G.GetTime = function() return state.now end
    G.TheInput = { IsControlPressed = function(_, key) return state.keys[key] end,
        ControllerAttached = function() return state.gamepad end, GetHUDEntityUnderMouse = function() return not state.offhud end }
    local ctx = { G = G, hud = {}, player = { replica = {}, components = { playercontroller = {
        DoCameraControl = function() state.camera = state.camera + 1 end,
    } } }, config = { inventory_scroll = true, inventory_middle = true }, params = require("fieldkit/core/parameters").new() }
    T.events(ctx.player)
    ctx.scope = Scope.new(ctx.player, error)
    G.TheFrontEnd = { GetActiveScreen = function() return state.screen or ctx.hud end }
    ctx.executor = { cancel = function(self) self.current = nil; state.cancelled = state.cancelled + 1 end }
    ctx.notify = function(_, message) state.message = message end
    ctx.selection = { handle_control = function() return false end }
    ctx.storage = {}
    for _, method in ipairs({ "scroll", "transfer", "collect", "return_active" }) do
        ctx.storage[method] = function(_, ...)
            state.method, state.args, state.requests = method, { ... }, state.requests + 1
            if state.accepted then ctx.executor.current = { input = { mode = "inventory" } } end
            return state.accepted, "rejected"
        end
    end
    local callbacks = {}
    Input.install({ GLOBAL = G, AddClassPostConstruct = function(name, fn) callbacks[name] = fn end }, function() return ctx end)
    local slot = { owner = ctx.player, focus = true, container = {}, num = 2,
        OnControl = function() state.native = state.native + 1; return false end,
        OnMouseButton = function() state.native = state.native + 1; return false end }
    callbacks["widgets/invslot"](slot)

    local env = setmetatable({ require = function() return {} end, ValidateLineNumber = function() end,
        Class = T.Class, IsNotConsole = function() return false end },
        { __index = function(_, key) if G[key] ~= nil then return G[key] end; return _G[key] end })
    G.TheInputProxy = { GetInputDeviceCount = function() return 0 end }
    T.native("events", env)
    local function native(path) return T.native(path, env) end
    local NativeInput, NativeEquip = native("input.lua"), native("widgets/equipslot.lua")
    env.TheInput = G.TheInput
    local raw
    G.TheInput.AddMouseButtonHandler = function(_, fn) raw = fn; return { Remove = function() raw = nil end } end
    G.TheInput.onmousebutton = { HandleEvent = function(_, _, ...) if raw then raw(...) end end }
    G.TheInput.mouse_enabled = true
    ctx.hud.OnMouseButton = function(_, ...) return not state.offhud and slot:OnMouseButton(...) end
    G.TheFrontEnd.OnMouseButton = function(_, ...) state.consumed = ctx.hud:OnMouseButton(...) end
    local function mouse(button, down)
        state.now = state.now + .01
        NativeInput.OnMouseButton(G.TheInput, button, down, 100, 200)
        return state.consumed
    end
    Policy.attach(ctx)
    Input.attach(ctx)

    assert(not slot:OnControl(G.CONTROL_SCROLLBACK, true) and state.requests == 0,
        "mapped control arriving before raw input cannot submit a duplicate gesture")
    assert(mouse(G.MOUSEBUTTON_SCROLLUP, true) and state.method == "scroll" and state.args[3] == 1 and state.args[4] == 1)
    assert(ctx.executor.current and state.cancelled == 0 and ctx.pointer_event == nil,
        "native FrontEnd then raw broadcast preserves exactly the accepted inventory gesture")
    assert(slot:OnControl(G.CONTROL_SCROLLBACK, true) and state.requests == 1,
        "mapped control arriving after raw input does not submit again")
    assert(mouse(G.MOUSEBUTTON_SCROLLUP, false) and state.requests == 1)
    state.keys[G.CONTROL_FORCE_STACK], ctx.config.inventory_reverse = true, true
    assert(mouse(G.MOUSEBUTTON_SCROLLUP, true) and state.args[3] == -1 and state.args[4] == 5)
    state.keys[G.CONTROL_ZOOM_IN] = true
    ctx.player.components.playercontroller:DoCameraControl()
    assert(state.camera == 0, "physical camera polling cannot duplicate a consumed wheel action")
    state.now = 1
    ctx.player.components.playercontroller:DoCameraControl()
    assert(state.camera == 1, "camera is restored after the input pulse")
    assert(mouse(G.MOUSEBUTTON_MIDDLE, true) and state.method == "transfer" and state.args[3] == 5)
    assert(mouse(G.MOUSEBUTTON_MIDDLE, false) and ctx.executor.current)
    state.keys[G.CONTROL_FORCE_TRADE] = true
    assert(mouse(G.MOUSEBUTTON_MIDDLE, true) and state.method == "collect")
    state.accepted = false
    assert(not mouse(G.MOUSEBUTTON_SCROLLDOWN, true) and state.message == "rejected", "unaccepted input is reported and falls through")
    assert(not ctx.executor.current and state.cancelled == 1, "a rejected gesture receives no blanket inventory exemption")
    assert(not mouse(G.MOUSEBUTTON_MIDDLE, true))
    slot:OnControl(G.CONTROL_ACCEPT, true)
    assert(state.cancelled == 2, "manual slot clicks cancel before native handling")

    local item = { replica = {} }
    local inv = { GetActiveItem = function() return state.active end, GetNumSlots = function() return 15 end }
    function inv:TakeActiveItemFromEquipSlot(value)
        assert(ctx.executor.current == nil and value == "hands", "native equipment pickup runs after takeover")
        state.equipped = (state.equipped or 0) + 1
    end
    function inv:UseItemFromInvTile(value)
        assert(ctx.executor.current == nil and value == item, "native unequip/use runs after takeover")
        state.used = (state.used or 0) + 1
    end
    ctx.player.replica.inventory = inv
    local equip = setmetatable({ owner = ctx.player, equipslot = "hands", tile = { item = item, UpdateTooltip = function() end } }, NativeEquip)
    callbacks["widgets/equipslot"](equip)
    state.keys[G.CONTROL_FORCE_TRADE] = false
    T.each("equipment_takeover", { { name = "pickup", control = G.CONTROL_ACCEPT },
        { name = "use", control = G.CONTROL_SECONDARY } }, function(action)
        ctx.executor.current = { input = { mode = "inventory" } }
        assert(equip:OnControl(action.control, true))
    end)
    assert(state.equipped == 1 and state.used == 1, "actual native equipment methods preserve their actions")

    state.accepted, state.active, state.keys[G.CONTROL_FORCE_STACK] = true, item, false
    assert(mouse(G.MOUSEBUTTON_MIDDLE, true) and state.method == "return_active")
    assert(mouse(G.MOUSEBUTTON_MIDDLE, false) and ctx.executor.current)
    state.keys[G.CONTROL_FORCE_STACK] = true
    assert(mouse(G.MOUSEBUTTON_MIDDLE, true) and state.method == "transfer", "an explicit partial-transfer modifier keeps its meaning")
    state.keys[G.CONTROL_FORCE_TRADE] = true
    assert(mouse(G.MOUSEBUTTON_MIDDLE, true) and state.method == "collect", "an explicit collection modifier keeps its meaning")
    state.keys[G.CONTROL_FORCE_STACK], state.keys[G.CONTROL_FORCE_TRADE], state.offhud = false, false, true
    assert(mouse(G.MOUSEBUTTON_MIDDLE, true) and state.method == "return_active", "cursor return also works over the world")
    assert(mouse(G.MOUSEBUTTON_MIDDLE, false) and ctx.executor.current and not ctx.pointer_event)
    state.offhud, state.active = false, nil

    state.accepted, state.gamepad = true, true
    state.now = 2
    assert(not slot:OnControl(G.CONTROL_SCROLLFWD, true), "controller shoulders retain native inventory navigation")
    assert(not mouse(G.MOUSEBUTTON_SCROLLDOWN, true))
    state.gamepad, state.screen = false, {}
    assert(not mouse(G.MOUSEBUTTON_SCROLLDOWN, true), "other screens cannot issue inventory shortcuts")
    state.screen = nil
    ctx.scope:close()
    assert(not mouse(G.MOUSEBUTTON_SCROLLDOWN, true), "retained widgets become inert after player teardown")
    ctx.player.components.playercontroller:DoCameraControl()
    assert(state.camera == 2)
end)

T.done()
