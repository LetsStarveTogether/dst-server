local Ranges = require("fieldkit/rules/ranges")
local M = {}

function M.new(ctx)
    local self, G, last = {}, ctx.G
    local function target()
        local input, frontend = G.TheInput, G.TheFrontEnd
        if ctx.config.range_modifier == "off" or not ctx.config.range_hints or input:ControllerAttached()
            or frontend:GetActiveScreen() ~= ctx.hud or frontend.textProcessorWidget or ctx.hud.modfocus
            or input:GetHUDEntityUnderMouse() or not ctx:ready() then return end
        local modifier = ctx.config.range_modifier or "none"
        local key = ({ ctrl = G.KEY_CTRL, alt = G.KEY_ALT, shift = G.KEY_SHIFT })[modifier]
        if key and not input:IsKeyDown(key) then return end
        local entity = input:GetWorldEntityUnderMouse()
        return ctx.awareness:can_pin(entity) and entity or nil
    end
    function self:press(button)
        if button ~= G.MOUSEBUTTON_MIDDLE then return false end
        local entity = target()
        if not entity then last = nil; return false end
        local now, prefab = G.GetTime(), Ranges.alias(entity.prefab)
        if last and last.prefab == prefab and now - last.time <= (ctx.config.range_double_click or .3) then
            ctx.awareness:toggle_group(entity, last.show)
            last = nil
        else
            local ok, _, show = ctx.awareness:toggle_pin(entity)
            last = ok and { prefab = prefab, time = now, show = show } or nil
        end
        return true
    end
    return self
end

return M
