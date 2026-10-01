local telemetry = require("dst_server.telemetry")
local adapters = {}

local function observe(object, name, capture)
    local original = assert(object[name])
    capture = telemetry.guard("vote." .. string.lower(name), capture)
    object[name] = function(...)
        capture(...)
        return original(...)
    end
end

local function submitted(source, command, userid, target, selection)
    telemetry.emit("dst.vote.submitted", {
        source = source, command = command, userid = userid,
        target_userid = target or json.null, selection = selection,
    })
end

function adapters.lobbyvote(voter)
    if voter == nil then return false end
    assert(type(voter.SubmitVote) == "function" and type(voter.EndVote) == "function")
    observe(voter, "SubmitVote", function(self, userid, selection)
        local ballot = self.vote_results and self.vote_results[userid]
        if ballot ~= nil and not ballot.voted then
            submitted("lobbyvote", self.current_vote, userid,
                self.vote_params and self.vote_params.target_id, selection)
        end
    end)
    observe(voter, "EndVote", function(self)
        if self.is_vote_active then
            telemetry.emit("dst.vote.closed", {
                source = "lobbyvote", command = self.current_vote,
                starter_userid = self.initiator_id or json.null,
                target_userid = self.vote_params and self.vote_params.target_id or json.null,
            })
        end
    end)
    return true
end

function adapters.gorge_voter(voter)
    if voter == nil then return false end
    assert(type(voter.VoteKick) == "function" and type(voter.VoteForMode) == "function")
    observe(voter, "VoteKick", function(self, userid, target)
        if self.canvote and userid and target
            and not (self.kicks_pending[target] and self.kicks_pending[target][userid]) then
            submitted("gorge_voter", "kick", userid, target, 1)
        end
    end)
    observe(voter, "VoteForMode", function(self, doer, selection)
        if self.canvote and doer and selection
            and (doer.admin or not self.modes_pending[doer.userid]) then
            submitted("gorge_voter", "mode", doer.userid, nil, selection)
        end
    end)
    return true
end

return adapters
