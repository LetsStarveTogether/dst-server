local values = require("dst_server.values")
local voting = { methods = {} }
local current, countdown, watched

local function failure(code, message)
    return { error = { code = code, message = message } }
end

local function voter()
    return TheWorld.ismastershard and TheWorld.net ~= nil and TheWorld.net.components.worldvoter or nil
end

local function options(command)
    return command.voteoptions or { STRINGS.UI.VOTEDIALOG.YES, STRINGS.UI.VOTEDIALOG.NO }
end

local function nullable(value)
    return value ~= nil and value ~= "" and value or json.null
end

function voting.install()
    if voter() == nil then return false end
    TheWorld:ListenForEvent("master_worldvoterupdate", function(_, data)
        if type(data) ~= "table" or type(data.commandid) ~= "number" or type(data.countdown) ~= "number" then return end
        countdown = current ~= nil and math.min(countdown, data.countdown) or data.countdown
        current = data.commandid ~= 0 and data or nil
    end)
    TheWorld:ListenForEvent("worldvotertick", function(_, data)
        if current ~= nil and type(data) == "table" and type(data.time) == "number" then countdown = data.time end
    end)
    watched = true
    return true
end

function voting.methods.get_vote()
    local component = voter()
    if component == nil then return failure("unsupported", "this world has no standard world voter") end
    if not watched or current == nil and component:IsVoteActive() then
        return failure("not_ready", "vote state is unavailable")
    end
    local vote = json.null
    if current ~= nil then
        local command = require("usercommands").GetCommandFromHash(current.commandid)
        if command == nil then return failure("not_ready", "active vote command is unavailable") end
        local choices, voters = {}, {}
        for _, description in ipairs(options(command)) do
            choices[#choices + 1] = { description = description, count = 0 }
        end
        for userid, selection in pairs(current.voters or {}) do
            local selected = choices[selection]
            if selected ~= nil then selected.count = selected.count + 1 end
            voters[#voters + 1] = {
                userid = userid, can_vote = selection > 0,
                selection = selected ~= nil and selection or json.null,
            }
        end
        table.sort(voters, function(a, b) return a.userid < b.userid end)
        vote = {
            command = command.name, command_hash = current.commandid,
            starter_userid = nullable(current.starteruserid), target_userid = nullable(current.targetuserid),
            countdown = countdown, options = choices, voters = voters,
        }
    end
    return { enabled = component:IsEnabled(), current = vote }
end

function voting.methods.list_vote_commands()
    if voter() == nil then return failure("unsupported", "this world has no standard world voter") end
    local commands, result = require("usercommands"), {}
    for _, name in ipairs(commands.GetCommandNames()) do
        local command = commands.GetCommandFromName(name)
        if command ~= nil and command.vote and command.aliasfor == nil then
            result[#result + 1] = {
                command = name, command_hash = command.hash, requires_target = command.usermenu == true,
                options = options(command), timeout = math.min(255, command.votetimeout or TUNING.VOTE_TIMEOUT_DEFAULT),
                minimum_pass_count = command.voteminpasscount or 1,
                minimum_starter_age = command.voteminstartage or json.null,
            }
        end
    end
    table.sort(result, function(a, b) return a.command < b.command end)
    return result
end

local function connected(userid)
    for _, client in ipairs(GetPlayerClientTable()) do
        if client.userid == userid then return client end
    end
end

local function vote_command(commands, name, target)
    local command = commands.GetCommandFromName(name)
    if command == nil or not command.vote or command.aliasfor ~= nil then
        return nil, failure("invalid", "command is not a registered vote")
    end
    if (command.usermenu == true) ~= (target ~= nil) then
        return nil, failure("invalid", "vote target does not match the command")
    end
    return command
end

local function check_access(commands, command, player, target)
    local result = commands.UserRunCommandResult(command.name, player, target)
    if result == COMMAND_RESULT.VOTE or result == COMMAND_RESULT.ALLOW then return command.hash end
    if result == COMMAND_RESULT.DISABLED then
        local _, reason = commands.CanUserStartCommand(command.name, player, target)
        return failure("invalid", reason or "command cannot start")
    elseif result == COMMAND_RESULT.DENY then
        if TheWorld.net.components.worldvoter:IsVoteActive() then
            return failure("busy", "a vote is already active")
        end
        local _, reason = commands.CanUserStartVote(command.name, player, target)
        return failure("invalid", reason or "starter cannot start a vote now")
    end
    return failure("invalid", "starter cannot access this command or target")
end

function voting.methods.check_vote_access(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if not player then return failure("not_found", "vote starter is disconnected") end
    local commands = require("usercommands")
    local command, err = vote_command(commands, args.command, args.target_userid)
    if not command then return err end
    return check_access(commands, command, player, args.target_userid)
end

function voting.methods.start_vote(args)
    local name = values.required_string(args, "command")
    local starter = values.required_string(args, "starter_userid")
    local target = args.target_userid ~= nil and values.required_string(args, "target_userid") or nil
    local status = voting.methods.get_vote()
    if status.error ~= nil then return status end
    if not status.enabled then return failure("unsupported", "voting is disabled") end
    if status.current ~= json.null then return failure("busy", "a vote is already active") end
    local commands = require("usercommands")
    local command, err = vote_command(commands, name, target)
    if not command then return err end
    local client = connected(starter)
    if client == nil or target ~= nil and connected(target) == nil then
        return failure("not_found", "vote starter or target is disconnected")
    end
    local player = LookupPlayerInstByUserID(starter)
    if player ~= nil or args._checked_command_hash == nil then
        local checked = check_access(commands, command, player or client, target)
        if type(checked) ~= "number" then return checked end
    elseif args._checked_command_hash ~= command.hash then
        return failure("invalid", "vote command differs between shards")
    end
    local allowed, reason = commands.CanUserStartVote(name, client, target)
    if not allowed then return failure("invalid", reason or "vote requirements are not met") end
    Shard_StartVote(command.hash, starter, target)
    if current == nil or current.commandid ~= command.hash or current.starteruserid ~= starter
        or nullable(current.targetuserid) ~= nullable(target) then
        return failure("invalid", "native voter rejected the request; the starter may be on cooldown")
    end
    return voting.methods.get_vote()
end

function voting.methods.cancel_vote()
    local status = voting.methods.get_vote()
    if status.error ~= nil then return status end
    if status.current == json.null then return false end
    TheWorld:PushEvent("ms_stopvote")
    if current ~= nil or voter():IsVoteActive() then
        return failure("unknown", "vote cancellation was not confirmed")
    end
    return true
end

return voting
