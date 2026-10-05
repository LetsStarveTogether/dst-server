local T = require("tests.client_mod.support")
local Data = require("fieldkit/game/announcement_data")
local character = { HEALTH = "health", MAX_HEALTH = "max_health", SANITY = "sanity", MAX_SANITY = "max_sanity", OLDAGE = "oldage" }
local recipe = { name = "test", product = "test", ingredients = { { type = "twigs", amount = 3 } },
    character_ingredients = { { type = "health", amount = 10 }, { type = "max_health", amount = .25 } },
    tech_ingredients = { { type = "sculpting_material", amount = 2 } } }
local health, penalty, oldage, charge = 10, .6, false, 3
local player = T.entity({ HasTag = function() return oldage end })
local ctx = { player = player, G = {
    CHARACTER_INGREDIENT = character, TUNING = { OLDAGE_HEALTH_SCALE = .4, MAXIMUM_HEALTH_PENALTY = .75 },
    STRINGS = { NAMES = {}, UI = { COOKBOOK = {
        FOOD_EFFECTS_NONE = "None", FOOD_EFFECTS_COLD_FOOD = "Cold", FOOD_EFFECTS_HOT_FOOD = "Hot" } } },
    GetValidRecipe = function(key) return key == "test" and recipe end,
    RoundBiasedUp = function(value) return math.floor(value + .5) end,
    CIRCUIT_BARS = { ALPHA = 0, BETA = 1, GAMMA = 2 },
} }
local env = setmetatable({ CHARACTER_INGREDIENT = character, TUNING = ctx.G.TUNING,
    Class = T.Class, require = function() return {} end,
    IsTechIngredient = function() return true end }, { __index = _G })
local NativeBuilder = T.native("components/builder_replica", env)
player.replica.health = { classified = {}, GetCurrent = function() return health end,
    GetPenaltyPercent = function() return penalty end }
local builder = { inst = player, classified = { techtrees = { SCULPTING = 1 } },
    CanLearn = function() return true end, IsBuildBuffered = function() return false end,
    IsFreeBuildMode = function() return false end, KnowsRecipe = function() return true end,
    IngredientMod = function() return .5 end, HasCharacterIngredient = NativeBuilder.HasCharacterIngredient,
    HasTechIngredient = NativeBuilder.HasTechIngredient }
player.replica.builder = builder
player.replica.inventory = { classified = {}, Has = function(_, kind, amount, containers)
    assert(kind == "twigs" and amount == 2 and containers); return false, 1
end }
T.case("native recipe names", function()
    local names = { CARRAT = "Carrat", WINONA_CATAPULT = "Winona's Catapult",
        TOPHAT_MAGICIAN = "Magician's Top Hat", TOPHAT = "Top Hat",
        RECIPE = "Recipe name", PRODUCT = "Product name", OVERRIDE = "Override name" }
    local selected
    local naming_ctx = { player = player, G = setmetatable({
        STRINGS = { NAMES = names }, GetValidRecipe = function() return selected end,
    }, { __index = ctx.G }) }
    local NativeDetails = T.native("widgets/redux/craftingmenu_details", {
        Class = T.Class, require = function() return {} end, STRINGS = { NAMES = names },
    })
    local native_name
    local panel = { panel_width = 400, namestring = {
        SetMultilineTruncatedString = function(_, value) native_name = value end,
    } }
    local source = T.source("recipes")
    T.each("display name", {
        { name = "wormwood mutation", native = "wormwood_carrat", expected = "Carrat" },
        { name = "portable catapult", native = "winona_catapult_item", expected = "Winona's Catapult" },
        { name = "magician variant", native = "tophat_magician", expected = "Magician's Top Hat" },
        { name = "recipe before product", expected = "Recipe name" },
        { name = "override before recipe", override = "override", expected = "Override name" },
        { name = "missing recipe translation", recipe = "untranslated", expected = "Product name" },
        { name = "missing override translation", override = "untranslated", expected = "Product name" },
        { name = "missing all translations", recipe = "untranslated", product = "internal", expected = "internal", fallback = true },
    }, function(row)
        selected = { name = row.recipe or "recipe", product = row.product or "product", nameoverride = row.override }
        if row.native then
            local start = assert(source:find('Recipe2("' .. row.native .. '"', 1, true))
            local declaration = assert(source:sub(start):match("^Recipe2%b()"))
            T.eval(declaration, { CHARACTER_INGREDIENT = character, TECH = { NONE = {} },
                Ingredient = function() end, Recipe2 = function(name, _, _, config)
                    selected = config or {}
                    selected.name, selected.product = name, selected.product or name
                end })
        end
        selected.numtogive, selected.ingredients = 1, {}
        selected.character_ingredients, selected.tech_ingredients = {}, {}
        panel.data = { recipe = selected, meta = {} }
        NativeDetails.UpdateNameString(panel)
        assert(native_name == (not row.fallback and row.expected or nil), "expected names follow the native crafting menu")
        local data = Data.recipe(naming_ctx, selected.name)
        assert(data.name == row.expected and data.recipe == selected.name,
            "announcement names preserve the recipe identity while matching its crafting label")
    end)
end)
T.each("native health costs", {
    { name = "last health", health = 10, oldage = false, required = 10, has = false, count = 10 },
    { name = "health rounded up", health = 10.1, oldage = false, required = 10, has = true, count = 11 },
    { name = "old-age conversion", health = 10, oldage = true, required = 4, has = true, count = 10 },
    { name = "last old-age point", health = 4, oldage = true, required = 4, has = false, count = 4 },
}, function(row)
    health, oldage = row.health, row.oldage
    local data = Data.recipe(ctx, "test")
    assert(data.ingredients[1].required == 2 and data.ingredients[1].count == 1)
    local cost = data.ingredients[2]
    assert(cost.strict and cost.has == row.has and cost.count == row.count and cost.required == row.required)
    assert(cost.name == (row.oldage and "oldage" or "health"))
    assert(not data.ingredients[3].has and data.ingredients[3].unit == "percent" and data.ingredients[3].count == .4)
    assert(data.ingredients[4].unit == "level" and data.ingredients[4].count == 1)
end)
player.replica.health.classified = nil
assert(Data.recipe(ctx, "test").ingredients[2].quality == "unknown", "unattached health replicas cannot invent zero")
player.replica.builder.classified = nil
assert(Data.recipe(ctx, "test") == nil)

local eaten, unlocked = false, false
ctx.G.TheCookbook = { IsUnlocked = function() return unlocked and { has_eaten = eaten } end }
package.loaded.cooking = { cookbook_recipes = { cookbook = {
    soup = { health = 12.39, hunger = -3.14, sanity = 0, temperature = 10 },
    custom = { health = 100, custom_cookbook_details_fn = function() end },
} } }
T.each("cookbook visibility", {
    { name = "undiscovered", unlocked = false, eaten = false },
    { name = "discovered but uneaten", unlocked = true, eaten = false },
    { name = "eaten", unlocked = true, eaten = true, visible = true },
    { name = "custom details", unlocked = true, eaten = true, prefab = "custom" },
}, function(row)
    unlocked, eaten = row.unlocked, row.eaten
    local food = Data.food(ctx, "cookbook", row.prefab or "soup")
    if row.visible then assert(food.health == 12.3 and food.hunger == -3.2 and food.sanity == 0 and food.effect == "Hot")
    else assert(not food) end
end)

assert(#Data.circuits(ctx) == 0)
local modules = { [0] = { 1, 2, 0 }, [1] = { 2, 0 }, [2] = { 0 } }
player.GetModulesData = function() return modules end
assert(#Data.circuits(ctx) == 0, "WX getters without synchronized classified state cannot expose default chips")
player.wx78_classified = { GetEnergyLevel = function() return charge end, GetMaxEnergy = function() return 7 end }
package.loaded.wx78_moduledefs = { GetModuleDefinitionFromNetID = function(id)
    return ({ { name = "one", slots = 2 }, { name = "two", slots = 3 } })[id]
end }
local circuits = Data.circuits(ctx)
assert(#circuits == 3 and circuits[1].bar == 0 and circuits[1].powered and not circuits[2].powered
    and circuits[3].bar == 1 and circuits[3].powered, "each native circuit bar has the full shared charge budget")
charge = 7
assert(Data.circuits(ctx)[2].powered, "effective shadow power comes from the classified energy getter")
modules[0][1] = 99
assert(#Data.circuits(ctx) == 0, "unknown or unsynchronized chip definitions are not guessed")
T.done()
