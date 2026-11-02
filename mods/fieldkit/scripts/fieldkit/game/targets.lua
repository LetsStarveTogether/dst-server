local Geometry = require("fieldkit/rules/geometry")
local M = {}
local readers = {}

-- A reader returns nil for other families, or a description with phase=false when unreadable.
function M.register(name, read)
    assert(type(name) == "string" and name ~= "" and type(read) == "function", "Invalid target reader")
    for _, reader in ipairs(readers) do
        if reader.name == name then reader.read = read; return end
    end
    table.insert(readers, 1, { name = name, read = read })
end

local function animation(entity, names)
    local anim = entity.AnimState
    if not anim or not anim.IsCurrentAnimation then return end
    for _, name in ipairs(names) do if anim:IsCurrentAnimation(name) then return true end end
end

local function build(entity)
    local anim = entity.AnimState
    return anim and anim.GetBuild and anim:GetBuild() or nil
end

local phases = { "short", "normal", "tall", "old", "young", "sapling" }
local sizes = {}
for _, phase in ipairs(phases) do
    local names = {}
    for _, prefix in ipairs({ "idle_", "sway1_loop_", "sway2_loop_", "chop_", "hit_", "burnt_",
        "chop_burnt_", "idle_chop_burnt_", "drop_leaves_", "grow_leaves_" }) do
        names[#names + 1] = prefix .. phase
    end
    names[#names + 1] = "grow_seed_to_" .. phase
    for _, from in ipairs(phases) do names[#names + 1] = "grow_" .. from .. "_to_" .. phase end
    sizes[#sizes + 1] = { phase = phase, animations = names }
end
local size_builds = {
    evergreen_new = true, evergreen_new_2 = true, twiggy_build = true,
    tree_leaf_trunk_build = true, moon_tree = true, palmcone_build = true,
    tree_rock_normal = true, marbleshrub = true, evergreen_living_wood_growable = true,
    wintertree_palmcone = true,
}
local ocean_builds = { oceantree_short = "short", oceantree_normal = "normal",
    oceantree_tall = "tall", oceantree_tall_jammed_build = "tall" }

M.register("size", function(entity)
    local model = build(entity)
    if ocean_builds[model] then return { phase = ocean_builds[model] } end
    if entity:HasTag("rock_tree") and entity:HasTag("boulder") then return { phase = "fallen" } end
    local known = size_builds[model]
    if not known and not entity:HasTag("tree") and not entity:HasTag("boulder")
        and not entity:HasTag("winter_tree") then return end
    if entity:HasTag("winter_tree") and animation(entity, { "idle" }) then return { phase = "empty" } end
    for _, size in ipairs(sizes) do
        if animation(entity, size.animations) then return { phase = size.phase } end
    end
    if known then return { phase = false } end
end)

local fruit_stages = {
    { phase = "empty", animations = { "idle1", "picked", "crumble" } },
    { phase = "growing", animations = { "idle2", "grow1" } },
    { phase = "ripe", animations = { "idle3", "grow2" } },
    { phase = "overripe", animations = { "idle4", "grow3" } },
    { phase = "barren", animations = { "dead1", "idle1_to_dead1", "dead1_to_idle1" } },
}
M.register("fruit", function(entity)
    local model = build(entity)
    if model ~= "rock_avocado_build" and model ~= "rock_avocado_diseased_build" then return end
    for _, stage in ipairs(fruit_stages) do
        if animation(entity, stage.animations) then return { phase = stage.phase } end
    end
    return { phase = false }
end)

M.register("seedling", function(entity)
    if entity:HasTag("silviculture") then
        if entity:HasTag("seedstage") then return { phase = "seed" } end
        if animation(entity, { "sprout_idle", "grow_seed_to_sprout" }) then return { phase = "sprout" } end
        return { phase = false }
    elseif build(entity) == "livingtree_root" then
        if animation(entity, { "idle_planted_flask" }) then return { phase = "seed" } end
        if animation(entity, { "idle_planted", "burst" }) then return { phase = "sprout" } end
        return { phase = false }
    end
end)

M.register("plant_registry", function(entity)
    local definition = entity.plant_def or entity.weed_def
    if not definition or type(entity.GetResearchStage) ~= "function" then return end
    local phase = entity:GetResearchStage()
    local info = definition.plantregistryinfo and definition.plantregistryinfo[phase]
    return { phase = (info or definition.is_randomseed) and phase or false,
        crop = entity.plant_def ~= nil, weed = entity.weed_def ~= nil,
        mature = info and info.fullgrown == true, rotten = info and info.is_rotten == true }
end)

function M.describe(entity)
    if not entity or not entity:IsValid() then return end
    local result = { prefab = entity.prefab, stump = entity:HasTag("stump"),
        burnt = entity:HasTag("burnt"), barren = entity:HasTag("barren"), withered = entity:HasTag("withered") }
    -- Stump work no longer depends on the former tree's size.
    if result.stump then return result end
    for _, reader in ipairs(readers) do
        local ok, value = pcall(reader.read, entity)
        if not ok or value ~= nil then
            result.reader = reader.name
            local valid = ok and type(value) == "table"
            local phase = valid and value.phase
            result.phase = (type(phase) == "string" or Geometry.finite(phase)) and phase or false
            if valid then
                result.crop, result.weed = value.crop == true, value.weed == true
                result.mature, result.rotten = value.mature == true, value.rotten == true
            end
            return result
        end
    end
    return result
end

function M.matches(sample, entity)
    if not sample or not entity or sample.prefab ~= entity.prefab or sample.phase == false then return false end
    local current = M.describe(entity)
    if not current then return false end
    for _, key in ipairs({ "reader", "phase", "stump", "burnt", "barren", "withered" }) do
        if sample[key] ~= current[key] then return false end
    end
    return true
end

function M.accepts(entity, filter)
    if filter == nil or filter == "all_targets" then return true end
    local state = M.describe(entity)
    if not state then return false end
    if filter == "mature_crops" then return state.crop and state.mature and not state.rotten end
    if filter == "weeds" then return state.weed == true end
    if filter == "stumps" then return state.stump end
    return false
end

return M
