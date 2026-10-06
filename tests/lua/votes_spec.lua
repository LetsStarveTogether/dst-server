local root, scripts, scenario = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local state = require("dst_server.state")
state.nonce, state.generation = "01ARZ3NDEKTSV4RRFFQ69G5FAV", 4
state.requested_profile, state.telemetry_active = "timeline", true
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local update
TheWorld = {
    ismastershard = scenario ~= "secondary", net = { components = { worldvoter = {} } },
    meta = { session_identifier = "SESSION" }, state = { cycles = 2 },
    ListenForEvent = function(_, name, callback)
        assert(update == nil and name == "master_worldvoterupdate")
        update = callback
    end,
}
local records = {}
dofile(root .. "/tests/lua/capture_remote.lua")(function(line)
    records[#records + 1] = require("dst_server.wire").decode(line:sub(10))
    io.write(line:sub(10), "\n")
end)

package.loaded["screens/redux/popupdialog"], package.loaded.stats = {}, {}
IsRail = function() return false end
smallhash = function() return 42 end
local commands = require("usercommands")
local passed, announced, executed = true, 0, 0
AddUserCommand("kick", { vote = true,
    voteresultfn = function() return passed and 1 or nil, 2 end,
    serverfn = function() executed = executed + 1 end,
})
local native_finish = commands.FinishVote
local failure = {}
-- Preserve a mod wrapper's return tuple and errors while exercising the actual FinishVote.
commands.FinishVote = function(name, params, counts)
    if params == failure then error(failure, 0) end
    return native_finish(name, params, counts), nil, false, nil
end
local before_install = commands.FinishVote
local installed = require("dst_server.vote_events").install()
if scenario == "secondary" then
    assert(installed == false and update == nil and commands.FinishVote == before_install)
    return
end
assert(scenario == "complete" and installed)
TheNet = { AnnounceVoteResult = function(_, hash, _, outcome)
    assert(hash == 42 and outcome == passed)
    announced = announced + 1
end }
UserToName = function() return nil end
local counts = {total = 2, total_voted = 2, total_not_voted = 0, options = {2, 0}}
local params = {user = "KU_TARGET", username = "Target"}
local ballot = {commandid = 42, starteruserid = "KU_A", targetuserid = "KU_TARGET"}
local cleared = {commandid = 0}
local function begin()
    local before = #records
    update(TheWorld, ballot)
    ballot.voters = {KU_A = 1, KU_B = 0}
    update(TheWorld, ballot)
    assert(#records == before + 1 and records[#records].event == "dst.vote.started")
    assert(records[#records].data.starter_userid == "KU_A" and records[#records].data.target_userid == "KU_TARGET")
    update(TheWorld, cleared)
    assert(#records == before + 1, "the native voter clears on both settlement and cancellation")
end

update(TheWorld, cleared)
begin() -- Cancelled vote. The next one may have the same command and players.
for _, outcome in ipairs({true, false}) do
    passed = outcome
    begin()
    local results = require("dst_server.telemetry").pack(commands.FinishVote("kick", params, counts))
    assert(results.n == 4 and results[1] == passed and results[3] == false and results[4] == nil)
    local last = records[#records]
    assert(last.event == "dst.vote.result" and last.data.passed == passed)
    assert(last.data.target_userid == "KU_TARGET" and last.data.starter_userid == "KU_A")
end
assert(announced == 2 and executed == 1)

begin()
local before = #records
assert(not commands.FinishVote("kick", {user = "KU_TARGET"}, counts))
assert(announced == 2 and #records == before, "FinishVote returned early without announcing a failure")
begin()
before = #records
local ok, err = pcall(commands.FinishVote, "kick", failure, counts)
assert(not ok and err == failure and #records == before)
assert(state.errors == 0)
