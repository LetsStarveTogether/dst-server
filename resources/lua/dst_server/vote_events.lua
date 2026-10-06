local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local vote_events = {}

function vote_events.install()
    local net = TheWorld.net
    if not TheWorld.ismastershard or net == nil or net.components.worldvoter == nil then return false end
    local commands = require("usercommands")
    local finish = commands.FinishVote
    assert(type(finish) == "function", "FinishVote is unavailable")
    local active, started = false, nil
    TheWorld:ListenForEvent("master_worldvoterupdate", telemetry.guard("vote.started", function(_, data)
        if data.commandid == 0 then
            active = false
        elseif not active then
            active = true
            local command = commands.GetCommandFromHash(data.commandid)
            started = command and {
                command = command.name,
                starter_userid = values.userid(data.starteruserid) or json.null,
                target_userid = values.userid(data.targetuserid) or json.null,
            }
            if started ~= nil then telemetry.emit("dst.vote.started", started) end
        end
    end))
    local capture_result = telemetry.guard("vote.result", function(vote, name, params, passed)
        if vote == nil or vote.command ~= name or type(passed) ~= "boolean" then return end
        local target = values.userid(params.user) or json.null
        if target ~= vote.target_userid then return end
        -- FinishVote also returns false before announcing if the command or target is gone.
        -- The world voter supplies its cached target name when one was observed.
        if not passed and (commands.GetCommandFromName(name) == nil or target ~= json.null and params.username == nil) then
            return
        end
        telemetry.emit("dst.vote.result", {
            command = name, passed = passed, starter_userid = vote.starter_userid, target_userid = target,
        })
    end)
    commands.FinishVote = function(...)
        -- The native voter clears the active vote immediately before calling FinishVote.
        local vote = started
        started = nil
        local name, params = ...
        local results = telemetry.pack(finish(...))
        capture_result(vote, name, params, results[1])
        return unpack(results, 1, results.n)
    end
    return true
end

return vote_events
