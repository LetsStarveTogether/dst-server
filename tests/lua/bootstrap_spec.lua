local root, scripts, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
require("class")

local profile = scenario == "optional_failure" and "timeline" or "off"
local nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
local configuration = json.encode({driver = { nonce = nonce, profile = profile }})
local configure, records, ready_writes = nil, {}, 0
TheSim = {
    GetNumLaunches = function() return 7 end,
    GetPersistentString = function(_, path, callback)
        assert(path == "../dst_server_driver.json")
        if scenario == "delayed_configuration" then configure = callback
        else callback(true, configuration) end
    end,
}
dofile(root .. "/tests/lua/capture_remote.lua")(function(line)
    assert(line:sub(1, 11) == "DST_DRIVER|" and #line < 65536)
    local record = require("dst_server.wire").decode(line:sub(12))
    assert(record.nonce == nonce and record.v == nil)
    if record.health then
        ready_writes = ready_writes + 1
        if scenario == "write_before" then error("private write failure") end
        if scenario == "optional_failure" then io.write(line, "\n") end
    end
    records[#records + 1] = record
    if record.health and scenario == "write_after" then error("private write failure") end
end)
GetTick = function() return 1 end
GetTimeReal = function() return 20 end
ExecuteConsoleCommand = function() end
OnSimPaused, OnSimUnpaused = function() end, function() end
Networking_ModOutOfDateAnnouncement = function() end
Networking_Say = function() end
MAX_CHAT_INPUT_LENGTH = 150
string.utf8len = function(value) return #value end
AllPlayers, Ents = {}, {}
local listeners, timers = {}, 0
SpawnPrefabFromSim = function(name)
    if name == "rock" then return 2, nil, name, nil end
    assert(name == "world")
    TheWorld = {
        GUID = 1, prefab = name, ismastersim = true, ismastershard = true, state = { cycles = 1 },
        components = {}, HasTag = function() return false end, IsValid = function() return true end,
        ListenForEvent = function(_, event, callback)
            if scenario == "connections_install_failure" and event == "ms_clientauthenticationcomplete" then
                error("private listener failure")
            end
            listeners[event] = callback
        end,
        WatchWorldState = function() error("optional world observer failed") end,
        DoStaticPeriodicTask = function(_, period, _, delay)
            assert(period == 60 and delay == 0)
            timers = timers + 1
            return { Cancel = function() timers = timers - 1 end }
        end,
        AddComponent = function(self, component)
            self.components[component] = require("components/" .. component)(self)
        end,
    }
    Ents[1] = TheWorld
    return TheWorld.GUID, nil, name, nil
end

local bootstrap = require("dst_server.bootstrap")
local original = SpawnPrefabFromSim
bootstrap.start()
local spawn = SpawnPrefabFromSim
bootstrap.start()
assert(SpawnPrefabFromSim == spawn)
local expected = original
if scenario == "delayed_configuration" then
    expected = function(...) return spawn(...) end
    SpawnPrefabFromSim = expected
end
local function check_spawn(guid, prefab, ...)
    local value, hole, name, trailing = ...
    assert(select("#", ...) == 4 and value == guid and hole == nil and name == prefab and trailing == nil)
end
check_spawn(1, "world", SpawnPrefabFromSim("world"))
assert(SpawnPrefabFromSim == expected)
setmetatable(Ents, { __index = function() error("ordinary spawns must not inspect Ents") end })
check_spawn(2, "rock", SpawnPrefabFromSim("rock"))
local runtime = TheWorld.components.dst_server_runtime
TheWorld.meta = {session_identifier = "SESSION"}
TheWorld.net = {components = {worldvoter = {}}}
runtime:OnPostInit()
if configure then
    assert(#records == 0 and runtime.installed == nil)
    configure(true, configuration)
    configure(false)
end
runtime:OnPostInit()
assert(ready_writes == 1 and records[1].generation == 7)
local result = records[#records]
if scenario == "write_before" or scenario == "write_after" then
    assert(result.error == "publication_failed")
else
    local health = assert(result.health)
    assert(health.generation == 7 and health.profile == profile and health.protocol == nil)
    assert(health.control_ready == (scenario ~= "connections_install_failure"))
    assert(health.telemetry_status == (profile == "off" and "disabled" or "degraded"))
    assert(timers == (profile == "off" and 0 or 1))
    if scenario == "off" then assert(listeners.master_worldvoterupdate, "vote control still observes native state") end
    runtime:OnRemoveFromEntity()
    assert(timers == 0)
end
io.write("ok\n")
