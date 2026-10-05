local Scope = require("fieldkit/core/scope")
local M = {}
local owners = setmetatable({}, { __mode = "k" })

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

local function visibility(renderer, visible)
    if renderer:IsVisible() ~= visible then renderer:ToggleVisibility() end
end

function M.new(ctx)
    local G = ctx.G
    local renderer = G.TheWorld.minimap and G.TheWorld.minimap.MiniMap
    if ctx.config.minimap == false or not renderer or G.GetGameModeProperty("no_minimap") then return nil end
    local previous = owners[renderer]
    local edited = {}
    local self = {
        renderer = renderer, open = true,
        level = 5,
        fullscreen_level = previous and previous.fullscreen_level or renderer:GetZoom(),
        offset_x = 0, offset_y = 0,
        screen = G.TheFrontEnd:GetOpenScreenOfType("MapScreen"),
    }
    owners[renderer] = self

    function self:active()
        local controller = ctx.player.components.playercontroller
        return not ctx.scope.closed and owners[renderer] == self and not self.screen
            and G.ThePlayer == ctx.player and ctx.player.HUD == ctx.hud
            and ctx.hud.shown == true and controller ~= nil and controller:IsMapControlsEnabled() == true
            and G.TheFrontEnd:GetActiveScreen() == ctx.hud
    end

    function self:refresh()
        if owners[renderer] ~= self then return end
        local active = self:active()
        if self.widget then self.widget:render(active, self.open) end
        if not self.screen then visibility(renderer, active and self.open) end
    end

    local function restore()
        renderer:Zoom(self.level - renderer:GetZoom())
        renderer:ResetOffset()
        renderer:Offset(self.offset_x, self.offset_y)
        self:refresh()
    end

    local function save()
        if not ctx.store or not ctx.store.ready then return end
        local saved = ctx.store:get("minimap")
        if type(saved) == "table" and saved.open == self.open and saved.zoom == self.level then return end
        ctx.store:set("minimap", { open = self.open, zoom = self.level })
    end

    function self:attach(widget)
        self.widget = widget
        if not self.screen then restore() else self:refresh() end
    end

    function self:begin_map(screen)
        if owners[renderer] ~= self or self.screen == screen then return end
        self.screen = screen
        if self.widget then self.widget:render(false, self.open) end
        screen:SetZoom(self.fullscreen_level)
    end

    function self:end_map(screen)
        if owners[renderer] ~= self or self.screen ~= screen then return end
        self.fullscreen_level = renderer:GetZoom()
        self.screen = nil
        restore()
    end

    function self:set_open(open)
        if ctx.scope.closed or owners[renderer] ~= self then return end
        edited.open = true
        self.open = open == true
        self:refresh()
        save()
    end

    function self:zoom(delta)
        if not self:active() or not self.open or not finite(delta) then return end
        edited.zoom = true
        self.level = math.max(1, math.min(20, self.level + delta))
        renderer:Zoom(self.level - renderer:GetZoom())
        save()
    end

    function self:pan(dx, dy)
        if not self:active() or not self.open or not finite(dx) or not finite(dy) then return end
        self.offset_x, self.offset_y = self.offset_x + dx, self.offset_y + dy
        renderer:Offset(dx, dy)
    end

    function self:recenter()
        self.offset_x, self.offset_y = 0, 0
        if owners[renderer] == self and not self.screen then renderer:ResetOffset() end
    end

    ctx.scope:defer(function()
        if owners[renderer] ~= self then return end
        owners[renderer] = nil
        if not self.screen then
            renderer:Zoom(self.fullscreen_level - renderer:GetZoom())
            renderer:ResetOffset()
            visibility(renderer, false)
        end
    end)
    if ctx.store then
        ctx.scope:defer(ctx.store:on_ready(function()
            if ctx.scope.closed or owners[renderer] ~= self then return end
            local saved = ctx.store:get("minimap")
            saved = type(saved) == "table" and saved or {}
            if not edited.open then self.open = saved.open ~= false end
            if not edited.zoom and finite(saved.zoom) then self.level = math.max(1, math.min(20, saved.zoom)) end
            if self.widget then
                if not self.screen then restore() else self:refresh() end
            end
            if edited.open or edited.zoom then save() end
        end))
    end
    return self
end

function M.install(env, get_context)
    env.AddClassPostConstruct("screens/mapscreen", function(screen)
        local scope = Scope.new(screen.owner)
        local function view()
            local ctx = get_context()
            return ctx and not ctx.scope.closed and ctx.player == screen.owner and ctx.mapview or nil
        end
        local current = view()
        if current then current:begin_map(screen) end
        scope:wrap(screen, "OnBecomeActive", function(original)
            return function(self, ...)
                local current = view()
                if current then current:begin_map(self) end
                return original(self, ...)
            end
        end)
        scope:wrap(screen, "OnDestroy", function(original)
            return function(self, ...)
                local function finish(...)
                    local current = view()
                    if current then current:end_map(self) end
                    scope:close()
                    return ...
                end
                return finish(original(self, ...))
            end
        end)
    end)
end

return M
