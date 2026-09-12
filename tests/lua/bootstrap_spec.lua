local root, scenario = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, assert(arg[3]))
require("class")

local outputs = {}
nolineprint = function(value) outputs[#outputs + 1] = value end
local encoded = io.read("*a")
local configuration_callback
TheSim = {
    LuaPrintRemote = function() error("bootstrap does not send RPC replies") end,
    GetNumLaunches = function() return scenario == "invalid_generation" and 0.5 or 7 end,
    GetPersistentString = function(self, path, callback)
        assert(path == "../dst_server_driver.json")
        if scenario == "configuration_read_failure" then error("SECRET_TOKEN private chat", 0) end
        if scenario == "delayed_configuration" then configuration_callback = callback
        else callback(encoded ~= "", encoded) end
    end,
}
ExecuteConsoleCommand = function() error("bootstrap does not execute console commands") end
os.getenv = nil -- DST's native sandbox does not expose getenv.
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
Networking_ModOutOfDateAnnouncement = function(...) return ... end
Networking_Say = function(...) return ... end
Networking_Announcement = function(...) return ... end
Networking_SkinAnnouncement = function(...) return ... end
Networking_SystemMessage = function(...) return ... end
Networking_RollAnnouncement = function(...) return ... end
OnSimPaused = function(...) return ... end
OnSimUnpaused = function(...) return ... end
MAX_VOTE_OPTIONS = 4
package.loaded.usercommands = { FinishVote = function() end }
Shard_UpdateWorldState = function(...) return ... end
AllPlayers, Ents = {}, {}
local native_failure = {}
local next_guid = 0

local function entity(name)
    next_guid = next_guid + 1
    local inst = {
        prefab = name, GUID = next_guid, components = {}, replica = {}, listeners = {},
        state = { cycles = 1 },
        Transform = { GetWorldPosition = function() return 1, 0, 2 end },
        IsValid = function() return true end,
        HasTag = function(self, tag) return tag == "player" and self.userid ~= nil end,
        ListenForEvent = function(self, event, callback)
            if scenario == "connections_install_failure" and event == "ms_clientauthenticationcomplete" then
                error("SECRET_TOKEN private chat", 0)
            end
            self.listeners[event] = self.listeners[event] or {}
            table.insert(self.listeners[event], callback)
        end,
        PushEvent = function(self, event, data)
            for _, callback in ipairs(self.listeners[event] or {}) do callback(self, data) end
        end,
        WatchWorldState = function()
            if scenario == "optional_failure" then error("SECRET_TOKEN private chat", 0) end
        end,
        DoStaticPeriodicTask = function(self, period, callback, delay)
            if scenario == "connections_start_failure" then error("SECRET_TOKEN private chat", 0) end
            assert(period == 60 and delay == 0 and self.meta ~= nil)
            assert(self.presence_task == nil, "only one snapshot task per world")
            self.presence_task = { callback = callback, Cancel = function(task) task.cancelled = true end }
            return self.presence_task
        end,
        AddComponent = function(self, name)
            assert(name == "dst_server_runtime" and self.meta == nil, "install before metadata is loaded")
            if scenario ~= "delayed_configuration" then
                local starting = json.decode(outputs[1]:sub(12))
                assert(starting.generation == 7 and starting.health == nil, "starting must precede component installation")
            end
            if scenario == "component_failure" then error("SECRET_TOKEN private chat", 0) end
            self.components[name] = require("components/" .. name)(self)
        end,
    }
    Ents[inst.GUID] = inst
    return inst
end

SpawnPrefabFromSim = function(name)
    if name == "native_error" then error(native_failure, 0) end
    if name == "missing" then return -1 end
    if name == "existing" then return TheWorld.GUID end
    local inst = entity(name)
    if name == "world" or name == "client" then
        TheWorld = inst
        inst.ismastersim = name == "world"
        inst.ismastershard = inst.ismastersim
    end
    if TheWorld ~= nil then TheWorld:PushEvent("entity_spawned", inst) end
    return inst.GUID
end

local original = SpawnPrefabFromSim
local bootstrap = require("dst_server.bootstrap")
bootstrap.start()
local wrapped = SpawnPrefabFromSim
bootstrap.start()
assert(SpawnPrefabFromSim == wrapped, "duplicate bootstrap must not stack wrappers")

local function controls()
    local records = {}
    for _, line in ipairs(outputs) do
        assert(not line:find("SECRET_TOKEN", 1, true) and not line:find("private chat", 1, true))
        assert(not line:find("[\r\n%z]") and #line <= 64 * 1024)
        if line:sub(1, 11) == "DST_DRIVER|" then records[#records + 1] = json.decode(line:sub(12)) end
    end
    return records
end

if scenario == "configuration_failure" or scenario == "invalid_generation" or scenario == "configuration_read_failure" then
    assert(wrapped ~= original and #outputs == 1)
    local failed = controls()[1]
    assert(failed.nonce == nil and failed.generation == nil and failed.error == "configuration_failed")
elseif scenario == "native_results" then
    assert(SpawnPrefabFromSim("missing") == -1)
    assert(SpawnPrefabFromSim("tree") == 1)
    assert(SpawnPrefabFromSim("client") == 2 and next(TheWorld.components) == nil)
    local ok, failure = pcall(SpawnPrefabFromSim, "native_error")
    assert(not ok and failure == native_failure and #outputs == 0)
else
    if scenario == "installation_failure" then Networking_ModOutOfDateAnnouncement = nil end
    local guid = SpawnPrefabFromSim("world")
    assert(guid == TheWorld.GUID)
    assert(SpawnPrefabFromSim("existing") == guid)
    local component = TheWorld.components.dst_server_runtime
    if scenario == "component_failure" then
        assert(component == nil)
    else
        assert(component ~= nil)
        TheWorld.meta = { session_identifier = "SESSION" }
        assert(TheWorld.listeners.master_worldvoterupdate == nil, "network not yet created")
        TheWorld.net = { components = { worldvoter = { OnUpdate = function() end } } }
        if scenario == "votes_install_failure" then TheWorld.net.components.worldvoter = nil end
        if scenario == "publication_failure" then
            require("dst_server").health = function() return { oversized = string.rep("x", 64 * 1024) } end
        end
        component:OnPostInit()
        component:OnPostInit()
        if scenario == "delayed_configuration" then
            assert(#outputs == 0 and component.installed == nil)
            configuration_callback(true, encoded)
            configuration_callback(false) -- Duplicate callback cannot undo readiness.
        end
    end
    local records = controls()
    assert(#records == 2 and records[1].generation == 7)
    local result = records[2]
    if scenario == "component_failure" or scenario == "installation_failure" then
        assert(result.error == "installation_failed" and result.health == nil and result.generation == 7)
    elseif scenario == "publication_failure" then
        assert(result.error == "publication_failed" and result.health == nil and result.generation == 7)
    else
        assert(result.health.generation == 7 and result.error == nil)
        local expected = scenario == "off" and "disabled" or scenario == "optional_failure" and "degraded" or "active"
        if scenario == "connections_install_failure" or scenario == "connections_start_failure" then expected = "degraded" end
        if scenario == "votes_install_failure" then expected = "degraded" end
        assert(result.health.telemetry_status == expected)
        if expected == "degraded" then
            assert(component.installed and result.health.errors == 1)
            assert(result.health.last_error.stage == (scenario == "connections_install_failure"
                and "connections.install" or scenario == "optional_failure" and "world.install"
                or scenario == "votes_install_failure" and "votes.install" or "connections.start"))
        end
        local votes_installed = scenario ~= "off" and scenario ~= "votes_install_failure"
        assert((TheWorld.listeners.master_worldvoterupdate ~= nil) == votes_installed)
        if votes_installed then assert(#TheWorld.listeners.master_worldvoterupdate == 1) end
        assert(require("dst_server").call("health").generation == 7)
        if scenario == "connections_start_failure" then
            assert(component.presence_task == nil)
        else
            assert(component.presence_task == TheWorld.presence_task)
            component:OnRemoveFromEntity()
            assert(component.presence_task.cancelled)
        end
        if scenario == "active" or scenario == "off" then
            local player = entity("wilson")
            player.userid = "KU_TEST"
            AllPlayers = { player }
            TheWorld:PushEvent("ms_playerjoined", player)
            player:PushEvent("ms_skilltreeinitialized")
            local loaded = json.decode(outputs[#outputs]:sub(10))
            assert(loaded.event == "dst.player.loaded" and loaded.generation == 7)
            assert(loaded.data.player.userid == player.userid)
        end
    end
end

controls()
for _, line in ipairs(outputs) do io.write(line, "\n") end
io.write("ok\n")
