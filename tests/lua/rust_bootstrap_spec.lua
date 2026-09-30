local root, scripts, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local configuration, records, shutdowns, continued = nil, {}, 0, false
local snapshot = 2
nolineprint = function() end
SpawnPrefabFromSim = function() return 0 end
Ents = {}
TheSim = {
    GetNumLaunches = function() return 7 end,
    GetPersistentString = function(_, path, callback)
        assert(path == "../dst_server_driver.json")
        configuration = callback
    end,
    LuaPrintRemote = function(_, line)
        assert(line:sub(1, 12) == "DST_CONTROL|" and line:sub(-1) == "\n" and #line <= 65536)
        records[#records + 1] = json.decode(line:sub(13, -2))
    end,
}
ExecuteConsoleCommand = function() end
StartDedicatedServer = function() assert(scenario == "default" or scenario == "save") end
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
require("dst_server.bootstrap").start()
StartDedicatedServer()
local index = { GetSession = function() return "SESSION" end }
ShardIndex.Load(index, function(a, b, c)
    assert(a == "native" and b == nil and c == 7)
    continued = true
end)
assert(not continued, "configuration must resolve before native world loading continues")
local control = scenario == "catalog" and ',"control":{"recovery":{"mode":"catalog"}}'
    or scenario == "save" and ',"control":{"observe_saves":true}'
    or scenario == "invalid" and ',"control":{"recovery":{"mode":"apply"}}' or ""
configuration(true, '{"nonce":"01ARZ3NDEKTSV4RRFFQ69G5FAV","profile":"off","actions":[]' .. control .. '}')
if scenario == "catalog" then
    assert(not continued and shutdowns == 1 and #records == 1)
    assert(records[1].event == "recovery_catalog" and records[1].session_id == "SESSION")
elseif scenario == "invalid" then
    assert(not continued and shutdowns == 1 and #records == 1 and records[1].event == "recovery_failed")
else
    assert(continued and shutdowns == 0)
    if scenario == "save" then
        TheWorld = { meta = { session_identifier = "SESSION" } }
        assert(SaveGame(false, function() return "native result" end) == "native result")
        assert(#records == 2 and records[2].event == "save_complete" and records[2].snapshot_id == 2)
    else
        assert(SaveGame == original_save and #records == 0)
    end
end
for _, record in ipairs(records) do
    assert(record.v == 1)
    if scenario ~= "invalid" then
        assert(record.nonce == "01ARZ3NDEKTSV4RRFFQ69G5FAV" and record.generation == 7)
    end
end
io.write("ok\n")
