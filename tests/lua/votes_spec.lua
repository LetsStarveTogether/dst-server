local root, scripts, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
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
MAX_VOTE_OPTIONS = 6
STRINGS = { UI = { VOTEDIALOG = {YES = "Yes", NO = "No"} } }
local command = {
    name = scenario == "custom" and "mod_test" or "kick",
    voteoptions = scenario == "custom" and {"Opt A", "自定义", "Opt C"} or nil,
}
local failure, calls = {}, 0
local selected = scenario == "custom" and 3 or 1
local params = {user = "KU_TARGET"}
local counts = { total = 3, total_voted = 2, total_not_voted = 0,
    options = scenario == "custom" and {0, 0, 2} or {2, 0} }
local commands = {
    GetCommandFromHash = function() return command end,
    FinishVote = function(_, received_params, received_counts)
        calls = calls + 1
        assert(received_params == params and received_counts == counts)
        if scenario == "error" then error(failure, 0) end
        if scenario ~= "failed" then params.voteselection, params.votecount = selected, 2 end
        if scenario == "capture_error" then
            params.user = nil
            setmetatable(params, {__index = function() error("capture failed", 0) end})
        end
        return scenario ~= "failed", nil, "preserved"
    end,
}
package.loaded.usercommands = commands
TheWorld = setmetatable({
    GUID = 1, ismastershard = scenario ~= "secondary",
    meta = {session_identifier = "SESSION"}, state = {cycles = 2},
}, {__index = EntityScript})
local voter = { OnUpdate = function()
    TheWorld:PushEvent("master_worldvoterupdate", {countdown = 0})
    return commands.FinishVote(command.name, params, counts)
end }
TheWorld.net = {components = {worldvoter = voter}}
require("dst_server.vote_events").install()
local snapshot = {
    countdown = 30, commandid = 42, targetuserid = "KU_TARGET", starteruserid = "KU_A",
    voters = {KU_A = 7, KU_B = 7},
}
TheWorld:PushEvent("master_worldvoterupdate", snapshot)
if scenario == "secondary" then assert(#outputs == 0); return end
assert(#outputs == 1)
TheWorld:PushEvent("master_worldvoterupdate", snapshot)
assert(#outputs == 1, "repeated snapshots cannot duplicate vote starts")
snapshot.voters.KU_A = selected
TheWorld:PushEvent("master_worldvoterupdate", snapshot)
TheWorld:PushEvent("master_worldvoterupdate", snapshot)
assert(#outputs == 2, "repeated snapshots cannot duplicate ballots")
snapshot.voters.KU_B = selected
TheWorld:PushEvent("master_worldvoterupdate", snapshot)
assert(#outputs == 3)
if scenario == "cancelled" then
    TheWorld:PushEvent("master_worldvoterupdate", {countdown = 0})
    commands.FinishVote(command.name, params, counts)
    assert(#outputs == 4, "an unrelated result cannot reuse a cancelled vote")
else
    local results = require("dst_server.telemetry").pack(pcall(voter.OnUpdate))
    if scenario == "error" then
        assert(results[1] == false and results[2] == failure and #outputs == 4)
        scenario = "passed"
        commands.FinishVote(command.name, params, counts)
        assert(#outputs == 4, "errors cannot leak an update context")
    else
        assert(results.n == 4 and results[1] and results[2] == (scenario ~= "failed") and results[3] == nil and results[4] == "preserved")
        assert(calls == 1 and #outputs == 5)
    end
end
assert(state.errors == (scenario == "capture_error" and 1 or 0))
for _, line in ipairs(outputs) do io.write(line, "\n") end
