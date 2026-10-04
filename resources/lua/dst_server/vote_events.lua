local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local vote_events = {}

local function userid(value)
    return value ~= nil and value ~= "" and value or json.null
end

local function same_vote(previous, data)
    if previous == nil or previous.command_hash ~= data.commandid
        or previous.starter_userid ~= userid(data.starteruserid)
        or previous.target_userid ~= userid(data.targetuserid) then return false end
    local voters, count = data.voters or {}, 0
    for _ in pairs(voters) do count = count + 1 end
    if count ~= #previous.voters then return false end
    for _, voter in ipairs(previous.voters) do
        if voters[voter.userid] ~= voter.selection then return false end
    end
    return true
end

local function install_standard()
    if TheWorld.net == nil or TheWorld.net.components.worldvoter == nil then return false end
    local commands = require("usercommands")
    local finish = assert(commands.FinishVote)
    local previous
    TheWorld:ListenForEvent("master_worldvoterupdate", telemetry.guard("vote.update", function(_, data)
        if same_vote(previous, data) or previous == nil and data.commandid == 0 then return end
        local command = commands.GetCommandFromHash(data.commandid)
        local voters = {}
        for id, selection in pairs(data.voters or {}) do
            voters[#voters + 1] = { userid = id, selection = selection }
        end
        table.sort(voters, function(a, b) return a.userid < b.userid end)
        previous = {
            source = "worldvoter",
            command = command ~= nil and command.name or json.null,
            command_hash = data.commandid,
            starter_userid = userid(data.starteruserid),
            target_userid = userid(data.targetuserid),
            countdown = data.countdown,
            voters = voters,
        }
        telemetry.emit("dst.vote.updated", previous)
    end))
    local capture = telemetry.guard("vote.result", function(command, params, counts, passed)
        telemetry.emit("dst.vote.result", {
            source = "worldvoter", command = command, target_userid = userid(params.user),
            passed = not not passed,
            selection = params.voteselection or json.null,
            count = params.votecount or json.null,
            total = counts.total, total_voted = counts.total_voted,
            total_not_voted = counts.total_not_voted, options = counts.options,
        })
    end)
    commands.FinishVote = function(command, params, counts, ...)
        local results = telemetry.pack(finish(command, params, counts, ...))
        capture(command, params, counts, results[1])
        return telemetry.unpack(results)
    end
end

function vote_events.install()
    if not TheWorld.ismastershard then
        state.capabilities.votes = "unsupported"
        state.capabilities.gorge_voter = "unsupported"
        state.capabilities.lobbyvote = "unsupported"
        return
    end
    local components = TheWorld.net and TheWorld.net.components or {}
    local adapters = require("dst_server.mode_votes")
    telemetry.install("votes", install_standard)
    telemetry.install("gorge_voter", function() return adapters.gorge_voter((TheWorld.components or {}).gorge_voter or components.gorge_voter) end)
    telemetry.install("lobbyvote", function() return adapters.lobbyvote(components.lobbyvote) end)
end

return vote_events
