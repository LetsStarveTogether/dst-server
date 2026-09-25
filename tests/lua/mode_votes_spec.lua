local root, scripts, mode = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local state = require("dst_server.state")
state.nonce, state.generation, state.telemetry_active = "01ARZ3NDEKTSV4RRFFQ69G5FAV", 1, true
GetTick = function() return 1 end
GetTimeReal = function() return 1000 end
local output, listeners = {}, {}
nolineprint = function(line) output[#output + 1] = line:sub(10) end
TheWorld = { meta = {session_identifier = "TEST"}, state = {cycles = 1}, components = {},
    ListenForEvent = function(_, name, fn) listeners[name] = fn end }
local adapters = require("dst_server.mode_votes")
local effects = 0
if mode == "gorge" then
    local voter = { canvote = true, kicks_pending = {}, modes_pending = {} }
    function voter:VoteKick(doer, target)
        if self.kicks_pending[target] and self.kicks_pending[target][doer] then return end
        self.kicks_pending[target] = self.kicks_pending[target] or {}
        self.kicks_pending[target][doer] = true
        if doer == "B" then effects = effects + 1; self.kicks_pending[target] = nil end
        return "ok", nil, 3
    end
    function voter:CalculateNewMode() end
    function voter:VoteForMode(doer, id) self.modes_pending[doer.userid] = id end
    TheWorld.components.quagmire = {ChangeMode = function() effects = effects + 1 end}
    assert(adapters.gorge_voter(voter))
    local a, b, c = voter:VoteKick("A", "TARGET")
    assert(a == "ok" and b == nil and c == 3)
    voter:VoteKick("A", "TARGET")
    voter:VoteKick("B", "TARGET")
    voter:VoteForMode({userid = "A"}, "endless")
    TheWorld.components.quagmire:ChangeMode("endless")
    assert(effects == 2)
else
    local selection = mode == "forge_no" and 2 or mode == "forge_tie" and 0 or 1
    local voter = {is_vote_active = true, current_vote = "reset", initiator_id = "A", result = 0,
        vote_results = {A = {voted = false}, B = {voted = false}}}
    function voter:SubmitVote(userid, vote)
        local ballot = self.vote_results and self.vote_results[userid]
        if ballot and not ballot.voted then
            ballot.voted, ballot.vote = true, vote
            return self:CheckVote(self.vote_results.A.voted and self.vote_results.B.voted)
        end
    end
    function voter:EndVote() self.is_vote_active = false; self.vote_results = nil end
    function voter:CancelVote() self:EndVote() end
    function voter:CheckVote(complete)
        if complete then
            self.result = selection
            if self.result == 1 then effects = effects + 1 end
            self:EndVote()
        end
        return "ok", nil, 3
    end
    assert(adapters.lobbyvote(voter))
    local a, b, c = voter:SubmitVote("A", 1)
    assert(a == "ok" and b == nil and c == 3)
    voter:SubmitVote("A", 1)
    voter:SubmitVote("B", selection)
    assert(effects == (selection == 1 and 1 or 0))
end
for _, record in ipairs(output) do io.write(record, "\n") end
