local Input = require("fieldkit/game/input")
local M = {}

local function returning(ctx)
    local input, G = ctx.G.TheInput, ctx.G
    local inv = ctx.player.replica.inventory
    return inv and inv:GetActiveItem() and not input:IsControlPressed(G.CONTROL_FORCE_STACK)
        and not input:IsControlPressed(G.CONTROL_FORCE_TRADE)
end

function M.install(env, get_context)
    local G = env.GLOBAL
    env.AddClassPostConstruct("widgets/invslot", function(slot)
        local on_control, on_mouse = slot.OnControl, slot.OnMouseButton
        local pressed = {}
        local function context()
            local ctx = get_context()
            return ctx and not ctx.scope.closed and ctx.storage and slot.owner == ctx.player
                and slot.focus and G.TheFrontEnd:GetActiveScreen() == ctx.hud and ctx or nil
        end
        local function result(ctx, ok, why)
            if not ok and why then ctx:notify(why) end
            return ok == true
        end
        local function batch(ctx)
            return G.TheInput:IsControlPressed(G.CONTROL_FORCE_STACK) and ctx.params.inventory_batch or 1
        end
        slot.OnControl = function(self, control, down, ...)
            local ctx = context()
            if ctx and ctx.inventory_scroll_at == G.GetTime() and control == ctx.inventory_scroll_control
                and not G.TheInput:ControllerAttached() then
                return true
            end
            if ctx and down and (control == G.CONTROL_ACCEPT or control == G.CONTROL_SECONDARY
                or control == G.CONTROL_SPLITSTACK or control == G.CONTROL_TRADEITEM or control == G.CONTROL_TRADESTACK) then
                ctx.executor:cancel("manual")
            end
            return on_control(self, control, down, ...)
        end
        slot.OnMouseButton = function(self, button, down, ...)
            local ctx = context()
            local direction = button == G.MOUSEBUTTON_SCROLLUP and 1 or button == G.MOUSEBUTTON_SCROLLDOWN and -1
            local wheel = ctx and direction and ctx.config.inventory_scroll and not G.TheInput:ControllerAttached()
            if ctx and not G.TheInput:ControllerAttached() and (wheel or ctx.config.inventory_middle and button == G.MOUSEBUTTON_MIDDLE) then
                if not down and pressed[button] then
                    pressed[button] = nil
                    Input.claim_pointer(ctx, button, down)
                    return true
                end
                if down then
                    pressed[button] = nil
                    local ok, why
                    if wheel then
                        if ctx.config.inventory_reverse then direction = -direction end
                        ok, why = ctx.storage:scroll(self.container, self.num, direction, batch(ctx))
                    elseif G.TheInput:IsControlPressed(G.CONTROL_FORCE_TRADE) then ok, why = ctx.storage:collect(self.container, self.num)
                    elseif returning(ctx) then ok, why = ctx.storage:return_active()
                    else
                        local count = G.TheInput:IsControlPressed(G.CONTROL_FORCE_STACK) and batch(ctx) or nil
                        ok, why = ctx.storage:transfer(self.container, self.num, count)
                    end
                    if result(ctx, ok, why) then
                        pressed[button] = true
                        Input.claim_pointer(ctx, button, down)
                        if wheel then
                            ctx.inventory_scroll_at = G.GetTime()
                            ctx.inventory_scroll_control = button == G.MOUSEBUTTON_SCROLLUP and G.CONTROL_SCROLLBACK or G.CONTROL_SCROLLFWD
                        end
                        return true
                    end
                end
            end
            return on_mouse(self, button, down, ...)
        end
    end)
    env.AddClassPostConstruct("widgets/equipslot", function(slot)
        local original = slot.OnControl
        slot.OnControl = function(self, control, down, ...)
            local ctx = get_context()
            if ctx and not ctx.scope.closed and self.owner == ctx.player and down
                and (control == G.CONTROL_ACCEPT or control == G.CONTROL_SECONDARY) then
                ctx.executor:cancel("manual")
            end
            return original(self, control, down, ...)
        end
    end)
end

function M.attach(ctx)
    local G = ctx.G
    local pressed = false
    ctx.scope:wrap(ctx.hud, "OnMouseButton", function(original)
        return function(hud, button, down, ...)
            if button == G.MOUSEBUTTON_MIDDLE and not down and pressed then
                pressed = false
                Input.claim_pointer(ctx, button, down)
                return true
            end
            if original(hud, button, down, ...) then return true end
            if button ~= G.MOUSEBUTTON_MIDDLE or not down or not ctx.config.inventory_middle
                or G.TheInput:ControllerAttached() or G.TheFrontEnd:GetActiveScreen() ~= ctx.hud
                or G.TheInput:GetHUDEntityUnderMouse()
                or not returning(ctx) then return end
            local ok, why = ctx.storage:return_active()
            if not ok and why then ctx:notify(why) end
            if ok then pressed = true; Input.claim_pointer(ctx, button, down); return true end
        end
    end)
    ctx.scope:wrap(ctx.player.components.playercontroller, "DoCameraControl", function(original)
        return function(controller, ...)
            -- Camera zoom polls physical controls even after a widget consumes OnControl.
            if ctx.inventory_scroll_at and G.GetTime() - ctx.inventory_scroll_at < .2
                and G.TheInput:GetHUDEntityUnderMouse()
                and (G.TheInput:IsControlPressed(G.CONTROL_ZOOM_IN) or G.TheInput:IsControlPressed(G.CONTROL_ZOOM_OUT)) then return end
            return original(controller, ...)
        end
    end)
end

return M
