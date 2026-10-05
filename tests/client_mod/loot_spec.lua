local T = require("tests.client_mod.support")
local Loot = require("fieldkit/features/loot")
local Rules = require("fieldkit/rules/loot")
local Scope = require("fieldkit/core/scope")

local function scenario(name, run)
    T.case(name, function()
        local guid, ctx = 0, { sounds = 0, known = {}, config = { loot_sound = true } }
        local function entity(prefab, x, item)
            guid = guid + 1
            local e = T.entity({ prefab = prefab, GUID = guid, x = x })
            e.AnimState = { IsCurrentAnimation = function(_, animation) return e.animation == animation end }
            if item then e.replica.inventoryitem = { IsHeld = function() return e.held end }
            else e.replica.health = { IsDead = function() return e.dead end } end
            return e
        end
        ctx.player = entity("wilson")
        ctx.player.replica.builder = { KnowsRecipe = function(_, recipe, ignore_bonus)
            assert(ignore_bonus, "temporary crafting access must not count as learned")
            return ctx.known[recipe] == true
        end }
        ctx.scope = Scope.new(ctx.player, error)
        ctx.G = { AllRecipes = { dragonflyfurnace = {}, bundlewrap = {}, arbitrary = {} },
            STRINGS = { NAMES = { BLUEPRINT = "Blueprint", BLUEPRINT_RARE = "Rare {item}",
                DRAGONFLYFURNACE = "Scaled Furnace", BUNDLEWRAP = "Bundling Wrap", ARBITRARY = "Other Recipe" } },
            subfmt = function(text, values) return (text:gsub("{item}", values.item)) end,
            TheFocalPoint = { SoundEmitter = { PlaySound = function() ctx.sounds = ctx.sounds + 1 end } } }
        function ctx:text(_, en) return en end
        run(ctx, entity, Loot.new(ctx))
        ctx.scope:close()
        assert(T.listener_count(ctx.player) == 0)
    end)
end

scenario("observation, pickup and alert lifecycle", function(ctx, entity, tracker)
    local dragon = entity("dragonfly")
    local old = entity("dragon_scales", 1, true)
    local frame = tracker:observe({ dragon, old }, 0)
    assert(#frame == 1 and not frame[1].glow and ctx.sounds == 0, "existing loot must never flash on join")
    dragon.dead = true
    local fresh = entity("dragon_scales", 2, true)
    frame = tracker:observe({ dragon, old, fresh }, 1)
    assert(#frame == 2 and frame[1].entity == fresh and frame[1].glow and frame[1].quality == "inferred")
    assert(frame[1].label:find("likely new drop") and ctx.sounds == 1)
    tracker:observe({ dragon, old, fresh }, 2)
    assert(ctx.sounds == 1, "a visible item alerts once")
    fresh.held = true
    frame = tracker:observe({ dragon, old, fresh }, 3)
    assert(#frame == 1 and frame[1].entity == old, "pickup immediately releases the item effect")
    fresh.held = false
    frame = tracker:observe({ dragon, old, fresh }, 4)
    assert(#frame == 1, "dropping the same inventory item must not rearm a shiny alert")
    local unrelated = entity("walrus_tusk", 1, true)
    frame = tracker:observe({ dragon, old, unrelated }, 5)
    assert(#frame == 2 and frame[1].entity == unrelated and not frame[1].glow, "unrelated deaths cannot prove a drop")
    assert(frame[1].label:find("newly observed") and ctx.sounds == 1)
    assert(#tracker:observe({ dragon, old, unrelated }, 36) == 0, "effects and labels expire")
    assert(#tracker:observe({}, 37) == 0)
    assert(#tracker:observe({ fresh, unrelated }, 38) == 0, "walking away and back does not repeat old alerts")
end)

scenario("visible source history", function(ctx, entity, tracker)
    local dragon = entity("dragonfly")
    dragon.dead = true
    tracker:observe({ dragon }, 20)
    local frame = tracker:observe({ dragon, entity("dragon_scales", 1, true) }, 21)
    assert(#frame == 1 and not frame[1].glow, "an old corpse is not a fresh death")
    dragon.dead = false
    tracker:observe({ dragon }, 22)
    tracker:observe({}, 23)
    dragon.dead = true
    frame = tracker:observe({ dragon, entity("dragon_scales", 1, true) }, 24)
    assert(not frame[1].glow, "death while out of view cannot be presented as witnessed")
end)

scenario("late break effect", function(ctx, entity, tracker)
    tracker:observe({}, 30)
    local fossil = entity("fossil_piece", 1, true)
    local frame = tracker:observe({ fossil }, 31)
    assert(not frame[1].glow)
    local rockfx = entity("rock_break_fx")
    rockfx.tags.NOCLICK = true
    frame = tracker:observe({ fossil, rockfx }, 31.25)
    assert(frame[1].glow, "replicated loot may arrive before its visible break effect")
    assert(frame[1].expires == 61.25, "the thirty-second effect starts when its evidence arrives")
end)

scenario("replicated blueprint identity", function(ctx, entity, tracker)
    local dragon = entity("dragonfly")
    tracker:observe({ dragon }, 40)
    dragon.dead = true
    local blueprint = entity("blueprint", 1, true)
    blueprint.name = "Rare Scaled Furnace"
    local frame = tracker:observe({ dragon, blueprint }, 41)
    assert(#frame == 1 and frame[1].glow, "rare named blueprints use the replicated name")
    ctx.known.dragonflyfurnace = true
    assert(#tracker:observe({ dragon, blueprint }, 42) == 0, "learning removes an active blueprint effect")
    local generic = entity("blueprint", 1, true)
    generic.name = "Other Recipe Blueprint"
    frame = tracker:observe({ generic }, 43)
    assert(#frame == 1 and not frame[1].label:find("unidentified"), "ordinary named recipes are recognized too")
    local unidentified = entity("blueprint", 1, true)
    unidentified.name, unidentified.recipetouse = "Unrecognized Name", "bundlewrap"
    local beequeen = entity("beequeen")
    tracker:observe({ beequeen }, 45)
    beequeen.dead = true
    frame = tracker:observe({ beequeen, unidentified }, 46)
    assert(#frame == 1 and not frame[1].glow and frame[1].label:find("unidentified"),
        "host-only recipe fields must not guess a client's blueprint identity")
end)

scenario("ambiguous and cross-language names", function(ctx, entity, tracker)
    ctx.G.AllRecipes.collision = {}
    ctx.G.STRINGS.NAMES.COLLISION = "Other Recipe"
    assert(Rules.blueprint_names(ctx.G)["Other Recipe Blueprint"] == false, "ambiguous translated names stay unidentified")
    ctx.G.STRINGS.NAMES.DRAGONFLYFURNACE = "龙鳞火炉"
    ctx.G.STRINGS.NAMES.BLUEPRINT = "蓝图"
    ctx.G.STRINGS.NAMES.BLUEPRINT_RARE = "{item}蓝图"
    tracker = Loot.new(ctx)
    ctx.known.dragonflyfurnace = true
    local english = entity("blueprint", 1, true)
    english.name = "Scaled Furnace Blueprint"
    assert(#tracker:observe({ english }, 47) == 0, "an English server's learned blueprint is recognized by a Chinese client")
    ctx.known.dragonflyfurnace = false
    local another = entity("blueprint", 1, true)
    another.name = english.name
    local frame = tracker:observe({ another }, 48)
    assert(#frame == 1 and not frame[1].label:find("unidentified"), "unlearned cross-language recipes remain in the built-in catalog")
end)

for _, row in ipairs({
    { name = "nonlethal defeat", source = "daywalker2", loot = "scrap_monoclehat", animation = "defeat", delay = 0, glow = true },
    { name = "delayed finale", source = "alterguardian_phase4_lunarrift", loot = "lunar_seed", corpse = true, animation = "finale_pst", at = 19, delay = 0, glow = true },
    { name = "long death sequence", source = "stalker_atrium", loot = "thurible", delay = 10.6, glow = true },
    { name = "expired death", source = "dragonfly", loot = "dragon_scales", delay = 7, glow = false },
}) do
    scenario("visible defeat/" .. row.name, function(ctx, entity, tracker)
        local source = entity(row.source)
        source.dead = row.corpse
        tracker:observe({ source }, 1)
        source.animation, source.dead = row.animation, row.animation == nil or source.dead
        local at = row.at or 2
        tracker:observe({ source }, at)
        local frame = tracker:observe({ source, entity(row.loot, 1, true) }, at + row.delay)
        assert(#frame == 1 and frame[1].glow == row.glow)
    end)
end

scenario("marker budget preserves rare drops", function(ctx, entity, tracker)
    local clutter = {}
    for i = 1, 300 do clutter[i] = entity("dragon_scales", i / 100, true) end
    assert(#tracker:observe(clutter, 83) == 24, "the renderer receives a fixed marker budget")
    tracker = Loot.new(ctx)
    local dragon = entity("dragonfly")
    clutter = { dragon }
    for i = 1, 300 do clutter[#clutter + 1] = entity("livinglog", i / 100, true) end
    tracker:observe(clutter, 90)
    dragon.dead = true
    local scale = entity("dragon_scales", 1, true)
    clutter[#clutter + 1] = scale
    local frame = tracker:observe(clutter, 91)
    assert(frame[1].entity == scale and frame[1].glow, "old material piles cannot consume every rare-drop slot")
end)

T.done()
