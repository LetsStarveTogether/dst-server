local repository, cluster, expected_path = assert(arg[1]), assert(arg[2]), assert(arg[3])
package.path = repository .. "/dst-scripts/scripts/?.lua;" .. package.path
local json = require("json")
local stream = assert(io.open(expected_path, "r"))
local expected = json.decode(stream:read("*a"))
stream:close()

local function equal(actual, wanted, path)
    assert(type(actual) == type(wanted), path .. ": Lua type changed")
    if type(wanted) ~= "table" then
        assert(actual == wanted, path .. ": value changed")
        return
    end
    for key, value in pairs(wanted) do
        equal(actual[key], value, path .. "." .. tostring(key))
    end
    for key in pairs(actual) do
        assert(wanted[key] ~= nil, path .. ": unexpected key " .. tostring(key))
    end
end

for shard, world in pairs(expected.worlds) do
    equal(dofile(cluster .. "/" .. shard .. "/modoverrides.lua"), expected.mods, shard .. ".mods")
    equal(dofile(cluster .. "/" .. shard .. "/worldgenoverride.lua"), world, shard .. ".world")
end

local downloaded = {}
function ServerModSetup(identifier)
    assert(type(identifier) == "string", "Workshop IDs must remain strings")
    downloaded[#downloaded + 1] = identifier
end
function ServerModCollectionSetup()
    error("the room templates do not declare Workshop collections")
end
dofile(cluster .. "/mods/dedicated_server_mods_setup.lua")
equal(downloaded, expected.downloads, "downloads")
