local root, handlers, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
local scripts = os.getenv("DST_SERVER_TEST_SCRIPTS") or root .. "/dst-scripts/scripts"
package.path = root .. "/src/dst_server/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
json = require("json")
local native = dofile(handlers)
local state = require("dst_server.state")
local connections = require("dst_server.connections")
local outputs = {}
nolineprint = function(line) outputs[#outputs + 1] = line end
GetTick = function() return 10 end
local now = 20
GetTimeReal = function() return now end

state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 4
state.requested_profile = scenario == "off" and "off" or scenario == "critical" and "critical" or "history"
state.telemetry_active = scenario ~= "off" and scenario ~= "optional_failure"
if scenario == "optional_failure" then
    state.errors = 1
    state.last_error = { stage = "install", message = "installation_failed", count = 1 }
end

local clients = { {userid = "KU_A", name = "PRIVATE_NAME"}, {userid = "KU_A"} }
local read_fails = false
TheNet = {
    GetServerMaxPlayers = function() return 9 end,
    GetServerIsClientHosted = function() return false end,
    GetClientTable = function()
        if read_fails then error("PRIVATE_NAME secret", 0) end
        local result = { {userid = "DEDICATED_HOST", performance = {}} }
        for _, client in ipairs(clients) do result[#result + 1] = client end
        return result
    end,
}
GetPlayerClientTable = native.GetPlayerClientTable

local function player(userid, guid, valid)
    return { userid = userid, GUID = guid, IsValid = function() return valid end }
end
AllPlayers = {
    player("KU_A", 21, true), player("KU_A", 20, true),
    player("KU_REMOVED", 22, false), player(nil, 23, true),
}
local listeners = {}
local task
TheWorld = {
    meta = { session_identifier = "SESSION" }, state = { cycles = 2 },
    IsValid = function() return true end,
    ListenForEvent = function(_, name, callback)
        assert(listeners[name] == nil)
        listeners[name] = callback
    end,
    PushEvent = function(self, name, data)
        if listeners[name] ~= nil then listeners[name](self, data) end
    end,
    DoStaticPeriodicTask = function(_, period, callback, delay)
        assert(period == 60 and delay == 0)
        assert(task == nil)
        task = { callback = callback }
        return task
    end,
    DoPeriodicTask = function() error("simulation scheduling pauses with the game") end,
}

connections.install(TheWorld)
local scheduled = connections.start(TheWorld)
assert(#outputs == 0, "initial snapshot must run after the startup stack finishes")
    assert(scheduled == task)
    if scenario == "snapshot_failure" then
        read_fails = true
        task.callback()
        assert(json.decode(outputs[1]:sub(10)).event == "dst.telemetry.error")
        read_fails = false
    end
    task.callback()
    local initial = json.decode(outputs[#outputs]:sub(10))
    assert(initial.event == "dst.server.presence" and initial.data.reason == "startup")
    assert(#initial.data.clients == 1 and initial.data.clients[1] == "KU_A")
    assert(#initial.data.players == 2 and initial.data.players[1].guid == 20)

    if scenario == "invalid_authentication" then
        native.ClientAuthenticationComplete(nil)
        assert(json.decode(outputs[#outputs]:sub(10)).event == "dst.telemetry.error")
    end
    clients[#clients + 1] = {userid = "KU_LOBBY"}
    native.ClientAuthenticationComplete("KU_LOBBY")
    native.ClientDisconnected("KU_LOBBY")
    table.remove(clients)
    local count = #outputs
    TheWorld:PushEvent("ms_playerleft", AllPlayers[1])
    assert(#outputs == count, "entity departure is not a client disconnection")
    AllPlayers = {}
    now = 60020
    task.callback() -- Static scheduler continues while game simulation is paused.
    local periodic = json.decode(outputs[#outputs]:sub(10))
    assert(periodic.data.reason == "periodic" and #periodic.data.players == 0)
    assert(periodic.data.clients[1] == "KU_A")
for _, line in ipairs(outputs) do
    assert(not line:find("[\r\n%z]") and #line <= 64 * 1024)
    assert(not line:find("PRIVATE_NAME", 1, true) and not line:find("DEDICATED_HOST", 1, true))
    io.write(line, "\n")
end
