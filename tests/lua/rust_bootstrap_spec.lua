local root, scripts, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local configuration, records, shutdowns, continued = nil, {}, 0, false
local snapshot = 2
SpawnPrefabFromSim = function() return 0 end
Ents = {}
TheSim = {
    GetNumLaunches = function() return 7 end,
    GetPersistentString = function(_, path, callback)
        assert(path == "../dst_server_driver.json")
        configuration = callback
    end,
    LuaPrintRemote = function(_, line)
        if line:sub(1, 11) == "DST_DRIVER|" then return end
        assert(line:sub(1, 12) == "DST_CONTROL|" and line:sub(-1) == "\n" and #line <= 65536)
        records[#records + 1] = require("dst_server.wire").decode(line:sub(13, -2))
    end,
}
ExecuteConsoleCommand = function() end
StartDedicatedServer = function() assert(scenario == "default" or scenario == "load" or scenario == "observer_failure") end
ShardIndex = { Load = function(_, callback) return callback("native", nil, 7) end }
TheNet = {
    GetIsServer = function() return true end,
    GetCurrentSnapshot = function() return snapshot end,
    GetWorldSessionFile = function() return "save/session/SESSION/0000000001" end,
    IsOnlineMode = function() return true end,
    ListSnapshots = function() return { { snapshot_id = 1, world_file = "save/session/SESSION/0000000001" } }, false end,
    TruncateSnapshots = function() error("catalog must not truncate", 0) end,
}
Shutdown = function() shutdowns = shutdowns + 1 end
SaveGame = function(_, callback) snapshot = snapshot + 1; if callback ~= nil then return callback() end end
local original_save = SaveGame
local installs = 0
package.loaded["dst_server.load_observer"] = {install = function()
    installs = installs + 1
    if scenario == "observer_failure" then error("unavailable load instrumentation") end
    return scenario ~= "recovery_observer_failure"
end}
require("dst_server.bootstrap").start()
StartDedicatedServer()
local index = { GetSession = function() return "SESSION" end }
ShardIndex.Load(index, function(a, b, c)
    assert(a == "native" and b == nil and c == 7)
    continued = true
end)
assert(not continued, "configuration must resolve before native world loading continues")
local control = (scenario == "catalog" or scenario == "recovery_observer_failure") and ',"control":{"recovery":{"mode":"catalog"}}'
    or scenario == "load" and ',"control":{}'
    or scenario == "invalid" and ',"control":{"recovery":{"mode":"apply"}}' or ""
configuration(true, '{"driver":{"nonce":"01ARZ3NDEKTSV4RRFFQ69G5FAV","profile":"off"' .. control .. '}}')
assert(installs == (scenario == "invalid" and 0 or 1))
if scenario == "catalog" then
    assert(not continued and shutdowns == 1 and #records == 1)
    assert(records[1].event == "recovery_catalog" and records[1].session_id == "SESSION")
elseif scenario == "invalid" or scenario == "recovery_observer_failure" then
    assert(not continued and shutdowns == 1 and #records == 1 and records[1].event == "recovery_failed")
    if scenario == "recovery_observer_failure" then assert(records[1].error == "installation_failed") end
else
    assert(continued and shutdowns == 0)
    assert(SaveGame == original_save and #records == 0)
    if scenario == "observer_failure" then
        assert(require("dst_server.state").faults["load_observer.install"] == "installation_failed")
    end
end
for _, record in ipairs(records) do
    assert(record.v == nil)
    if scenario == "invalid" then
        assert(record.nonce == json.null and record.generation == json.null)
    else
        assert(record.nonce == "01ARZ3NDEKTSV4RRFFQ69G5FAV" and record.generation == 7)
    end
end
io.write("ok\n")
