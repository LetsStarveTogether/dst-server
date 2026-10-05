local Scope = require("fieldkit/core/scope")

local M = {}
local IDENTITY = "images/colour_cubes/identity_colourcube.tex"
local SNOW = "levels/textures/snow.tex"
local installations = setmetatable({}, { __mode = "k" })
local unpack = unpack

local function pack(...) return { n = select("#", ...), ... } end
local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

-- Engine components are userdata; Lua widgets/components own their instance methods.
local function methods(object)
    if type(object) == "table" then return object end
    local meta = object and getmetatable(object)
    return meta and type(meta.__index) == "table" and meta.__index or nil
end

local function active(state)
    local self = state.active
    return self and not self.scope.closed and state.G.TheWorld == self.world
        and state.G.ThePlayer == self.ctx.player and self.ctx.player:IsValid()
        and self.ctx.player.HUD == self.ctx.hud and self or nil
end

local function selected(mode, index)
    return (mode == "neutral_all" and (index == 0 or index == 1 or index == 2))
        or (mode == "neutral_effects" and (index == 1 or index == 2))
end

local function replay(state, fn)
    -- Calling the current entry point keeps wrappers installed by later mods in the chain.
    -- Replays must not replace the latest upstream request with our transformed values.
    state.replaying = true
    local ok, err = pcall(fn)
    state.replaying = false
    if not ok then error(err) end
end

local function observe_luts(state)
    local pp = state.G.PostProcessor
    local owner = methods(pp)
    if pp == state.pp or not owner or not pp.SetColourCubeData then return end
    state.pp = pp
    local original = pp.SetColourCubeData
    owner.SetColourCubeData = function(object, index, source, destination, ...)
        if object == state.pp then
            if not state.replaying then
                if state.lut_world ~= state.G.TheWorld then
                    state.lut_world, state.luts = state.G.TheWorld, {}
                end
                if index == 0 or index == 1 or index == 2 then
                    state.luts[index] = pack(index, source, destination, ...)
                end
            end
            local self = active(state)
            if self and selected(self.ctx.config.colour_filter, index) then
                return original(object, index, IDENTITY, IDENTITY, ...)
            end
        end
        return original(object, index, source, destination, ...)
    end
end

local function observe_map(state, world)
    local map = world and world.Map
    local owner = methods(map)
    if not owner or not map.SetOverlayTexture or not map.SetOverlayLerp then return end
    if state.map ~= map then
        state.map, state.map_world, state.texture, state.level = map, world, nil, nil
    end
    if state.map_owners[owner] then return end
    state.map_owners[owner] = true
    local texture, lerp = map.SetOverlayTexture, map.SetOverlayLerp
    owner.SetOverlayTexture = function(object, name, ...)
        if object == state.map then state.texture = name end
        return texture(object, name, ...)
    end
    owner.SetOverlayLerp = function(object, level, ...)
        if object == state.map then
            if not state.replaying then state.level = pack(level, ...) end
            local self = active(state)
            local limit = self and self.ctx.config.snow_overlay
            -- Weather uses this same API for puddles. Only the observed snow texture qualifies.
            if self and state.map_world == self.world and state.texture == SNOW
                and finite(level) and finite(limit) and limit >= 0 and limit < 1 then
                return lerp(object, math.min(level, 3 * limit), ...)
            end
        end
        return lerp(object, level, ...)
    end
end

function M.install(env)
    local G = env.GLOBAL
    local state = installations[G]
    if state then return state end
    state = { G = G, luts = {}, map_owners = setmetatable({}, { __mode = "k" }) }
    installations[G] = state
    -- main.lua creates PostProcessor after loading modmain.
    if env.AddGamePostInit then env.AddGamePostInit(function() observe_luts(state) end) end
    if env.AddComponentPostInit then
        -- MakeWorld creates worldstate before colourcube/weather: capture their first writes,
        -- even when a controller popup delays GamePostInit until after world loading.
        env.AddComponentPostInit("worldstate", function(_, inst)
            if inst == G.TheWorld then
                observe_luts(state)
                observe_map(state, inst)
            end
        end)
    end
    observe_luts(state)
    if G.TheWorld then observe_map(state, G.TheWorld) end
    return state
end

local function replay_world(state, self)
    if state.G.TheWorld ~= self.world or not self.world:IsValid() then return end
    replay(state, function()
        if state.lut_world == self.world and state.pp then
            for index = 0, 2 do
                local value = state.luts[index]
                if value then state.pp:SetColourCubeData(unpack(value, 1, value.n)) end
            end
        end
        if state.map_world == self.world and state.level then
            state.map:SetOverlayLerp(unpack(state.level, 1, state.level.n))
        end
    end)
end

local function sand_overlay(self)
    local ctx, scope = self.ctx, self.scope
    local overlay = ctx.hud and ctx.hud.sandover
    local factor = ctx.config.sand_overlay
    if not finite(factor) or factor < 0 or factor >= 1 or not overlay
        or not overlay.ApplyLevels or not overlay.bg or not overlay.bg.GetAnimState
        or not overlay.dust or not overlay.dust.GetAnimState
        or not overlay.letterbox or not overlay.letterbox.SetMultColour then
        return
    end
    scope:wrap(overlay.letterbox, "SetMultColour", function(original)
        return function(widget, r, g, b, a, ...)
            return original(widget, r, g, b, finite(a) and a * factor or a, ...)
        end
    end)
    local function tint(widget)
        local anim = widget:GetAnimState()
        local r, g, b, a = anim:GetMultColour()
        if finite(a) then anim:SetMultColour(r, g, b, a * factor) end
    end
    scope:wrap(overlay, "ApplyLevels", function(original)
        return function(widget, ...)
            local function finish(...)
                -- Native ApplyLevels just rewrote these colours; never multiply yesterday's alpha.
                if widget.alpha > 0 then tint(widget.bg) end
                if widget.fade > 0 then tint(widget.dust) end
                return ...
            end
            return finish(original(widget, ...))
        end
    end)
    scope:defer(function()
        if ctx.G.TheWorld == self.world and ctx.player.HUD == ctx.hud
            and overlay.inst:IsValid() then
            -- The closed scope forwards to native ApplyLevels, including any later wrapper.
            overlay:ApplyLevels()
        end
    end)
    -- Keep shown/fade/blind intact: dust.shown also controls starting/stopping storm audio.
    overlay:ApplyLevels()
end

local function comfort(self)
    local ctx, scope = self.ctx, self.scope
    local G = ctx.G
    if ctx.config.reduce_shake and G.TheCamera and G.TheCamera.Shake then
        scope:wrap(G.TheCamera, "Shake", function(original)
            return function(camera, kind, duration, speed, scale, ...)
                return original(camera, kind, duration, speed,
                    (scale or 1) * (active(self.state) == self and 0.25 or 1), ...)
            end
        end)
    end
    local cube = self.world.components and self.world.components.colourcube
    local latest = ctx.config.reduce_distortion and G.Profile and G.Profile:GetDistortionModifier()
    if cube and cube.SetDistortionModifier and finite(latest) then
        scope:wrap(cube, "SetDistortionModifier", function(original)
            return function(component, modifier, ...)
                latest = modifier
                return original(component, finite(modifier) and modifier * 0.25 or modifier, ...)
            end
        end)
        scope:defer(function()
            if G.TheWorld == self.world and self.world:IsValid() then
                cube:SetDistortionModifier(latest)
            end
        end)
        cube:SetDistortionModifier(latest)
    end
end

function M.new(ctx)
    local state = installations[ctx.G] or M.install({ GLOBAL = ctx.G })
    if state.active then state.active.scope:close() end
    local self = { ctx = ctx, state = state, world = ctx.G.TheWorld }
    self.scope = Scope.new(ctx.player, function(err) ctx:report(err) end)
    ctx.scope:defer(function() self.scope:close() end)
    if not self.world or not self.world.IsValid then return self end
    observe_luts(state)
    observe_map(state, self.world)
    state.active = self
    self.scope:defer(function()
        if state.active == self then
            state.active = nil
            replay_world(state, self)
        end
    end)
    self.scope:listen("onremove", function() self.scope:close() end, self.world)
    comfort(self)
    sand_overlay(self)
    replay_world(state, self)
    return self
end

return M
