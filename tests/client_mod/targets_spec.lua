local T = require("tests.client_mod.support")
local Targets = require("fieldkit/game/targets")

local function visual(prefab, model, current, tags)
    local entity = T.entity({ prefab = prefab, model = model, current = current, tags = tags })
    entity.AnimState = { GetBuild = function() return entity.model end,
        IsCurrentAnimation = function(_, name) return entity.current == name end }
    return entity
end

local plant_source = T.source("prefabs/farm_plants")
local research = T.eval(assert(plant_source:match("local function GetResearchStage%b().-\nend"))
    .. "\nreturn GetResearchStage")
local update = T.eval(assert(plant_source:match("local function UpdateResearchStage%b().-\nend"))
    .. "\nreturn UpdateResearchStage")
local plant_defs = T.source("prefabs/farm_plant_defs")
local plant_info = T.eval("return " .. assert(plant_defs:match("data%.plantregistryinfo = (%b{})")))
local function plant(prefab, info, weed)
    local entity = T.entity({ prefab = prefab, GetResearchStage = research, stage = 0 })
    entity._research_stage = { value = function() return entity.stage end,
        set = function(_, n) entity.stage = n end }
    entity[weed and "weed_def" or "plant_def"] = { plantregistryinfo = info }
    return entity
end

T.case("native crop stages and client metadata", function()
    local count = 0
    for name in plant_defs:gmatch("PLANT_DEFS%.(%w+)%s*=%s*{") do
        if name ~= "randomseed" then
            local crop = plant("farm_plant_" .. name, plant_info)
            count = count + 1
            for stage = 1, 6 do
                for _, oversized in ipairs({ false, true }) do
                    crop.is_oversized = oversized
                    update(crop, stage)
                    local state = Targets.describe(crop)
                    assert(state.phase == research(crop) and state.crop and not state.weed)
                    assert(state.mature == (stage == 5) and state.rotten == (stage == 6))
                    assert(Targets.accepts(crop, "mature_crops") == (stage == 5))
                end
            end
            crop.components.growable = { stage = 900 }
            assert(Targets.describe(crop).phase == 8, "host-only components cannot override client state")
        end
    end
    assert(count >= 14, "exercise every current crop definition")
    local seed = plant("farm_plant_randomseed")
    seed.plant_def.is_randomseed = true
    assert(Targets.describe(seed).phase == 1 and not Targets.accepts(seed, "mature_crops"))
end)

T.case("native weeds and new plant definitions inherit support", function()
    local source = T.source("prefabs/weed_defs")
    local info = T.eval("return " .. assert(source:match("data%.plantregistryinfo = (%b{})")))
    local names = {}
    for name in source:gmatch("WEED_DEFS%.([%w_]+)%s*=%s*{") do names[name] = true end
    local count = 0
    for name in pairs(names) do
        count = count + 1
        local weed = plant(name, info, true)
        for stage, data in ipairs(info) do
            weed.stage = stage - 1
            local state = Targets.describe(weed)
            assert(state.phase == stage and state.mature == (data.fullgrown == true))
            assert(Targets.accepts(weed, "weeds") and not Targets.accepts(weed, "mature_crops"))
        end
    end
    assert(count >= 4)
    local new = plant("modded_crop", { {}, { fullgrown = true }, { is_rotten = true } })
    new.stage = 1
    assert(Targets.accepts(new, "mature_crops"), "maturity comes from metadata, not a fixed stage number")
    local sample = Targets.describe(new)
    new.stage = 2
    assert(not Targets.matches(sample, new) and not Targets.accepts(new, "mature_crops"))
    new.stage = 99
    assert(Targets.describe(new).phase == false and not Targets.matches(Targets.describe(new), new))
end)

T.each("native tree animation families", { "evergreens", "moontree", "palmconetree", "tree_rocks" }, function(name)
    local makeanims = T.eval(assert(T.source("prefabs/" .. name):match("local function makeanims%b().-\nend"))
        .. "\nreturn makeanims")
    local tree = visual("new_tree_using_native_animations", "new_build", "", { tree = true })
    for _, phase in ipairs({ "short", "normal", "tall", "old" }) do
        local anims = makeanims(phase)
        for _, key in ipairs({ "idle", "sway1", "sway2", "chop", "burnt", "chop_burnt" }) do
            if anims[key] then
                tree.current = anims[key]
                assert(Targets.describe(tree).phase == phase, name .. "/" .. key)
            end
        end
        tree.current = "grow_seed_to_" .. phase
        assert(Targets.describe(tree).phase == phase)
    end
end)

T.case("growth models, transitions and unknown states", function()
    local tree = visual("tree_rock1", "tree_rock_normal", "sway1_loop_normal", { tree = true })
    local sample = Targets.describe(tree)
    tree.current = "grow_short_to_normal"
    assert(Targets.matches(sample, tree))
    tree.current = "idle_short"
    assert(not Targets.matches(sample, tree))
    tree.current = "unrecognized_transition"
    assert(Targets.describe(tree).phase == false and not Targets.matches(sample, tree))
    tree:AddTag("rock_tree"); tree:AddTag("boulder"); tree:RemoveTag("tree")
    assert(Targets.describe(tree).phase == "fallen" and not Targets.matches(sample, tree))
    local winter = visual("winter_tree", "evergreen_new", "sway2_loop_tall", { winter_tree = true })
    assert(Targets.describe(winter).phase == "tall", "winter trees have no tree tag")
    winter.current = "idle"
    assert(Targets.describe(winter).phase == "empty")
    local ocean = visual("oceantree", "oceantree_normal", "idle_loop", { tree = true })
    sample = Targets.describe(ocean)
    ocean.model = "oceantree_tall"
    assert(not Targets.matches(sample, ocean))
    ocean.model = "oceantree_tall_jammed_build"
    assert(Targets.describe(ocean).phase == "tall")
    local living = visual("livingtree_halloween", "evergreen_living_wood_growable", "grow_young_to_old", { tree = true })
    assert(Targets.describe(living).phase == "old")
    living:AddTag("stump")
    sample = Targets.describe(living)
    living.current = "stump_young"
    assert(Targets.matches(sample, living) and Targets.accepts(living, "stumps"))
end)

T.case("native marble and fruit phases", function()
    local states = T.eval("return " .. assert(T.source("prefabs/marbleshrub"):match("local statedata%s*=%s*(%b{})")),
        { TUNING = {} })
    local marble = visual("marbleshrub", "marbleshrub", "", { boulder = true })
    for _, state in ipairs(states) do
        for _, key in ipairs({ "idleanim", "hitanim", "growanim" }) do
            marble.current = state[key]
            assert(Targets.describe(marble).phase == state.name)
        end
    end
    local anims = T.eval("return " .. assert(T.source("prefabs/rock_avocado_bush"):match("local BUSH_ANIMS%s*=%s*(%b{})")))
    local fruit = visual("rock_avocado_bush", "rock_avocado_build", "", { plant = true })
    local phases = {}
    for _, stage in ipairs(anims) do
        fruit.current = stage.idle
        local sample = Targets.describe(fruit)
        assert(not phases[sample.phase], "each yield stage stays distinct")
        phases[sample.phase] = true
        fruit.current = stage.grow
        assert(Targets.matches(sample, fruit), "growth animation already belongs to the new stage")
    end
end)

T.case("ordinary targets and decorative scaling need no stage", function()
    for _, kind in ipairs({ { "rocks", "rock1", "full", { boulder = true } },
        { "mushtree_tall", "mushroom_tree_tall", "idle_loop", { tree = true } },
        { "livingtree", "evergreen_living_wood", "idle_loop", { tree = true } } }) do
        local entity = visual(unpack(kind))
        local sample = Targets.describe(entity)
        assert(sample.phase == nil)
        entity.scale = 1.1
        assert(Targets.matches(sample, entity))
        entity:AddTag("burnt")
        assert(not Targets.matches(sample, entity))
    end
end)

T.case("registered readers are shared and isolated", function()
    local custom = T.entity({ prefab = "custom_resource", stage = 2 })
    Targets.register("test_resource", function(e)
        if e.prefab == "custom_resource" then return { phase = e.stage } end
    end)
    local sample = Targets.describe(custom)
    assert(sample.phase == 2 and Targets.matches(sample, custom))
    custom.stage = 3
    assert(not Targets.matches(sample, custom))
    Targets.register("test_resource", function() error("broken extension") end)
    assert(Targets.describe(custom).phase == false)
    Targets.register("test_resource", function() return { phase = {} } end)
    assert(Targets.describe(custom).phase == false)
    Targets.register("test_resource", function() end)
    assert(Targets.describe(custom).phase == nil)
    assert(not pcall(Targets.register, "test_resource", false))
end)

T.done()
