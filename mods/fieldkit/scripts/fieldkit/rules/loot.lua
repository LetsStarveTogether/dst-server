local M = {}

-- Native loot tables, keyed by the item actually replicated to a client.
M.items = {
    krampus_sack = { "krampus", "klaus_sack" },
    fossil_piece = { "rock_break_fx" },
    walrushat = { "walrus" },
    walrus_tusk = { "walrus" },
    rock_avocado_fruit_sprout = { "bomb_lunarplant_explode_fx", "explode_small", "explode_small_slurtle", "mining_fx" },
    hermit_cracked_pearl = { "crabking" },
    deerclops_eyeball = { "deerclops" },
    goose_feather = { "moose", "mossling" },
    dragon_scales = { "dragonfly" },
    lavae_egg = { "dragonfly" },
    bearger_fur = { "bearger" },
    townportaltalisman = { "antlion" },
    shadowheart = { "shadow_knight", "shadow_bishop", "shadow_rook" },
    thurible = { "stalker_atrium" },
    skeletonhat = { "stalker_atrium" },
    armorskeleton = { "stalker_atrium" },
    alterguardianhat = { "rock_break_fx" },
    alterguardianhatshard = { "alterguardian_phase3" },
    eyemaskhat = { "eyeofterror" },
    shieldofterror = { "twinofterror1", "twinofterror2" },
    fruitflyfruit = { "lordfruitfly" },
    minotaurhorn = { "minotaur" },
    malbatross_beak = { "malbatross" },
    royal_jelly = { "beequeen" },
    hivehat = { "beequeen" },
    scraphat = { "daywalker2" },
    scrap_monoclehat = { "daywalker2" },
    shroom_skin = { "toadstool", "toadstool_dark" },
    boat_bumper_crabking_kit = { "crabking" },
    klaussackkey = { "klaus" },
    lunar_seed = { "alterguardian_phase4_lunarrift" },
}

M.blueprints = {
    dragonflyfurnace = { "dragonfly" },
    bundlewrap = { "beequeen" },
    armordreadstone = { "daywalker" },
    dreadstonehat = { "daywalker" },
    wall_dreadstone_item = { "daywalker" },
    red_mushroomhat = { "toadstool", "toadstool_dark" },
    green_mushroomhat = { "toadstool", "toadstool_dark" },
    blue_mushroomhat = { "toadstool", "toadstool_dark" },
    mushroom_light = { "toadstool", "toadstool_dark" },
    mushroom_light2 = { "toadstool", "toadstool_dark" },
    sleepbomb = { "toadstool_dark" },
    trident = { "malbatross" },
    wagpunkhat = { "daywalker2", "collapse_small" },
    armorwagpunk = { "daywalker2", "collapse_small" },
    chestupgrade_stacksize = { "daywalker2", "collapse_small" },
    wagpunkbits_kit = { "daywalker2", "collapse_small" },
    antlionhat = { "antlion" },
    townportal = { "antlion" },
    support_pillar_dreadstone_scaffold = { "daywalker" },
}

-- Dedicated servers replicate their original English names even to translated clients.
local blueprint_english = {
    dragonflyfurnace = "Scaled Furnace", bundlewrap = "Bundling Wrap",
    armordreadstone = "Dreadstone Armor", dreadstonehat = "Dreadstone Helm",
    wall_dreadstone_item = "Dreadstone Wall", red_mushroomhat = "Red Funcap",
    green_mushroomhat = "Green Funcap", blue_mushroomhat = "Blue Funcap",
    mushroom_light = "Mushlight", mushroom_light2 = "Glowcap", sleepbomb = "Napsack",
    trident = "Strident Trident", wagpunkhat = "W.A.R.B.I.S. Head Gear",
    armorwagpunk = "W.A.R.B.I.S. Armor", chestupgrade_stacksize = "Elastispacer",
    wagpunkbits_kit = "Auto-Mat-O-Chanic", antlionhat = "Turf-Raiser Helm",
    townportal = "The Lazy Deserter", support_pillar_dreadstone_scaffold = "Dreadstone Pillar Scaffold",
}

-- Existing material labels remain useful without claiming that they just dropped.
M.materials = { gears = true, thulecite = true, livinglog = true, mandrake = true,
    purebrilliance = true, horrorfuel = true, dreadstone = true, opalpreciousgem = true,
    yellowgem = true, orangegem = true, greengem = true, redgem = true, bluegem = true, purplegem = true }
M.effects = { rock_break_fx = true, bomb_lunarplant_explode_fx = true, explode_small = true,
    explode_small_slurtle = true, mining_fx = true, collapse_small = true }
M.sources = {}
for _, catalog in ipairs({ M.items, M.blueprints }) do
    for _, sources in pairs(catalog) do
        for _, source in ipairs(sources) do M.sources[source] = true end
    end
end

function M.matches(prefab, recipe, source)
    for _, candidate in ipairs(recipe and M.blueprints[recipe] or M.items[prefab] or {}) do
        if candidate == source then return true end
    end
    return false
end

function M.blueprint_names(G)
    local result, names = {}, G.STRINGS and G.STRINGS.NAMES or {}
    local function add(name, recipe)
        if name then result[name] = result[name] == nil and recipe or result[name] == recipe and recipe or false end
    end
    for recipe, name in pairs(blueprint_english) do
        if G.AllRecipes and G.AllRecipes[recipe] then add(name .. " Blueprint", recipe) end
    end
    for recipe in pairs(G.AllRecipes or {}) do
        local name = names[string.upper(recipe)]
        if name then
            if names.BLUEPRINT then add(name .. " " .. names.BLUEPRINT, recipe) end
            if names.BLUEPRINT_RARE and G.subfmt then add(G.subfmt(names.BLUEPRINT_RARE, { item = name }), recipe) end
        end
    end
    return result
end

return M
