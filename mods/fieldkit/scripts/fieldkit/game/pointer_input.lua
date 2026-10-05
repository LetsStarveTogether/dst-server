local Input = require("fieldkit/game/input")
local AnnounceInput = require("fieldkit/game/announce_input")
local RangeInput = require("fieldkit/game/range_input")
local M = {}

function M.attach(ctx)
    local G, owned = ctx.G, {}
    local announce, ranges = AnnounceInput.new(ctx), RangeInput.new(ctx)
    local buttons = { G.MOUSEBUTTON_LEFT, G.MOUSEBUTTON_MIDDLE }
    local function mapped(control, button)
        local device, count, a, b, c, d = G.TheInputProxy:GetLocalizedControl(0, control, false, true)
        return device == 1 and count > 0 and (a == button or b == button or c == button or d == button)
    end
    local function handle(button, source, down)
        local gesture = owned[button]
        if down and gesture and (gesture[source] == "up" or gesture.raw == "up") then
            owned[button], gesture = nil, nil
        end
        if gesture then gesture[source] = down and "down" or "up"; return true end
        if not down then return false end
        if announce:press(button) or ranges:press(button) or ctx.travel:handle_pointer(button) then
            owned[button] = { [source] = "down" }
            return true
        end
        return false
    end
    ctx.scope:wrap(ctx.hud, "OnMouseButton", function(original)
        return function(hud, button, down, ...)
            if (button == G.MOUSEBUTTON_LEFT or button == G.MOUSEBUTTON_MIDDLE) and handle(button, "raw", down) then
                Input.claim_pointer(ctx, button, down)
                return true
            end
            return original(hud, button, down, ...)
        end
    end)
    ctx.scope:wrap(ctx.hud, "OnControl", function(original)
        return function(hud, control, down, ...)
            local physical = control == G.CONTROL_ACCEPT and G.CONTROL_PRIMARY or control
            for _, button in ipairs(buttons) do
                local gesture = owned[button]
                local pending = gesture and (gesture[physical] == "down" or gesture.raw == "down")
                if (down and G.TheInput:IsMouseDown(button) or not down and pending)
                    -- FrontEnd aliases primary mouse controls; keyboard accept stays native.
                    and (control ~= G.CONTROL_ACCEPT or G.TheFrontEnd.isprimary)
                    and mapped(physical, button) and handle(button, physical, down) then return true end
            end
            return original(hud, control, down, ...)
        end
    end)
    ctx.scope:wrap(G.TheInput, "IsControlPressed", function(original)
        return function(input, control, ...)
            for button, gesture in pairs(owned) do
                if gesture.raw ~= "up" and gesture[control] ~= "up" and mapped(control, button) then return false end
            end
            return original(input, control, ...)
        end
    end)
    local mouse = G.TheInput:AddMouseButtonHandler(function(button, down)
        -- Input broadcasts raw release even when another screen has replaced the HUD.
        if not down and owned[button] then owned[button].raw = "up" end
    end)
    ctx.scope:defer(function() mouse:Remove(); owned = {} end)
end

return M
