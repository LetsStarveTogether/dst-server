-- MapScreen owns projection, static-icon aiming and native map action priority.
local Scope = require("fieldkit/core/scope")
local M = {}
local contexts = setmetatable({}, { __mode = "k" })

local function state_for(ctx)
    if contexts[ctx] then return contexts[ctx] end
    local state = {}
    contexts[ctx] = state
    ctx.scope:defer(function()
        if state.screen_scope then state.screen_scope:close() end
        contexts[ctx] = nil
    end)
    return state
end

-- Only the map editing surface may retain a paused route; overlays still relinquish input.
function M.editing(ctx, job)
    if not ctx.travel or not ctx.travel:is_editing(job) then return false end
    local frontend, mini = ctx.G.TheFrontEnd, ctx.minimap
    if frontend.textProcessorWidget ~= nil then return false end
    local active = frontend:GetActiveScreen()
    if mini and ctx.travel:is_editing(job, mini) and (mini.focused or mini.drag)
        and active == ctx.hud and ctx.mapview and ctx.mapview:active() then
        for name, identifiers in pairs(ctx.hud.modfocus or {}) do
            if name ~= "fieldkit" then return false end
            for identifier in pairs(identifiers) do if identifier ~= "minimap" then return false end end
        end
        return true
    end
    local state = contexts[ctx]
    return state ~= nil and ctx.travel:is_editing(job, state) and ctx.hud.modfocus == nil
        and (active == state.screen and state.screen ~= nil or state.opening and active == ctx.hud)
end

function M.attach(ctx)
    local state, G = state_for(ctx), ctx.G
    ctx.scope:wrap(G.TheFrontEnd, "PushScreen", function(original)
        return function(frontend, screen, ...)
            -- A paused simulation may never tick before an overlay closes again.
            if ctx.travel:is_editing(ctx.executor.current) and screen ~= state.screen then
                ctx.executor:cancel("focus")
            end
            return original(frontend, screen, ...)
        end
    end)
    for _, method in ipairs({ "ShowMap", "ToggleMap" }) do
        ctx.scope:wrap(ctx.hud.controls, method, function(original)
            return function(controls, ...)
                if ctx.hud:IsMapScreenOpen() or G.GetGameModeProperty("no_minimap")
                    or G.TheNet:GetServerGameMode() == "quagmire" then return original(controls, ...) end
                -- Native constructors autopause before the new screen reaches PushScreen.
                state.opening = true
                ctx.travel:pause_route(state)
                local function finish(ok, ...)
                    state.opening = false
                    if not state.screen then ctx.travel:resume_route(state) end
                    if not ok then error((...), 0) end
                    return ...
                end
                return finish(pcall(original, controls, ...))
            end
        end)
    end
end

function M.request(ctx, point, append, screen, control)
    if ctx.scope.closed or ctx.config.travel == false then return false end
    if append then return ctx.travel:add_waypoint(point) end
    if not screen then return ctx.travel:navigate(point, false) end
    screen.quitting = true
    ctx.G.SetAutopaused(false)
    ctx.G.TheFrontEnd:PopScreen(screen)
    local controller = ctx.player.components.playercontroller
    controller._hack_ignore_held_controls = 0.1
    controller._hack_ignore_ups_for = controller._hack_ignore_ups_for or {}
    if control then controller._hack_ignore_ups_for[control] = true end
    -- PopScreen restores HUD focus synchronously; no delayed callback can restart cancelled work.
    return ctx.travel:navigate(point, false)
end

function M.install(env, get_context)
    local G = env.GLOBAL
    env.AddClassPostConstruct("screens/mapscreen", function(screen)
        local ctx = get_context()
        if not ctx or ctx.scope.closed or screen.owner ~= ctx.player then return end
        local state = state_for(ctx)
        if state.screen_scope then state.screen_scope:close() end
        local scope = Scope.new(ctx.player, function(err) ctx:report(err) end)
        state.screen_scope, state.screen = scope, screen
        ctx.travel:pause_route(state)
        local overlay = require("fieldkit/ui/map_overlay").new(ctx, screen.minimap, screen.minimap, function()
            local width, height = G.TheSim:GetScreenSize()
            return width, height, width, height
        end)
        overlay.root:SetHAnchor(G.ANCHOR_MIDDLE)
        overlay.root:SetVAnchor(G.ANCHOR_MIDDLE)
        scope:defer(function() overlay:close() end)
        local consumed = {}
        scope:wrap(screen, "OnBecomeInactive", function(original)
            return function(self, ...)
                -- Retain release ownership, but never execute a press from before focus loss.
                for _, intent in pairs(consumed) do intent.generation = nil end
                overlay:update(0, false)
                return original(self, ...)
            end
        end)
        scope:wrap(screen, "OnBecomeActive", function(original)
            return function(self, ...)
                local result = original(self, ...)
                overlay:update(.1, self.shown)
                return result
            end
        end)
        scope:wrap(screen, "OnUpdate", function(original)
            return function(self, dt, ...)
                local result = original(self, dt, ...)
                overlay:update(dt, self.shown and G.TheFrontEnd:GetActiveScreen() == self)
                return result
            end
        end)
        scope:defer(function()
            if state.screen == screen then
                state.screen_scope, state.screen = nil, nil
                ctx.travel:resume_route(state)
            end
        end)
        scope:wrap(screen, "OnDestroy", function(original)
            return function(self, ...)
                overlay:close()
                local function finish(...)
                    scope:close()
                    return ...
                end
                return finish(original(self, ...))
            end
        end)
        scope:wrap(screen, "OnControl", function(original)
            return function(self, control, down, ...)
                if down then consumed[control] = nil end
                if not down and consumed[control] then
                    local intent = consumed[control]
                    consumed[control] = nil
                    if intent.generation == ctx.executor.generation then
                        local _, reason = M.request(ctx, intent.point, intent.append, self, control)
                        if reason then ctx:notify(reason) end
                    end
                    return true
                end
                local modifier = ctx.config.map_modifier
                local gamepad = G.TheInput:ControllerAttached()
                local navigate_control = control == G.CONTROL_SECONDARY
                    or gamepad and control == G.CONTROL_MENU_MISC_3
                local append_control = gamepad and control == G.CONTROL_MENU_MISC_2
                local held = modifier and modifier ~= 0 and G.TheInput:IsKeyDown(modifier)
                if ctx.config.travel == false or modifier == 0
                    or not down or not self.shown or self.quitting or self._hack_ignore_held_controls
                    or not (navigate_control or append_control) or not (held or gamepad)
                    or self.maptarget ~= nil then
                    return original(self, control, down, ...)
                end
                local x, y, z = self:GetWorldPositionAtCursor()
                local ax, ay, az = self:AutoAimToStaticDecorations(x, y, z)
                local _, native_right = self:UpdateMapActions(ax, ay, az)
                if native_right then return original(self, control, down, ...) end
                local point = G.Vector3(x, 0, z)
                local append = append_control or G.TheInput:IsKeyDown(G.KEY_SHIFT)
                -- Complete on release so the same raw mouse-down cannot cancel the new HUD route.
                consumed[control] = { point = point, append = append, generation = ctx.executor.generation }
                return true
            end
        end)
        scope:wrap(screen, "GetHelpText", function(original)
            return function(self, ...)
                local text = original(self, ...)
                if ctx.config.travel == false or ctx.config.map_modifier == 0 then return text end
                local hint
                if G.TheInput:ControllerAttached() then
                    local id = G.TheInput:GetControllerID()
                    hint = G.TheInput:GetLocalizedControl(id, G.CONTROL_MENU_MISC_3) .. ctx:text(" 导航", " Navigate")
                        .. " / " .. G.TheInput:GetLocalizedControl(id, G.CONTROL_MENU_MISC_2) .. ctx:text(" 追加航点", " Add waypoint")
                else
                    hint = ctx:text("地图修饰键 + 右键：导航；+ Shift：追加航点", "Map modifier + right click: navigate; + Shift: add waypoint")
                end
                local status = ctx.travel:route_status()
                return text .. "  " .. hint .. (status ~= "" and "  " .. status or "")
            end
        end)
    end)
end

return M
