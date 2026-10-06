local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local gameplay_actions = {}

local action_ids = {
    "ACTIVATE", "ADDFUEL", "ADDWETFUEL", "FERTILIZE", "HEAL", "REPAIR", "REPAIR_LEAK",
    "UPGRADE", "LIGHT", "EXTINGUISH", "MANUALEXTINGUISH", "SMOTHER", "CASTSPELL",
    "CASTAOE", "CAST_SPELLBOOK", "CAST_POCKETWATCH",
}

local function action_item(id, act)
    if id == "CASTSPELL" and not act.invobject then
        local inventory = act.doer.components and act.doer.components.inventory
        return inventory and inventory:GetEquippedItem(EQUIPSLOTS.HANDS)
    elseif id == "REPAIR" and act.target and act.target.components and act.target.components.repairable
        and act.doer.components then
        local inventory, rider = act.doer.components.inventory, act.doer.components.rider
        if inventory and inventory:IsHeavyLifting() and not (rider and rider:IsRiding()) then
            return inventory:GetEquippedItem(EQUIPSLOTS.BODY)
        end
    end
    return act.invobject
end

local capture = telemetry.guard("actions.capture", function(id, act)
    local doer = act.doer
    if not doer or not doer:HasTag("player") then return end
    local player = values.entity_ref(doer)
    if player == json.null then return end
    local target = act.target
    if not target and (id == "HEAL" or id == "FERTILIZE") then target = doer end
    local item = action_item(id, act)
    return {
        player = player,
        action_id = id,
        target = target == doer and player or values.entity_ref(target),
        item = item and item.prefab or json.null,
    }
end)

local publish = telemetry.guard("actions.publish", function(data)
    telemetry.play("dst.player.action_performed", data)
end)

local function completed(data, ...)
    if (...) then publish(data) end
    return ...
end

local function observe(id, original)
    return function(...)
        -- Native actions can consume the item or remove the target.
        local data = capture(id, ...)
        if not data then return original(...) end
        return completed(data, original(...))
    end
end

function gameplay_actions.install()
    local actions = rawget(_G, "ACTIONS")
    if type(actions) ~= "table" then return false end
    local installed = false
    for _, id in ipairs(action_ids) do
        local action = actions[id]
        if type(action) == "table" and type(action.fn) == "function" then
            action.fn = observe(id, action.fn)
            installed = true
        else
            telemetry.report("actions." .. id:lower(), "installation_failed")
        end
    end
    return installed
end

return gameplay_actions
