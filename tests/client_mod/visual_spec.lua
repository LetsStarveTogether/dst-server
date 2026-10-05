local T = require("tests.client_mod.support")
T.case("visual contracts", function()
    local Visual = require("fieldkit/features/visual")
    local Scope = require("fieldkit/core/scope")
    local IDENTITY = "images/colour_cubes/identity_colourcube.tex"
    local SNOW = "levels/textures/snow.tex"
    local PUDDLES = "levels/textures/mud.tex"

    local function fixture(native_objects)
        local state = { calls = {}, hooks = {}, reports = {} }
        local function engine(methods)
            if not native_objects then return methods end
            local object = newproxy(true)
            getmetatable(object).__index = methods
            return object
        end
        local player = T.entity({ HUD = {} })
        local G = { ThePlayer = player }
        local env = {
            GLOBAL = G,
            AddGamePostInit = function(fn) state.hooks.game = fn end,
            AddComponentPostInit = function(name, fn)
                assert(name == "worldstate")
                state.hooks.world = fn
            end,
        }
        local installation = Visual.install(env)
        assert(installation == Visual.install(env), "install must not stack method observers")
        G.PostProcessor = engine({ SetColourCubeData = function(_, index, source, dest, ...)
            state.calls[index] = { source, dest, n = select("#", ...), ... }
            return "lut", nil, index
        end })
        local map_methods = {
            SetOverlayTexture = function(_, texture) state.texture = texture; return "texture", nil, 9 end,
            SetOverlayLerp = function(_, level, ...) state.level = level; return "level", nil, ... end,
        }
        function state:world()
            G.TheWorld = { components = {}, IsValid = function(self) return not self.removed end }
            -- Native Map instances share one __index table across worlds.
            G.TheWorld.Map = engine(native_objects and map_methods or {
                SetOverlayTexture = map_methods.SetOverlayTexture, SetOverlayLerp = map_methods.SetOverlayLerp,
            })
            -- Deliberately run worldstate before GamePostInit, matching the delayed-controller path.
            self.hooks.world({}, G.TheWorld)
            for index = 0, 2 do G.PostProcessor:SetColourCubeData(index, "source" .. index, "dest" .. index) end
            return G.TheWorld
        end
        function state:context(config)
            local ctx = { G = G, player = player, hud = player.HUD, config = config or {} }
            function ctx:report(err) state.reports[#state.reports + 1] = err end
            ctx.scope = Scope.new(player, function(err) ctx:report(err) end)
            return ctx
        end
        state:world()
        state.hooks.game()
        state.G, state.player = G, player
        return state
    end

    local state = fixture()
    local G, world = state.G, state.G.TheWorld
    local ctx = state:context({ colour_filter = "neutral_effects", snow_overlay = 0.6 })
    world.Map:SetOverlayTexture(SNOW)
    world.Map:SetOverlayLerp(2.7)
    Visual.new(ctx)
    assert(state.calls[0][1] == "source0" and state.calls[1][1] == IDENTITY and state.calls[2][1] == IDENTITY)
    assert(math.abs(state.level - 1.8) < 0.00001, "snow cap is a fraction of the native 0..3 range")
    local a, b, c = G.PostProcessor:SetColourCubeData(2, "new-source", "new-dest", "extension", nil)
    assert(a == "lut" and b == nil and c == 2 and state.calls[2].n == 2)
    assert(state.calls[2][3] == "extension" and state.calls[2][1] == IDENTITY)
    a, b, c = world.Map:SetOverlayLerp(0.2, "extension")
    assert(a == "level" and b == nil and c == "extension" and state.level == 0.2)
    T.each("unsnowed terrain", { { name = PUDDLES, level = 2.8 }, { name = "custom-surface.tex", level = 2.9 } }, function(case)
        world.Map:SetOverlayTexture(case.name)
        world.Map:SetOverlayLerp(case.level)
        assert(state.level == case.level, "the native shared setter must preserve other terrain overlays")
    end)
    world.Map:SetOverlayTexture(SNOW)
    world.Map:SetOverlayLerp(2.7)

    -- Later mods may delegate to our observer: restore through them, without losing their chain.
    local lut, lerp = G.PostProcessor.SetColourCubeData, world.Map.SetOverlayLerp
    local later_calls = 0
    local later_lut = function(...) later_calls = later_calls + 1; return lut(...) end
    local later_lerp = function(...) later_calls = later_calls + 1; return lerp(...) end
    G.PostProcessor.SetColourCubeData, world.Map.SetOverlayLerp = later_lut, later_lerp
    ctx.scope:close()
    assert(state.calls[2][1] == "new-source" and state.calls[2][2] == "new-dest")
    assert(state.level == 2.7 and later_calls >= 4)
    assert(G.PostProcessor.SetColourCubeData == later_lut and world.Map.SetOverlayLerp == later_lerp)

    -- Replacing the active owner in the same world closes the old visual scope exactly once.
    ctx = state:context({ colour_filter = "neutral_all", snow_overlay = 0 })
    local old = Visual.new(ctx)
    assert(state.calls[0][1] == IDENTITY and state.level == 0)
    local nextctx = state:context({ colour_filter = "neutral_effects", snow_overlay = 1 })
    local nextvisual = Visual.new(nextctx)
    assert(old.scope.closed and not nextvisual.scope.closed)
    assert(state.calls[0][1] == "source0" and state.calls[1][1] == IDENTITY and state.level == 2.7)
    ctx.scope:close()
    assert(not nextvisual.scope.closed and state.calls[1][1] == IDENTITY)

    -- New world writes are never filtered by the previous world's still-pending cleanup.
    local newworld = state:world()
    assert(state.calls[0][1] == "source0" and state.calls[2][1] == "source2")
    newworld.Map:SetOverlayTexture(PUDDLES)
    newworld.Map:SetOverlayLerp(2.4)
    local newctx = state:context({ colour_filter = "neutral_all", snow_overlay = 0 })
    Visual.new(newctx)
    nextctx.scope:close()
    assert(state.calls[0][1] == IDENTITY and state.level == 2.4)
    G.ThePlayer = nil
    G.PostProcessor:SetColourCubeData(0, "frontend", "frontend")
    assert(state.calls[0][1] == "frontend", "a stale player scope must not filter frontend writes")
    newctx.scope:close()
    assert(state.calls[0][1] == "frontend" and #state.reports == 0)

    -- Engine userdata cannot own Lua methods; the shared metatable hook still selects one receiver.
    T.each("world receiver", { false, true }, function(userdata)
        state = fixture(userdata)
        local oldworld = state.G.TheWorld
        oldworld.Map:SetOverlayTexture(SNOW)
        oldworld.Map:SetOverlayLerp(2.7)
        ctx = state:context({ colour_filter = "neutral_all", snow_overlay = 0 })
        Visual.new(ctx)
        assert(state.calls[2][1] == IDENTITY and state.level == 0)
        newworld = state:world()
        newworld.Map:SetOverlayTexture(SNOW)
        newworld.Map:SetOverlayLerp(2.2)
        newctx = state:context({ colour_filter = "neutral_all", snow_overlay = 0.6 })
        Visual.new(newctx)
        assert(math.abs(state.level - 1.8) < 0.00001)
        oldworld.Map:SetOverlayLerp(2.9)
        assert(state.level == 2.9, "a shared Map metatable must not alter another world's receiver")
        newworld.Map:SetOverlayLerp(2.2)
        ctx.scope:close()
        assert(math.abs(state.level - 1.8) < 0.00001)
        newctx.scope:close()
        assert(state.level == 2.2 and #state.reports == 0)

    end)

    -- No setting uses global no-op setters or edits the profile; latest user options are restored.
    state = fixture()
    G = state.G
    local modifier, scale
    local native_modifier = function(_, value) modifier = value; return "modifier", nil, 5 end
    local cube = { SetDistortionModifier = native_modifier }
    G.TheWorld.components.colourcube = cube
    G.Profile = { GetDistortionModifier = function() return 0.8 end }
    local native_shake = function(_, _, _, _, value) scale = value; return "shake", nil, 7 end
    G.TheCamera = { Shake = native_shake }
    ctx = state:context({ reduce_shake = true, reduce_distortion = true })
    Visual.new(ctx)
    assert(modifier == 0.2)
    a, b, c = cube:SetDistortionModifier(0.6)
    assert(modifier == 0.15 and a == "modifier" and b == nil and c == 5)
    a, b, c = G.TheCamera:Shake("full", 1, 0.1, 2)
    assert(scale == 0.5 and a == "shake" and b == nil and c == 7)
    local current_modifier = cube.SetDistortionModifier
    local later_modifier = function(...) return current_modifier(...) end
    cube.SetDistortionModifier = later_modifier
    ctx.scope:close()
    assert(modifier == 0.6 and cube.SetDistortionModifier == later_modifier and G.TheCamera.Shake == native_shake)

    -- Execute the real game's ApplyLevels/OnUpdate equations against fake engine draw/audio endpoints.
    local audio = { starts = 0, stops = 0 }
    local env = setmetatable({
        Class = T.Class,
        require = function() return {} end,
        Lerp = function(a, b, t) return a + (b - a) * t end,
        Remap = function(v, a, b, x, y) return x + (v - a) / (b - a) * (y - x) end,
        math = setmetatable({ clamp = function(v, lo, hi) return math.max(lo, math.min(hi, v)) end }, { __index = math }),
        TheNet = { IsServerPaused = function() return false end },
        TheFocalPoint = { SoundEmitter = {
            PlaySound = function() audio.starts = audio.starts + 1 end,
            KillSound = function() audio.stops = audio.stops + 1 end,
            SetParameter = function() end,
            SetVolume = function(_, _, value) audio.volume = value end,
        } },
    }, { __index = _G })
    local SandOver = T.native("widgets/sandover", env)
    local function animwidget()
        local colour = { 1, 1, 1, 1 }
        local anim = {
            SetMultColour = function(_, ...) colour = { ... } end,
            GetMultColour = function() return unpack(colour) end,
        }
        return {
            shown = false,
            GetAnimState = function() return anim end,
            Show = function(self) self.shown = true end,
            Hide = function(self) self.shown = false end,
            SetScale = function() end,
            alpha = function() return colour[4] end,
        }
    end
    T.each("storm alpha", { 0, .35, 1 }, function(alpha)
        audio.starts, audio.stops = 0, 0
        local overlay = setmetatable({
            brightness = 1, fade = 1, fadeto = 1, fadetime = 3, blind = 1, blindto = 1, blindtime = 0.2,
            bg = animwidget(), dust = animwidget(), inst = { IsValid = function() return true end },
            Show = function(self) self.shown = true end,
            Hide = function(self) self.shown = false end,
            StopUpdating = function() end,
            letterbox = { SetMultColour = function(self, r, g, b, alpha) self.alpha = alpha end },
        }, { __index = SandOver })
        state = fixture()
        state.player.HUD.sandover = overlay
        ctx = state:context({ sand_overlay = alpha })
        local native_levels = overlay.ApplyLevels
        overlay:ApplyLevels()
        Visual.new(ctx)
        if alpha == 1 then assert(overlay.ApplyLevels == native_levels, "full opacity leaves the native method intact") end
        assert(overlay.bg.alpha() == alpha and overlay.dust.alpha() == alpha and overlay.letterbox.alpha == alpha)
        assert(overlay.shown and overlay.dust.shown and audio.starts == 1, "transparency must preserve native sound state")
        overlay:ApplyLevels()
        assert(overlay.bg.alpha() == alpha and audio.starts == 1, "repeated updates cannot compound alpha or restart sound")
        overlay.brightness = 0.5
        overlay:ApplyLevels()
        local filtered = overlay.dust.alpha()
        ctx.scope:close()
        assert(math.abs(overlay.dust.alpha() * alpha - filtered) < 0.00001)
        assert(overlay.ApplyLevels == native_levels and overlay.bg.alpha() == 1 and overlay.letterbox.alpha == 1)
        ctx = state:context({ sand_overlay = 0 })
        Visual.new(ctx)
        assert(overlay.bg.alpha() == 0 and overlay.dust.alpha() == 0 and overlay.letterbox.alpha == 0)
        assert(overlay.dust.shown and audio.starts == 1)
        overlay.fadeto = 0
        overlay:OnUpdate(4)
        assert(not overlay.shown and not overlay.dust.shown and audio.stops == 1, "native fade-out must still end storm audio")
        ctx.scope:close()
        assert(audio.stops == 1 and #state.reports == 0)
    end)
end)
T.done()
