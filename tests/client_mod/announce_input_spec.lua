local T = require("tests.client_mod.support")
T.case("native announcement input", function()
    local Scope = require("fieldkit/core/scope")
    local PointerInput = require("fieldkit/game/pointer_input")
    local function fixture()
        local state = { keys = {}, sent = {}, native = 0, mouse = {}, accepted = true }
        local G = { KEY_ALT = 1, KEY_SHIFT = 2, KEY_CTRL = 3, CONTROL_ACCEPT = 4, CONTROL_PRIMARY = 5,
            MOUSEBUTTON_LEFT = 6, MOUSEBUTTON_MIDDLE = 10, CONTROL_FORCE_INSPECT = 7, CONTROL_FORCE_TRADE = 8, CONTROL_FORCE_STACK = 9 }
        local controller = { IsControlPressed = function(_, control) return G.TheInput:IsControlPressed(control) end }
        local player = { components = { playercontroller = controller }, replica = {} }
        local status = { heart = {}, brain = {}, stomach = {}, moisturemeter = {} }
        local craft = { pinbar = {} }
        local hud = { controls = { status = status, craftingmenu = craft, clock = {} } }
        G.TheFrontEnd = { GetActiveScreen = function() return state.screen or hud end }
        G.TheInput = {
            IsKeyDown = function(_, key) return state.keys[key] end,
            IsControlPressed = function() return true end,
            IsMouseDown = function(_, button) return button == G.MOUSEBUTTON_LEFT and state.held end,
            ResolveVirtualControls = function(_, control) return control end,
            ControllerAttached = function() return state.gamepad end,
            GetHUDEntityUnderMouse = function() return state.widget and { widget = state.widget } end,
            GetWorldEntityUnderMouse = function() return state.target end,
            AddMouseButtonHandler = function(_, fn)
                local handler = { fn = fn, Remove = function(self) self.removed = true end }
                state.mouse[#state.mouse + 1] = handler
                return handler
            end,
        }
        G.TheInputProxy = { GetLocalizedControl = function(_, _, control)
            if control == G.CONTROL_PRIMARY then return 1, 1, G.MOUSEBUTTON_LEFT end
            return 0, 0
        end }
        local ctx = { G = G, player = player, hud = hud, scope = Scope.new(player, error),
            config = { announcements = true }, notify = function(_, value) state.notice = value end }
        ctx.announcements = setmetatable({}, { __index = function(_, method)
            return function(_, argument)
                state.sent[#state.sent + 1] = { method = method, argument = argument }
                return state.accepted, "Not available"
            end
        end })
        local base = { OnControl = function() return false end }
        local env = setmetatable({ require = function() return base end, ValidateLineNumber = function() end,
            Class = T.Class,
        }, { __index = function(_, key) return G[key] ~= nil and G[key] or _G[key] end })
        env.IsNotConsole = function() return false end
        env.TheInputProxy = env.TheInputProxy or {}
        env.TheInputProxy.GetInputDeviceCount = function() return 0 end
        T.native("events", env)
        local function native(path) return T.native(path, env) end
        local NativeInput, InvSlot = native("input"), native("widgets/invslot")
        local EquipSlot, ImageButton = native("widgets/equipslot"), native("widgets/imagebutton")
        env.TheInput = G.TheInput
        local sample = { replica = {} }
        local container = { GetItemInSlot = function(_, slot) assert(slot == 2); return sample end }
        local invslot = setmetatable({ owner = player, container = container, num = 2,
            Inspect = function() state.inspected = true end }, InvSlot)
        player.replica.inventory = { GetEquippedItem = function() return sample end,
            GetActiveItem = function() end, GetNumSlots = function() return 15 end,
            TakeActiveItemFromEquipSlot = function() state.unequipped = true end }
        function state:equip()
            self.widget = setmetatable({ owner = player, equipslot = "hands",
                tile = { item = sample, UpdateTooltip = function() end } }, EquipSlot)
            self.slot = self.widget
        end
        function state:craft()
            self.widget = setmetatable({ focus = true, control = G.CONTROL_ACCEPT, stopclicksound = true,
                IsEnabled = function() return true end, IsSelected = function() return false end,
                GetLocalPosition = function() return {} end, ResetPreClickPosition = function() end,
                StopUpdating = function() end, onclick = function() self.crafted = true end,
                parent = { data = { recipe = { name = "campfire" } }, parent = craft } }, ImageButton)
            self.slot = self.widget
        end
        function state:chip()
            env.FRAMES = 1 / 30
            env.Vector3 = function(x, y, z) return { x = x, y = y, z = z } end
            env.SOCKETNAMES, env.SOCKETQUALITY = { SHADOW = 1 }, { NONE = 0, LOW = 1, MEDIUM = 2, HIGH = 3, PERFECT = 4 }
            local native_panel = native("widgets/upgrademodulesdisplay_inspecting")
            local oncontrol
            for index = 1, 100 do
                local name, value = debug.getupvalue(native_panel._ctor, index)
                if not name then break end
                if name == "Chip_OnControl" then oncontrol = value end
            end
            assert(oncontrol, "load the actual native chip input handler")
            local panel = { chip_objectpools = { [0] = {} }, IsBusy = function() return self.busy end,
                UnplugModule = function() self.unplugged = true end }
            hud.upgrademodulewidget = panel
            self.widget = { parent = panel, owner = player, moduletype = 0, chip_index = 1,
                focus = true, _base = base, OnControl = oncontrol, _net_id = 42 }
            panel.chip_objectpools[0][1] = self.widget
            self.slot = self.widget
        end
        function hud:OnControl(control, down)
            state.native = state.native + 1
            return state.slot and state.slot:OnControl(control, down) or false
        end
        function hud:OnMouseButton() state.native = state.native + 1; return false end
        G.TheFrontEnd.OnControl = function(self, control, down)
            self.isprimary = control == G.CONTROL_PRIMARY
            local result = (state.screen or hud):OnControl(self.isprimary and G.CONTROL_ACCEPT or control, down)
            self.isprimary = false
            return result
        end
        G.TheFrontEnd.OnMouseButton = function(_, ...)
            return (state.screen or hud):OnMouseButton(...)
        end
        local dispatch = { mouse_enabled = true,
            oncontrol = { HandleEvent = function() end },
            onmousebutton = { HandleEvent = function(_, _, button, down)
                for _, listener in ipairs(state.mouse) do if not listener.removed then listener.fn(button, down) end end
            end },
        }
        function state:control(down) self.held = down; NativeInput.OnControl(dispatch, G.CONTROL_PRIMARY, down) end
        function state:raw(down) self.held = down; NativeInput.OnMouseButton(dispatch, G.MOUSEBUTTON_LEFT, down, 0, 0) end
        function state:click(order)
            if type(order) ~= "table" then order = { down = order, up = order } end
            local function raw(down) self:raw(down) end
            local function mapped(down) self:control(down) end
            T.pointer(raw, mapped, true, order.down)
            assert(not controller:IsControlPressed(G.CONTROL_PRIMARY))
            T.pointer(raw, mapped, false, order.up)
            assert(controller:IsControlPressed(G.CONTROL_PRIMARY))
        end
        ctx.travel = { handle_pointer = function() return false end }
        PointerInput.attach(ctx)
        return ctx, state, invslot, sample
    end

    T.each("announcement gesture", T.pointer_orders, function(order)
        local ctx, state, slot, sample = fixture()
        local G = ctx.G
        state.keys[G.KEY_ALT] = true
        state.slot, state.widget = slot, { parent = slot }
        state:click(order)
        assert(#state.sent == 1 and state.sent[1].method == "item" and state.sent[1].argument == sample)
        assert(state.native == 0 and ctx.pointer_event.button == G.MOUSEBUTTON_LEFT, "accepted input never inspects or moves inventory")
        state.widget = { parent = ctx.hud.controls.status.heart }
        state:click(order)
        assert(#state.sent == 2 and state.sent[2].argument == "health", "status identity is independent of displayed text")
        state.widget = { parent = { data = { recipe = { name = "axe" } }, parent = ctx.hud.controls.craftingmenu } }
        state:click(order)
        assert(#state.sent == 3 and state.sent[3].method == "recipe" and state.sent[3].argument.name == "axe")
        state.widget = { parent = { recipe_name = "campfire", parent = ctx.hud.controls.craftingmenu.pinbar } }
        state:click(order)
        assert(#state.sent == 4 and state.sent[4].argument == "campfire", "pinned recipes use native recipe_name")
        state.widget = { fieldkit_announcement = "temperature" }
        state:click(order)
        assert(state.sent[5].method == "metric" and state.sent[5].argument == "temperature")
        state.widget = nil
        state.keys[G.KEY_SHIFT], state.target = true, sample
        state:click(order)
        assert(state.sent[6].method == "target" and state.sent[6].argument == sample)
        state.keys[G.KEY_ALT] = false
        state:control(true); state:raw(true); state:control(false); state:raw(false)
        assert(#state.sent == 6 and state.native > 0, "unmodified input reaches original methods")
        ctx.scope:close()
        assert(state.mouse[1].removed and rawget(ctx.player.components.playercontroller, "IsControlPressed"))
    end)

    do
        local ctx, state, slot = fixture()
        state.widget, state.slot, state.accepted = slot, slot, false
        state.keys[ctx.G.KEY_ALT] = true
        state:click(false)
        assert(#state.sent == 1 and not state.inspected and state.native == 0,
            "cooldown/rejection checks once and owns the complete announcement gesture")
        state.accepted = true
        state:control(true)
        assert(#state.sent == 2)
        state.screen = { OnControl = function() return true end, OnMouseButton = function() return true end }
        state:control(false); state:raw(false)
        state.screen, state.keys[ctx.G.KEY_ALT] = nil, false
        state:control(true)
        assert(#state.sent == 2 and state.native == 1, "raw release prevents a screen change from swallowing the next ordinary click")
        state:control(false); state:raw(false)
        ctx.G.TheFrontEnd.textProcessorWidget = {}
        state.keys[ctx.G.KEY_ALT] = true
        state:control(true)
        assert(#state.sent == 2, "text input owns its click")
        ctx.scope:close()
    end
    T.each("rejected native side effects", { "equip", "craft" }, function(kind)
        local ctx, state = fixture()
        state[kind](state)
        state.accepted, state.keys[ctx.G.KEY_ALT] = false, true
        state:click(true)
        assert(#state.sent == 1 and state.native == 0 and not state.unequipped and not state.crafted,
            "a rejected announcement cannot fall through to native equipment or crafting effects")
        state.keys[ctx.G.KEY_ALT] = false
        state:control(true); state:raw(true); state:control(false); state:raw(false)
        assert(state.unequipped or state.crafted, "the next unmodified native gesture still acts normally")
        ctx.scope:close()
    end)
    T.each("information row intents", { "moon", "world_temperature", "naughtiness" }, function(field)
        local ctx, state = fixture()
        state.keys[ctx.G.KEY_ALT] = true
        state.widget = { parent = { fieldkit_announcement = field } }
        state:click(true)
        assert(#state.sent == 1 and state.sent[1].method == field and state.native == 0,
            "status rows own both native input aliases and dispatch their actual information kind")
        ctx.scope:close()
    end)
    do
        local ctx, state = fixture()
        state.keys[ctx.G.KEY_ALT] = true
        ctx.player.prefab = "wanda"
        state.widget = { parent = ctx.hud.controls.status.heart }
        state:click(false)
        assert(state.sent[1].method == "metric" and state.sent[1].argument == "age",
            "native Wanda heart announces age through shared metric identity")
        ctx.hud.controls.secondary_status = { upgrademodulesdisplay = {} }
        state.widget = { parent = ctx.hud.controls.secondary_status.upgrademodulesdisplay }
        state:click(true)
        assert(state.sent[2].argument == "energy", "secondary character widgets share metric intent")
        ctx.scope:close()
    end
    do
        local ctx, state = fixture()
        state:craft()
        state.widget.owner = ctx.player
        state.widget.fieldkit_ingredient = { recipe = "campfire", category = "item", index = 1 }
        state.keys[ctx.G.KEY_ALT], state.accepted = true, false
        state:click(false)
        assert(state.sent[1].method == "ingredient" and state.sent[1].argument.recipe == "campfire" and not state.crafted,
            "a nested material uses its parent recipe cost, and rejection cannot accidentally craft it")
        ctx.scope:close()
    end
    do
        local ctx, state = fixture()
        state:chip()
        state.keys[ctx.G.KEY_ALT], state.busy, state.accepted = true, true, false
        state:click(true)
        assert(state.sent[1].method == "circuit" and state.sent[1].argument.netid == nil and not state.unplugged,
            "native moving-chip indices do not identify modules until its busy animation ends")
        state.busy, state.accepted = false, true
        state:click(false)
        assert(state.sent[2].argument.netid == 42 and not state.unplugged)
        state.keys[ctx.G.KEY_ALT] = false
        state:control(true); state:raw(true); state:control(false); state:raw(false)
        assert(state.unplugged, "unmodified native chip control still unplugs")
        ctx.scope:close()
    end
end)
T.done()
