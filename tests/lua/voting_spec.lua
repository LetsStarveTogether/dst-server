local root, scripts = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
require("class")
package.loaded["screens/redux/popupdialog"] = {}
package.loaded["map/customize"] = {}
require("shardnetworking")
package.loaded.stats = { PushMetricsEvent = function() end }
MAX_VOTE_OPTIONS = 6
COMMAND_PERMISSION = { USER = 1, MODERATOR = 2, ADMIN = 3 }
COMMAND_RESULT = { INVALID = 0, DISABLED = 1, ALLOW = 2, DENY = 3, VOTE = 4 }
STRINGS = { UI = { VOTEDIALOG = { YES = "Yes", NO = "No" }, BUILTINCOMMANDS = {},
    PLAYERSTATUSSCREEN = { VOTECANNOTSTART = { MINPLAYERS = "players", MINSTARTAGE = "age" } } } }
TUNING = { VOTE_TIMEOUT_DEFAULT = 30, VOTE_CANCELLED_SQUELCH_TIME = 60,
    VOTE_PASSED_SQUELCH_TIME = 30, VOTE_FAILED_SQUELCH_TIME = 40 }
local hashes = {}
smallhash = function(name)
    if hashes[name] == nil then hashes[name] = #hashes + 100; hashes[#hashes + 1] = name end
    return hashes[name]
end
local tasks, dirty = {}, {}
local function entity(guid)
    local inst = { GUID = guid, components = {}, listeners = {} }
    function inst:ListenForEvent(name, callback, source)
        local listeners = (source or self).listeners
        listeners[name] = listeners[name] or {}
        table.insert(listeners[name], callback)
    end
    function inst:PushEvent(name, data)
        for _, callback in ipairs(self.listeners[name] or {}) do callback(self, data) end
    end
    function inst:StartUpdatingComponent() self.updating = true end
    function inst:StopUpdatingComponent() self.updating = false end
    function inst:DoTaskInTime(_, callback, userid)
        local task = { Cancel = function(self) self.cancelled = true end }
        task.run = function() if not task.cancelled then callback(self, userid) end end
        tasks[#tasks + 1] = task
        return task
    end
    return inst
end
TheWorld = entity(1)
TheWorld.ismastersim, TheWorld.ismastershard = true, true
TheWorld.net = entity(2)
local function netvar(_, _, event)
    local data
    return {
        value = function() return data end,
        set = function(_, value) data = value; if event ~= nil then dirty[event] = true end end,
        set_local = function(_, value) data = value end,
    }
end
local function typed(default)
    return function(...)
        local var = netvar(...)
        var:set_local(default)
        return var
    end
end
net_bool, net_byte, net_uint, net_string = typed(false), typed(0), typed(0), typed("")
local function flush()
    local pending = dirty
    dirty = {}
    for event in pairs(pending) do TheWorld.net:PushEvent(event) end
end
local clients = {
    { userid = "SERVER", performance = {}, playerage = 0 },
    { userid = "KU_A", name = "A", playerage = 20 },
    { userid = "KU_B", name = "B", playerage = 20 },
    { userid = "KU_C", name = "C", playerage = 20 },
}
local enabled, executed = true, 0
TheNet = {
    IsDedicated = function() return true end,
    GetServerIsClientHosted = function() return false end,
    GetClientTable = function() return clients end,
    GetClientTableForUser = function(_, userid)
        for _, client in ipairs(clients) do if client.userid == userid then return client end end
    end,
    GetDefaultVoteEnabled = function() return enabled end,
    AnnounceVoteResult = function() end,
}
GetPlayerClientTable = function()
    local result = {}
    for _, client in ipairs(clients) do if client.performance == nil then result[#result + 1] = client end end
    return result
end
AllPlayers = {}
for _, client in ipairs(GetPlayerClientTable()) do
    AllPlayers[#AllPlayers + 1] = { userid = client.userid, components = { playervoter = {
        IsSquelched = function() return false end, SetSelection = function() end, SetSquelched = function() end,
    } } }
end
LookupPlayerInstByUserID = function(userid)
    for _, player in ipairs(AllPlayers) do if player.userid == userid then return player end end
end
local commands = require("usercommands")
local function command(target)
    return { vote = true, usermenu = target, permission = COMMAND_PERMISSION.ADMIN,
        voteminpasscount = 2, voteminstartage = 10, votecountvisible = true,
        voteresultfn = function(_, result) return 1, result.options[1] end,
        serverfn = function() executed = executed + 1 end }
end
AddUserCommand("kick", command(true))
AddUserCommand("rollback", command(false))
AddModUserCommand("test", "mod_vote", command(false))
AddUserCommand("novote", { permission = COMMAND_PERMISSION.ADMIN })
local native = require("components/worldvoter")(TheWorld.net)
TheWorld.net.components.worldvoter = native
native:OnPostInit()
local voting = require("dst_server.voting")
assert(voting.install())
local state = require("dst_server.state")
state.installed, state.requested_profile = true, "off"
local driver = require("dst_server")
local function start(name, starter, target)
    return driver.call("start_vote", { command = name, starter_userid = starter, target_userid = target })
end
assert(#driver.call("list_vote_commands") == 3)
assert(driver.call("get_vote").enabled and driver.call("get_vote").current == json.null)
assert(driver.call("cancel_vote") == false)
assert(start("novote", "KU_A").error.code == "invalid")
assert(start("kick", "KU_A").error.code == "invalid")
assert(start("rollback", "KU_A", "KU_B").error.code == "invalid")
assert(start("kick", "SERVER", "KU_B").error.code == "not_found")
assert(start("kick", "KU_A", "KU_ABSENT").error.code == "not_found")
assert(start("kick", "KU_A", "KU_A").error.code == "invalid")
clients[3].admin = true
assert(start("kick", "KU_A", "KU_B").error.code == "invalid")
clients[3].admin = false
local saved = table.remove(clients)
assert(start("kick", "KU_A", "KU_B").error.message == "MINPLAYERS")
clients[#clients + 1] = saved
clients[2].playerage = 1
assert(start("kick", "KU_A", "KU_B").error.message == "MINSTARTAGE")
clients[2].playerage = 20
local vote = start("kick", "KU_A", "KU_B")
assert(vote.current.command == "kick" and vote.current.starter_userid == "KU_A")
TheWorld:PushEvent("master_worldvoterupdate", {})
TheWorld:PushEvent("worldvotertick")
assert(driver.call("get_vote").current.countdown == vote.current.countdown)
assert(not native:IsVoteActive(), "native dialog visibility lags the start event")
assert(start("rollback", "KU_A").error.code == "busy")
assert(vote.current.voters[1].selection == json.null and vote.current.voters[1].can_vote)
assert(vote.current.voters[2].can_vote == false, "kick target is ineligible")
flush()
assert(native:IsVoteActive())
TheWorld:PushEvent("ms_receivevote", { userid = "KU_A", selection = 1 })
assert(driver.call("get_vote").current.options[1].count == 1)
native:OnUpdate(1)
assert(driver.call("get_vote").current.countdown < 30)
assert(driver.call("cancel_vote") and driver.call("cancel_vote") == false and executed == 0)
assert(start("rollback", "KU_A").error.code == "invalid", "native cancellation cooldown must apply")
for _, task in ipairs(tasks) do task.run() end
assert(start("mod_vote", "KU_A").current.command == "mod_vote")
flush()
for _, userid in ipairs({ "KU_A", "KU_B", "KU_C" }) do
    TheWorld:PushEvent("ms_receivevote", { userid = userid, selection = 1 })
end
native:OnUpdate(0.1)
native:OnUpdate(2)
assert(executed == 1 and driver.call("get_vote").current == json.null)

-- The cave checks command access with the character. The master receives Klei's shard vote callback.
local master, masterplayers = TheWorld, AllPlayers
local caveplayer = table.remove(masterplayers, 2)
assert(caveplayer.userid == "KU_B" and not LookupPlayerInstByUserID(caveplayer.userid))
assert(start("kick", "KU_B", "KU_C").error.code == "invalid")
TheWorld, AllPlayers = entity(3), {caveplayer}
TheWorld.ismastersim, TheWorld.ismastershard, TheWorld.net = true, false, entity(4)
TheWorld.net.components.worldvoter = require("components/worldvoter")(TheWorld.net)
TheWorld:PushEvent("secondary_worldvoterenabled", true)
local kick = commands.GetCommandFromName("kick")
kick.hasaccessfn = function(_, caller) return caller == caveplayer and caller.can_start end
local args = {command = "kick", starter_userid = "KU_B", userid = "KU_B", target_userid = "KU_C"}
assert(driver.call("check_vote_access", args).error.code == "invalid")
caveplayer.can_start = true
kick.votecanstartfn = function(_, caller) return caller ~= caveplayer, "PLAYER_CHECK" end
assert(driver.call("check_vote_access", args).error.message == "PLAYER_CHECK")
kick.votecanstartfn = nil
args._checked_command_hash = driver.call("check_vote_access", args)
assert(args._checked_command_hash == kick.hash)
TheWorld, AllPlayers = master, masterplayers
local remote_vote = driver.call("start_vote", args)
assert(remote_vote.current.command == "kick" and remote_vote.current.starter_userid == "KU_B"
    and remote_vote.current.target_userid == "KU_C")
assert(driver.call("get_vote").current.starter_userid == "KU_B" and driver.call("cancel_vote"))
kick.hasaccessfn = nil

TheWorld.net.components.worldvoter = { IsEnabled = function() return false end, IsVoteActive = function() return false end }
assert(start("rollback", "KU_B").error.code == "unsupported")
TheWorld.net.components.worldvoter = nil
assert(driver.call("get_vote").error.code == "unsupported")
assert(state.sequence == 0, "vote control must work with telemetry off")
io.write("ok\n")
