local root, handlers, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
local scripts = os.getenv("DST_SERVER_TEST_SCRIPTS") or root .. "/dst-scripts/scripts"
package.path = root .. "/src/dst_server/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
json = require("json")
require("class")
Entity = {}
require("entityscript")
local state = require("dst_server.state")
state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 4
state.requested_profile = "critical"
state.telemetry_active = true
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local outputs = {}
nolineprint = function(line) outputs[#outputs + 1] = line:sub(10) end
print = function() end
MAX_VOTE_OPTIONS = 6
TUNING = {
    VOTE_TIMEOUT_DEFAULT = 30, VOTE_CANCELLED_SQUELCH_TIME = 30,
    VOTE_PASSED_SQUELCH_TIME = 30, VOTE_FAILED_SQUELCH_TIME = 30,
}
STRINGS = { UI = { VOTEDIALOG = {YES = "Yes", NO = "No"}, PLAYERSTATUSSCREEN = {
    VOTECANNOTSTART = { DISABLED = "disabled" },
} } }
AllPlayers = {}
BRANCH = "release"
local clients = {
    {userid = "KU_A", name = "A"}, {userid = "KU_B", name = "B"},
    {userid = "KU_TARGET", name = "Target"},
}
local target_present = true
UserToName = function(userid)
    if userid == "KU_TARGET" and not target_present then return nil end
    for _, client in ipairs(clients) do if client.userid == userid then return client.name end end
end
local executed = 0
local selected = scenario == "custom" and 3 or 1
local failure = {}
local command = {
    name = scenario == "custom" and "mod_test" or "kick", hash = 42,
    vote = true, cantargetself = false, votecountvisible = true,
    voteoptions = scenario == "custom" and {"Opt A", "自定义", "Opt C"} or nil,
    voteresultfn = function(_, counts)
        if scenario == "failed" then return nil, 0 end
        return selected, counts.options[selected]
    end,
    serverfn = function(params)
        if scenario == "error" then error(failure, 0) end
        executed = executed + 1
        if scenario == "capture_error" then
            params.user = nil
            setmetatable(params, { __index = function() error("MOD parameter access failed", 0) end })
        end
    end,
}
local command_present = true
local can_start = true
local commands = {
    GetCommandFromName = function() return command_present and command or nil end,
    GetCommandFromHash = function() return command_present and command or nil end,
    CanUserStartVote = function() return can_start, "DISABLED" end,
    SendVoteMetricsEvent = function() end,
}
package.loaded.usercommands = commands
commands.FinishVote = dofile(handlers)
local announcements = 0
TheNet = {
    IsDedicated = function() return true end,
    GetDefaultVoteEnabled = function() return true end,
    GetClientTable = function() return clients end,
    GetClientTableForUser = function(_, userid)
        for _, client in ipairs(clients) do if client.userid == userid then return client end end
    end,
    GetServerIsClientHosted = function() return false end,
    AnnounceVoteResult = function() announcements = announcements + 1 end,
}
local function entity(guid)
    return setmetatable({
        GUID = guid,
        StartUpdatingComponent = function() end,
        StopUpdatingComponent = function() end,
        DoTaskInTime = function() return { Cancel = function() end } end,
    }, {__index = EntityScript})
end
TheWorld = entity(1)
TheWorld.ismastersim = true
TheWorld.ismastershard = scenario ~= "secondary"
TheWorld.meta = {session_identifier = "SESSION"}
TheWorld.state = {cycles = 2}
local net = entity(2)
TheWorld.net = net
local dirty = {}
local function netvar(initial, event)
    local value = initial
    return {
        value = function() return value end,
        set = function(_, next_value)
            if value ~= next_value then
                value = next_value
                if event ~= nil then dirty[event] = true end
            end
        end,
        set_local = function(_, next_value) value = next_value end,
    }
end
net_bool = function(_, _, event) return netvar(false, event) end
net_byte = function(_, _, event) return netvar(0, event) end
net_uint = net_byte
net_string = function(_, _, event) return netvar("", event) end
local function flush()
    local events = dirty
    dirty = {}
    for event in pairs(events) do net:PushEvent(event) end
end
local voter = require("components/worldvoter")(net)
net.components = {worldvoter = voter}
voter:OnPostInit()
require("dst_server.vote_events").install()
if scenario == "secondary" then
    TheWorld:PushEvent("master_worldvoterupdate", {
        countdown = 30, commandid = 42, targetuserid = "KU_TARGET", starteruserid = "KU_A",
        voters = {KU_A = 7},
    })
    assert(#outputs == 0)
    return
end

local start = {commandhash = 42, targetuserid = "KU_TARGET", starteruserid = "KU_A"}
can_start = false
TheWorld:PushEvent("ms_startvote", start)
assert(#outputs == 0, "native rejected start is not a started vote")
can_start = true
TheWorld:PushEvent("ms_startvote", start)
flush()
assert(#outputs == 1)
TheWorld:PushEvent("ms_startvote", start)
assert(#outputs == 1, "an already active vote must not start again")
TheWorld:PushEvent("ms_receivevote", {userid = "KU_TARGET", selection = 1})
TheWorld:PushEvent("ms_receivevote", {userid = "KU_UNKNOWN", selection = 1})
TheWorld:PushEvent("ms_receivevote", {userid = "KU_A", selection = 6})
assert(#outputs == 1, "rejected ballots cannot become vote facts")
TheWorld:PushEvent("ms_receivevote", {userid = "KU_A", selection = selected})
TheWorld:PushEvent("ms_receivevote", {userid = "KU_A", selection = selected})
assert(#outputs == 2, "native duplicate ballot rejection must be preserved")
voter:OnUpdate(5)
flush()
assert(#outputs == 2, "periodic voter snapshots do not duplicate events")
TheWorld:PushEvent("ms_receivevote", {userid = "KU_B", selection = selected})
assert(#outputs == 3)
if scenario == "cancelled" then
    TheWorld:PushEvent("ms_stopvote")
    assert(#outputs == 4)
    -- Even in the same tick and with the same command, an unrelated call cannot
    -- inherit a cancelled vote's identity. No timer needs to run first.
    commands.FinishVote(command.name, {user = "KU_TARGET"}, {
        total = 3, total_voted = 2, total_not_voted = 0, options = {2, 0},
    })
    assert(#outputs == 4)
else
    if scenario == "target_left" then
        -- Native FinishVote retains the target's name captured at vote start.
        target_present = false
    elseif scenario == "command_removed" then
        command_present = false
    end
    voter:OnUpdate(0.1)
    local ok, err = pcall(voter.OnUpdate, voter, 3)
    if scenario == "error" then
        assert(not ok and err == failure, "native exception identity must be preserved")
        assert(#outputs == 4, "a failed command must not invent a successful result")
        command.serverfn = function() end
        commands.FinishVote(command.name, {user = "KU_TARGET"}, {
            total = 3, total_voted = 2, total_not_voted = 0, options = {2, 0},
        })
        assert(#outputs == 4, "exception cleanup must not leak an update context")
    else
        assert(ok, tostring(err))
        assert(#outputs == 5)
        if scenario == "capture_error" then
            assert(executed == 1, "capture failure must not change successful native command execution")
            assert(announcements == 1, "capture failure must not change native vote result announcement")
            assert(json.decode(outputs[#outputs]).event == "dst.telemetry.error")
        end
    end
end
assert(state.errors == (scenario == "capture_error" and 1 or 0))
for _, line in ipairs(outputs) do io.write(line, "\n") end
