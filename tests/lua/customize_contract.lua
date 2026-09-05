local root = assert(arg[1], "repository root is required")
local scripts = root .. "/dst-scripts/scripts/"
package.path = scripts .. "?.lua;" .. package.path
local json = require("json")

local strings = setmetatable({}, {
    __index = function(value) return value end,
    __concat = function() return "" end,
    __tostring = function() return "" end,
})

local locations = {}
local env = setmetatable({
    STRINGS = strings,
    LEVELCATEGORY = { WORLDGEN = "WORLDGEN", SETTINGS = "SETTINGS" },
    SPECIAL_EVENTS = {},
    ModManager = { currentlyloadingmod = nil },
    IsNotConsole = function() return true end,
    IsPS4 = function() return false end,
    print = function() end,
    FunctionOrValue = function(value, ...)
        return type(value) == "function" and value(...) or value
    end,
    deepcopy = function(value) return value end,
}, { __index = _G })

env.table = setmetatable({
    contains = function(values, target)
        for _, value in ipairs(values) do
            if value == target then return true end
        end
        return false
    end,
}, { __index = table })

local function load_script(path)
    local chunk = assert(loadfile(scripts .. path .. ".lua"))
    setfenv(chunk, env)
    return chunk()
end

env.AddLocation = function(value) locations[value.location] = value end
load_script("map/locations")

local tasksets
local startlocations
env.require = function(name)
    if name:match("^map/tasksets/") then
        return load_script(name)
    elseif name == "map/tasksets" then
        return tasksets
    elseif name == "map/startlocations" then
        return startlocations
    elseif name == "map/tasks" then
        return {}
    elseif name == "map/levels" then
        return { GetDataForLocation = function(location) return locations[location] end }
    elseif name == "worldsettings_overrides" then
        return { Pre = setmetatable({}, { __index = function() return true end }), Post = {} }
    end
    error("unexpected require: " .. name)
end

tasksets = load_script("map/tasksets")
startlocations = load_script("map/startlocations")
local customize = load_script("map/customize")

local result = { options = {}, misc = {} }
for _, location in ipairs({ "forest", "cave" }) do
    local options = {}
    for _, option in ipairs(customize.GetOptionsWithLocationDefaults(location, true)) do
        local values = {}
        for _, value in ipairs(option.options) do
            assert(type(value.data) == "string", "built-in option values must be strings")
            values[#values + 1] = value.data
        end
        options[option.name] = { default = option.default, values = values }
    end
    result.options[location] = options
end

for name, forest_default in pairs(locations.forest.overrides) do
    if not customize.IsCustomizeOption(name)
        and customize.GetCategoryForOption(name) == env.LEVELCATEGORY.WORLDGEN
    then
        local cave_default = locations.cave.overrides[name]
        result.misc[name] = {
            forest = { kind = type(forest_default), default = forest_default },
            cave = {
                kind = type(cave_default),
                default = cave_default == nil and json.null or cave_default,
            },
        }
    end
end

print(json.encode_compliant(result))
