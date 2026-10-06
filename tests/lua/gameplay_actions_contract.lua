return function(entity, player, friend, records, pack)
    local kit = entity("healingsalve", 20)
    local heal = {action = ACTIONS.HEAL, doer = player, target = friend, invobject = kit}
    friend.components.health = {IsDead = function() return false end}
    kit.components.healer = {Heal = function(_, target, doer)
        assert(target == friend and doer == player)
        heal.invobject = nil
        return "restored", nil, 7, nil
    end}
    local result = pack(ACTIONS.HEAL.fn(heal))
    assert(result.n == 4 and result[1] == "restored" and result[2] == nil and result[3] == 7)
    local failure = {}
    heal.invobject = kit
    kit.components.healer.Heal = function() error(failure) end
    local ok, err = pcall(ACTIONS.HEAL.fn, heal)
    assert(not ok and err == failure)
    heal.invobject = nil
    assert(select("#", ACTIONS.HEAL.fn(heal)) == 0)

    local lever = entity("winch", 21)
    local use = {action = ACTIONS.ACTIVATE, doer = player, target = lever}
    lever.components.activatable = {CanActivate = function() return false, "BUSY" end}
    local activated, reason = ACTIONS.ACTIVATE.fn(use)
    assert(not activated and reason == "BUSY")
    lever.components.activatable = {CanActivate = function() return true end, DoActivate = function() end}
    assert(ACTIONS.ACTIVATE.fn(use))
    use.doer = lever
    assert(ACTIONS.ACTIVATE.fn(use))
    -- The native activation still succeeds if tracing cannot read the actor.
    use.doer = {HasTag = function() error("entity removed") end}
    assert(ACTIONS.ACTIVATE.fn(use))
    assert(require("dst_server").health().faults["actions.capture"] == "callback_failed")
    local actions = records("dst.player.action_performed")
    assert(#actions == 2 and actions[1].action_id == "HEAL" and actions[1].item == "healingsalve")
    assert(actions[1].player.userid == player.userid and actions[1].target.userid == friend.userid)
    assert(actions[2].action_id == "ACTIVATE" and actions[2].target.prefab == lever.prefab and actions[2].item == nil)

    local item = entity("wall_stone_item", 22)
    local deployable = setmetatable({inst = item, CanDeploy = function() return false, "BLOCKED" end},
        {__index = require("components/deployable")})
    local point = Vector3(2, 0, -3)
    local placed, why = deployable:Deploy(point, player)
    assert(not placed and why == "BLOCKED")
    deployable.CanDeploy = function() return true end
    deployable.ondeploy = function(inst, _, deployer)
        if deployer == player then inst.prefab = nil end
    end
    assert(deployable:Deploy(point, lever) and deployable:Deploy(point, player))
    item.prefab, item.tags.deployedplant = "dug_berrybush", true
    assert(deployable:Deploy(point, player))
    local deployments, plants = records("dst.player.item_deployed"), records("dst.player.planted")
    assert(#deployments == 1 and deployments[1].item == "wall_stone_item")
    assert(deployments[1].player.userid == player.userid and deployments[1].position.z == -3)
    assert(#plants == 1 and plants[1].player.userid == player.userid)
end
