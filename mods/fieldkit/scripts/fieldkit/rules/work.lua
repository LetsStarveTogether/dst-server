-- The picker owns availability; each intent owns its completion and item-use policy.
local M = { policies = {} }
local function define(ids, mode)
    for id in ids:gmatch("%S+") do M.policies[id] = { mode = mode } end
end
define([[PICKUP PICK HARVEST SHAVE CHECKTRAP RESETMINE PLANT PLANTSOIL REPAIR_LEAK
    DRY STORE TAKEITEM TAKESINGLEITEM FILL INTERACT_WITH PLANTREGISTRY_RESEARCH
    COMBINESTACK SCYTHE NABBAG OCEAN_TRAWLER_LOWER OCEAN_TRAWLER_RAISE OCEAN_TRAWLER_FIX
    UNWRAP TURNON TURNOFF BOTTLE BRUSH UNSADDLE DISMANTLE DECORATEVASE DRAW ERASE_PAPER
    TERRAFORM_REMOVE]], "once")
define([[FERTILIZE GIVE FEED ADDFUEL ADDWETFUEL HEAL REPAIR SEW ADDCOMPOSTABLE COOK
    UPGRADE POUR_WATER APPLYPRESERVATIVE BOAT_CANNON_LOAD_AMMO ADD_CARD_TO_DECK]], "count")
define("CHOP MINE DIG HAMMER NET REMOVELUNARBUILDUP", "work")
define("FISH REEL", "fish")
for id in ("CHOP MINE DIG HAMMER NET SHAVE FISH SCYTHE NABBAG REMOVELUNARBUILDUP POUR_WATER FILL BRUSH UNSADDLE TERRAFORM_REMOVE"):gmatch("%S+") do
    M.policies[id].tool = true
end
for id in ("CHOP MINE DIG HAMMER NET PICK HARVEST FISH SCYTHE UNWRAP REMOVELUNARBUILDUP BOTTLE DISMANTLE BRUSH TERRAFORM_REMOVE"):gmatch("%S+") do
    M.policies[id].collect = true
end
-- Recipes are only a way to acquire a tool. Every acquired item must pass the picker again.
M.craftable = { "CHOP", "MINE", "DIG", "HAMMER", "NET", "FISH", "REMOVELUNARBUILDUP" }
M.policies.CHOP.recipes = { "axe", "goldenaxe" }
M.policies.MINE.recipes = { "pickaxe", "goldenpickaxe" }
M.policies.DIG.recipes = { "shovel", "goldenshovel" }
M.policies.HAMMER.recipes = { "hammer" }
M.policies.NET.recipes = { "bugnet" }
M.policies.FISH.recipes = { "fishingrod" }
M.policies.REMOVELUNARBUILDUP.recipes = M.policies.MINE.recipes
M.policies.FILL.refills = true
function M.accept(action, player)
    return action and action.action and M.policies[action.action.id]
        and action.target ~= player and not (action.target and action.target:HasTag("player"))
end

function M.workable(target, action, button)
    return target:IsValid() and target:IsActionValid(action, button == "right")
end

return M
