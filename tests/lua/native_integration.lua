-- This runs inside the real dedicated server, with its complete modules and engine.
local actor = assert(LookupPlayerInstByUserID("KU_NATIVE"))
actor:OnNewSpawn()
ClientAuthenticationComplete(actor.userid)
ClientDisconnected(actor.userid)
Networking_Say(actor.GUID, actor.userid, "玩家", "wilson", "你好\n世界", {1, 1, 1, 1}, false, false)
Networking_ModOutOfDateAnnouncement("Native contract")
OnServerPauseDirty(true, false, false, "玩家")
OnServerPauseDirty(false, false, false, nil)
OnSimPaused()
OnSimUnpaused()

local attacker = assert(LookupPlayerInstByUserID("KU_ATTACKER"))
actor.components.health:SetInvincible(false)
actor.components.combat:GetAttacked(attacker, 10)
local axe = SpawnPrefab("axe")
actor.components.inventory:Equip(axe)
assert(actor.components.inventory:Unequip(EQUIPSLOTS.HANDS) == axe)
axe:Remove()
local tree = SpawnPrefab("evergreen")
tree.components.workable:SetWorkLeft(1)
tree.components.workable:WorkedBy(actor, 1)
local grass = SpawnPrefab("grass")
grass.components.pickable:Pick(actor)
local action = BufferedAction(actor, nil, {
    id = "CHOP", fn = function() return true, "preserved" end,
})
local ok, reason = action:Do()
assert(ok and reason == "preserved")
actor.sg:GoToState("sink_fast")
actor.sg:GoToState("idle")
actor.sg:GoToState("abyss_fall")
actor.sg:GoToState("idle")

-- Supply only the absent network clients; voting uses the real game component.
local methods = getmetatable(TheNet).__index
local original_clients, original_client = methods.GetClientTable, methods.GetClientTableForUser
local clients = { {userid = "KU_NATIVE", name = "玩家"}, {userid = "KU_ATTACKER", name = "Attacker"} }
methods.GetClientTable = function() return clients end
methods.GetClientTableForUser = function(_, userid)
    for _, client in ipairs(clients) do if client.userid == userid then return client end end
end
local executed = 0
AddUserCommand("sdk_contract_vote", {
    vote = true, cantargetself = true, votecountvisible = true,
    voteresultfn = function(_, counts) return 1, counts.options[1] end,
    serverfn = function()
        executed = executed + 1
        assert(executed == 1, "the accepted native vote must run once")
        -- The following tick observes FinishVote's completed result capture.
        TheWorld:DoTaskInTime(0, function()
            methods.GetClientTable, methods.GetClientTableForUser = original_clients, original_client
            RemoveUserCommand("sdk_contract_vote")
            Networking_SystemMessage("native-contract-complete")
        end)
    end,
})
local command = require("usercommands").GetCommandFromName("sdk_contract_vote")
TheWorld:PushEvent("ms_startvote", {commandhash = command.hash, starteruserid = "KU_NATIVE"})
for _, client in ipairs(clients) do
    TheWorld:PushEvent("ms_receivevote", {userid = client.userid, selection = 1})
end
return true
