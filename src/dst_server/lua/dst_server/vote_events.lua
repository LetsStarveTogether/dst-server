local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local vote_events = {}

local function userid(value)
    return value ~= nil and value ~= "" and value or json.null
end

function vote_events.install()
    if not TheWorld.ismastershard then return end
    local commands = require("usercommands")
    local finish = commands.FinishVote
    assert(type(finish) == "function", "UserCommands.FinishVote is unavailable")
    local voter = assert(TheWorld.net.components.worldvoter, "worldvoter is unavailable")
    local update = voter.OnUpdate
    assert(type(update) == "function", "worldvoter.OnUpdate is unavailable")
    local active, context
    local counter = 0
    local pending = MAX_VOTE_OPTIONS + 1

    TheWorld:ListenForEvent("master_worldvoterupdate", telemetry.guard("vote.update", function(_, data)
        if data.voters == nil or data.countdown <= 0 then
            if active ~= nil then
                local previous = active
                active = nil
                telemetry.emit("dst.vote.closed", { vote_id = previous.id })
                if context ~= nil then context.vote = previous end
            end
            return
        end

        if active == nil then
            counter = counter + 1
            local command = commands.GetCommandFromHash(data.commandid)
            local options = {}
            for _, option in ipairs(command ~= nil and (command.voteoptions
                or { STRINGS.UI.VOTEDIALOG.YES, STRINGS.UI.VOTEDIALOG.NO }) or {}) do
                options[#options + 1] = option
            end
            active = {
                id = string.format("%s:%.0f:%.0f", state.nonce, state.generation, counter),
                command = command ~= nil and command.name or nil,
                target = data.targetuserid,
                voters = {},
            }
            telemetry.emit("dst.vote.started", {
                vote_id = active.id,
                command = active.command or json.null,
                command_hash = data.commandid,
                starter_userid = userid(data.starteruserid),
                target_userid = userid(data.targetuserid),
                timeout = data.countdown,
                options = options,
            })
        end

        local voters = {}
        for voter, selection in pairs(data.voters) do
            if active.voters[voter] == pending and selection > 0 and selection < pending then
                telemetry.emit("dst.vote.cast", {
                    vote_id = active.id,
                    userid = voter,
                    selection = selection,
                })
            end
            voters[voter] = selection
        end
        -- Native snapshots reuse and mutate the same voters table.
        active.voters = voters
    end))

    voter.OnUpdate = function(...)
        -- Native CheckVoteResults clears state before FinishVote. Scope their link to
        -- that actual component update, so a cancelled vote can never be reused later.
        local previous = context
        context = {}
        local results = telemetry.pack(pcall(update, ...))
        context = previous
        if not results[1] then error(results[2], 0) end
        return telemetry.unpack(results, 2)
    end

    local capture_result = telemetry.guard("vote.result", function(vote, command, params, counts, passed)
        if vote == nil or (vote.command ~= nil and vote.command ~= command)
            or userid(vote.target) ~= userid(params.user) then return end
        telemetry.emit("dst.vote.result", {
            vote_id = vote.id,
            command = command,
            target_userid = userid(params.user),
            passed = not not passed,
            selection = params.voteselection or json.null,
            count = params.votecount or json.null,
            total = counts.total,
            total_voted = counts.total_voted,
            total_not_voted = counts.total_not_voted,
            options = counts.options,
        })
    end)
    commands.FinishVote = function(command, params, counts, ...)
        local vote = context ~= nil and context.vote or nil
        if context ~= nil then context.vote = nil end
        local results = telemetry.pack(finish(command, params, counts, ...))
        capture_result(vote, command, params, counts, results[1])
        return telemetry.unpack(results)
    end
end

return vote_events
