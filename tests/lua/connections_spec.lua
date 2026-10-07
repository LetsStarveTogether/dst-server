local root, scripts, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local state = require("dst_server.state")
local connections = require("dst_server.connections")
local outputs = {}
OnSimPaused = function() end
OnSimUnpaused = function() end
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

local clients = {
    {userid = "KU_A", netid = "76561198000000002", name = "玩家甲", admin = true, moderator = false}, {userid = "KU_A"},
}
local read_fails = false
local netid = "76561198000000001"
local identity_fails = false
local identity_calls = 0
local profile_calls = 0
TheNet = {
    GetServerMaxPlayers = function() return 9 end,
    GetNetIdForUser = function(_, userid)
        identity_calls = identity_calls + 1
        if identity_fails then error("identity unavailable") end
        return userid == "KU_LOBBY" and netid or nil
    end,
    GetClientTableForUser = function(_, userid)
        profile_calls = profile_calls + 1
        if identity_fails then error("profile unavailable") end
        if userid == "KU_LOBBY" then return {name = "大厅玩家", admin = false, moderator = true} end
    end,
}
GetPlayerClientTable = function()
    if read_fails then error("PRIVATE_ERROR secret", 0) end
    return clients
end

local function player(userid, guid, valid)
    return {
        userid = userid, GUID = guid, prefab = "wilson", IsValid = function() return valid end,
        HasTag = function(_, tag) return tag == "playerghost" and guid == 21 end,
    }
end
AllPlayers = {
    player("KU_A", 21, true), player("KU_A", 20, true),
    player("KU_REMOVED", 22, false), player(nil, 23, true),
}
local listeners = {}
local task
TheWorld = {
    meta = { session_identifier = "SESSION" },
    state = {
        cycles = 2, season = "autumn", phase = "day", moonphase = "full",
        cavephase = "dusk", cavemoonphase = "new", nightmarephase = "calm",
        israining = false, issnowing = false, isacidraining = false, islunarhailing = false,
    },
    HasTag = function(_, tag) return tag == "cave" and scenario == "cave" end,
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
assert(#initial.data.clients == 1 and initial.data.clients[1].userid == "KU_A")
assert(initial.data.clients[1].steam_id == "76561198000000002" and identity_calls == 0)
assert(initial.data.clients[1].name == "玩家甲" and initial.data.clients[1].admin and not initial.data.clients[1].moderator)
assert(profile_calls == 0, "a snapshot reuses the client table already read")
assert(#initial.data.players == 2 and initial.data.players[1].guid == 20)
assert(initial.data.players[1].prefab == "wilson" and not initial.data.players[1].is_ghost)
assert(initial.data.players[2].is_ghost)
assert(initial.data.world.season == "autumn" and initial.data.world.israining == false)
assert(initial.data.world.phase == (scenario == "cave" and "dusk" or "day"))
assert(initial.data.world.moonphase == (scenario == "cave" and "new" or "full"))

if scenario == "metadata" then
    clients[1].name, clients[1].admin, clients[1].moderator = "改名后的玩家", false, true
    TheWorld.state.season, TheWorld.state.israining = "winter", true
    task.callback()
    local changed = json.decode(outputs[#outputs]:sub(10)).data
    assert(changed.clients[1].name == "改名后的玩家" and not changed.clients[1].admin and changed.clients[1].moderator)
    assert(changed.world.season == "winter" and changed.world.israining)
    assert(initial.data.clients[1].name == "玩家甲", "older observations keep the name seen then")
    local previous_clients, previous_players = clients, AllPlayers
    clients, AllPlayers = {}, {}
    for i = 1, 64 do
        clients[i] = {userid = "KU_" .. i, name = string.rep("名", 100), netid = "76561198000000002"}
        AllPlayers[i] = player(clients[i].userid, i, true)
    end
    task.callback()
    local full = json.decode(outputs[#outputs]:sub(10))
    assert(full.event == "dst.server.presence" and #full.data.clients == 64)
    assert(full.data.clients[1].name == string.rep("名", 85), "nickname truncation preserves UTF-8")
    assert(identity_calls == 0 and profile_calls == 0)
    clients, AllPlayers = previous_clients, previous_players
end

if scenario == "invalid_authentication" then
    TheWorld:PushEvent("ms_clientauthenticationcomplete", {})
    assert(json.decode(outputs[#outputs]:sub(10)).event == "dst.server.presence")
end
if scenario == "authentication_output_failure" then
    local wire = require("dst_server.wire")
    local encode = wire.encode
    wire.encode = function(value, limit)
        if value.event == "dst.client.authenticated" then error("encoding failed", 0) end
        return encode(value, limit)
    end
    TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
    local failed = require("dst_server").health()
    assert(failed.telemetry_status == "degraded")
    assert(failed.faults["emit.dst.client.authenticated"] == "encoding_failed")
    TheWorld:PushEvent("ms_clientauthenticationcomplete", {})
    assert(require("dst_server").health().revision == failed.revision, "successful callbacks cannot clear failed output")
    wire.encode = encode
end
clients[#clients + 1] = {userid = "KU_LOBBY"}
TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
local authenticated = json.decode(outputs[#outputs]:sub(10))
assert(authenticated.event == "dst.client.authenticated")
assert(authenticated.data.userid == "KU_LOBBY" and authenticated.data.steam_id == netid)
assert(authenticated.data.name == "大厅玩家" and authenticated.data.admin == false and authenticated.data.moderator)
if scenario == "steam_identity" then
    for _, case in ipairs({
        { netid = "76561197960265729", expected = "76561197960265729" },
        { netid = "76561202255233023", expected = "76561202255233023" },
        {}, { netid = "" }, { netid = "0" }, { netid = "invalid" },
        { netid = "76561197960265728" }, { netid = "76561202255233024" },
        { netid = "12345678901234567" }, { netid = "1000000000000000001" },
        { netid = "7656119800000000x" }, { netid = " 76561198000000001" },
        { netid = 76561198000000001 }, { netid = false },
        { fails = true },
    }) do
        netid, identity_fails = case.netid, case.fails == true
        TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
        authenticated = json.decode(outputs[#outputs]:sub(10))
        assert(authenticated.event == "dst.client.authenticated")
        assert(authenticated.data.userid == "KU_LOBBY")
        assert(authenticated.data.steam_id == case.expected)
        assert(case.expected ~= nil or outputs[#outputs]:find('"steam_id":null', 1, true))
    end
    identity_fails, netid = false, nil
    TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
    assert(json.decode(outputs[#outputs]:sub(10)).data.steam_id == nil)
    clients[#clients].netid = "76561198000000003"
    local calls = identity_calls
    task.callback()
    local late = json.decode(outputs[#outputs]:sub(10)).data.clients[2]
    assert(late.userid == "KU_LOBBY" and late.steam_id == "76561198000000003")
    assert(identity_calls == calls, "the client table supplies the native identity directly")
    authenticated.data.steam_id = late.steam_id
    authenticated.data.name = late.name
end
identity_fails = true
local calls = identity_calls
local profiles = profile_calls
TheWorld:PushEvent("ms_clientdisconnected", {userid = "KU_LOBBY"})
local disconnected = json.decode(outputs[#outputs]:sub(10))
assert(disconnected.event == "dst.client.disconnected")
assert(disconnected.data.userid == "KU_LOBBY" and disconnected.data.steam_id == authenticated.data.steam_id)
assert(identity_calls == calls, "disconnection uses the identity captured before the connection vanished")
assert(disconnected.data.name == authenticated.data.name and profile_calls == profiles)
table.remove(clients)
if scenario == "steam_identity" then
    assert(connections.steam_id("KU_LOBBY") == json.null, "disconnect must evict the identity")
    identity_fails, netid = false, "76561198000000004"
    TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
    assert(json.decode(outputs[#outputs]:sub(10)).data.steam_id == netid)
    netid = nil
    TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
    assert(json.decode(outputs[#outputs]:sub(10)).data.steam_id == nil, "reconnect cannot inherit an old identity")
    TheWorld:PushEvent("ms_clientdisconnected", {userid = "KU_LOBBY"})
    assert(json.decode(outputs[#outputs]:sub(10)).data.steam_id == nil)
end
local count = #outputs
TheWorld:PushEvent("ms_playerleft", AllPlayers[1])
assert(#outputs == count, "entity departure is not a client disconnection")
AllPlayers = {}
now = 60020
task.callback() -- Static scheduler continues while game simulation is paused.
local periodic = json.decode(outputs[#outputs]:sub(10))
assert(periodic.data.reason == "periodic" and #periodic.data.players == 0)
assert(periodic.data.clients[1].userid == "KU_A" and periodic.data.clients[1].steam_id == "76561198000000002")
clients = {}
local presence = connections.presence()
assert(presence.idle_seconds == 0 and presence.observed_seconds == 60)
assert(presence.observation_epoch == 0)
now = now + 30000
presence = connections.presence()
assert(math.abs(presence.idle_seconds - 30) < 1e-9 and presence.observed_seconds == 90)
assert(presence.observation_epoch == 0)
GetTimeReal = function() error("clock unavailable") end
TheWorld:PushEvent("ms_playerleft", {})
GetTimeReal = function() return now end
presence = connections.presence()
assert(presence.observation_epoch == 1 and presence.observed_seconds == 0)
assert(connections.presence().observation_epoch == 1)
TheWorld:PushEvent("ms_clientdisconnected", {userid = "KU_A"})
assert(json.decode(outputs[#outputs]:sub(10)).data.steam_id == nil, "snapshots prune disconnected identities")
for _, line in ipairs(outputs) do
    assert(not line:find("[\r\n%z]") and #line <= 64 * 1024)
    assert(not line:find("PRIVATE_ERROR", 1, true))
    io.write(line, "\n")
end
