-- The picker owns availability; each intent owns its completion and item-use policy.
local M = { policies = {} }
local modes = { once = true, count = true, work = true, fish = true }
function M.register(id, policy)
    assert(type(id) == "string" and id ~= "" and type(policy) == "table" and modes[policy.mode], "Invalid work policy")
    assert(policy.actor == nil or policy.actor == "ghost", "Invalid work actor")
    assert(policy.accept == nil or type(policy.accept) == "function", "Invalid work predicate")
    assert(policy.ongoing == nil or type(policy.ongoing) == "function", "Invalid work completion")
    assert(policy.ready == nil or type(policy.ready) == "function", "Invalid work readiness")
    if policy.followup then
        local next = policy.followup
        assert(type(next) == "table" and type(next.action) == "string"
            and (type(next.tag) == "string" or type(next.prefab) == "string"), "Invalid work followup")
    end
    if policy.targets then
        assert(type(policy.targets) == "table", "Invalid work targets")
        for scope, allowed in pairs(policy.targets) do
            assert((scope == "entity" or scope == "self" or scope == "player") and type(allowed) == "boolean", "Invalid work target")
        end
    end
    M.policies[id] = policy
end
local function define(ids, mode)
    for id in ids:gmatch("%S+") do M.register(id, { mode = mode }) end
end
define([[PICKUP PICK HARVEST SHAVE CHECKTRAP RESETMINE PLANT PLANTSOIL REPAIR_LEAK
    DRY STORE TAKEITEM TAKESINGLEITEM FILL INTERACT_WITH PLANTREGISTRY_RESEARCH
    COMBINESTACK SCYTHE NABBAG OCEAN_TRAWLER_LOWER OCEAN_TRAWLER_RAISE OCEAN_TRAWLER_FIX
    UNWRAP TURNON TURNOFF BOTTLE BRUSH UNSADDLE DISMANTLE DECORATEVASE DRAW ERASE_PAPER
    TERRAFORM_REMOVE TEACH CASTSPELL ACTIVATE HAUNT RAISE_SAIL LOWER_SAIL_BOOST START_PUSHING DRAW_FROM_DECK]], "once")
define([[FERTILIZE GIVE FEED ADDFUEL ADDWETFUEL HEAL REPAIR SEW ADDCOMPOSTABLE COOK
    UPGRADE POUR_WATER APPLYPRESERVATIVE BOAT_CANNON_LOAD_AMMO ADD_CARD_TO_DECK EAT READ FEEDPLAYER]], "count")
define("CHOP MINE DIG HAMMER NET REMOVELUNARBUILDUP ATTACK", "work")
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
M.policies.CHOP.followup = { action = "DIG", tag = "stump", permission = "clear_stumps" }
M.policies.MINE.followup = { action = "MINE", source = "spiderhole", prefab = "spiderhole_rock" }
M.policies.FILL.refills = true
M.policies.FISH.expand = false
M.policies.HEAL.targets = { entity = true, self = true, player = true }
M.policies.EAT.targets = { entity = true, self = true }
M.policies.EAT.removes_target = true
M.policies.READ.targets = { self = true }
M.policies.TEACH.targets = { self = true }
M.policies.TEACH.accept = function(action) return action.invobject and action.invobject.prefab == "mapspotrevealer" end
M.policies.CASTSPELL.targets = { entity = true, self = true }
M.policies.CASTSPELL.consumes = true
M.policies.FEEDPLAYER.targets = { player = true }
M.policies.HAUNT.actor = "ghost"
M.policies.HAUNT.removes_target = true
M.policies.ACTIVATE.removes_target = true
M.policies.START_PUSHING.moves_target = true
M.policies.DRAW_FROM_DECK.product = "playing_card"
M.policies.DRAW_FROM_DECK.collect = true
M.policies.ATTACK.accept = function(action) return action.target and action.target:HasTag("wall") end
M.policies.ATTACK.ongoing = function(target)
    local health = target.replica and target.replica.health
    return target:HasTag("wall") and not target:HasTag("dead") and (not health or not health:IsDead())
end
M.policies.LOWER_SAIL_BOOST.mode = "work"
M.policies.LOWER_SAIL_BOOST.ongoing = function(target) return target:HasTag("sailraised") end
M.policies.LOWER_SAIL_BOOST.ready = function(player) return player:HasTag("is_furling") end
M.policies.ACTIVATE.accept = function(action)
    local target = action.target
    return target and (target.prefab == "dirtpile" or target.prefab == "winona_catapult")
end
function M.accept(action, player)
    local policy = action and action.action and M.policies[action.action.id]
    if not policy or (policy.actor == "ghost") ~= (player:HasTag("playerghost") == true) then return false end
    local target = action.target or player
    local scope = target == player and "self" or target:HasTag("player") and "player" or "entity"
    if policy.targets and not policy.targets[scope] or not policy.targets and scope ~= "entity" then return false end
    if policy.accept then
        local ok, accepted = pcall(policy.accept, action, player)
        if not ok or not accepted then return false end
    end
    return true
end

function M.expand(id, target, player)
    return M.policies[id] and M.policies[id].expand ~= false and target ~= player and not target:HasTag("player")
end

function M.workable(target, action, button)
    if not target:IsValid() then return false end
    local policy = M.policies[action.id]
    if policy and policy.ongoing then
        return policy.ongoing(target) == true
    end
    return target:IsActionValid(action, button == "right")
end

return M
