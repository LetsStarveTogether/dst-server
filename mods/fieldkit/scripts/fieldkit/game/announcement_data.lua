local Metrics = require("fieldkit/game/metrics")
local M = {}

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

local function name(G, prefab)
    return G.STRINGS.NAMES[string.upper(prefab)] or prefab
end

local function ready(player, key)
    local replica = player.replica and player.replica[key]
    return replica and (replica.classified or player.components and player.components[key])
end

function M.recipe(ctx, key)
    local G, player = ctx.G, ctx.player
    local recipe = type(key) == "string" and G.GetValidRecipe(key)
    local builder, inventory = player.replica.builder, player.replica.inventory
    if not recipe or not ready(player, "builder") or not ready(player, "inventory") then return end
    local result = { name = G.STRINGS.NAMES[string.upper(recipe.nameoverride or recipe.name)] or name(G, recipe.product),
        recipe = recipe.name, ingredients = {} }
    if not builder:CanLearn(recipe.name) then result.status = "restricted"
    elseif builder:IsBuildBuffered(recipe.name) then result.status = "buffered"; return result
    elseif builder:IsFreeBuildMode() then result.status = "free"; return result
    elseif builder:KnowsRecipe(recipe) then result.status = "known"
    elseif G.CanPrototypeRecipe(recipe.level, builder:GetTechTrees()) then result.status = "prototype"
    else result.status = "locked" end

    local modifier, character = builder:IngredientMod(), G.CHARACTER_INGREDIENT
    for _, category in ipairs({ "item", "character", "tech" }) do
        local source = category == "item" and recipe.ingredients or recipe[category .. "_ingredients"]
        for index, ingredient in ipairs(source) do
            local entry = { category = category, index = index, type = ingredient.type,
                name = name(G, ingredient.type), required = ingredient.amount }
            if category == "item" then
                entry.required = math.max(1, G.RoundBiasedUp(ingredient.amount * modifier))
                entry.has, entry.count = inventory:Has(ingredient.type, entry.required, true)
                entry.unit = "count"
            elseif category == "tech" then
                entry.has, entry.count = builder:HasTechIngredient(ingredient)
                entry.unit = "level"
            else
                local health = ingredient.type == character.HEALTH or ingredient.type == character.MAX_HEALTH
                local sanity = ingredient.type == character.SANITY or ingredient.type == character.MAX_SANITY
                if ready(player, health and "health" or sanity and "sanity" or "") then
                    entry.has, entry.count = builder:HasCharacterIngredient(ingredient)
                end
                entry.unit = "points"
                if ingredient.type == character.MAX_HEALTH or ingredient.type == character.MAX_SANITY then
                    entry.unit = "percent"
                elseif ingredient.type == character.HEALTH then
                    entry.strict = true
                    if player:HasTag("health_as_oldage") then
                        entry.required = math.ceil(ingredient.amount * G.TUNING.OLDAGE_HEALTH_SCALE)
                        entry.name = name(G, character.OLDAGE)
                    end
                end
            end
            if not finite(entry.count) then entry.count = nil end
            entry.quality = entry.count ~= nil and type(entry.has) == "boolean" and "known" or "unknown"
            result.ingredients[#result.ingredients + 1] = entry
        end
    end
    return result
end

function M.food(ctx, category, prefab)
    local G, cookbook = ctx.G, ctx.G.TheCookbook
    local recipes = require("cooking").cookbook_recipes[category]
    local definition = recipes and recipes[prefab]
    local known = cookbook and cookbook:IsUnlocked(prefab)
    if not definition or type(known) ~= "table" or not known.has_eaten
        or definition.custom_cookbook_details_fn ~= nil then return end
    local result = { name = name(G, prefab), prefab = prefab, category = category }
    for _, key in ipairs({ "health", "hunger", "sanity" }) do
        local value = definition[key]
        result[key] = finite(value) and math.floor(value * 10) / 10 or nil
    end
    local strings = G.STRINGS.UI.COOKBOOK
    result.effect = type(definition.oneat_desc) == "string" and definition.oneat_desc
        or finite(definition.temperature) and definition.temperature > 0 and strings.FOOD_EFFECTS_HOT_FOOD
        or finite(definition.temperature) and definition.temperature < 0 and strings.FOOD_EFFECTS_COLD_FOOD
        or strings.FOOD_EFFECTS_NONE
    return result
end

function M.circuits(ctx)
    local player, G = ctx.player, ctx.G
    if not player.GetModulesData
        or not (player.components and player.components.upgrademoduleowner or player.wx78_classified) then return {} end
    local energy = Metrics.get(ctx, "energy")
    if not energy or energy.quality ~= "known" then return {} end
    local modules, result = player:GetModulesData(), {}
    local definitions = require("wx78_moduledefs")
    for bar = G.CIRCUIT_BARS.ALPHA, G.CIRCUIT_BARS.GAMMA do
        local entries, charge = modules[bar], energy.value
        if type(entries) ~= "table" then return {} end
        for index, netid in ipairs(entries) do
            if netid == 0 then break end
            local definition = definitions.GetModuleDefinitionFromNetID(netid)
            if not definition or not finite(definition.slots) or definition.slots <= 0 then return {} end
            charge = charge - definition.slots
            result[#result + 1] = { bar = bar, index = index, netid = netid,
                name = name(G, "wx78module_" .. definition.name), slots = definition.slots, powered = charge >= 0 }
        end
    end
    return result
end

return M
