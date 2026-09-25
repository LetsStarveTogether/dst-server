local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local actions = {}

local function capture(action)
    -- GetPosition clears invalid platform references; resolve a copy.
    local point = action:GetDynamicActionPoint()
    point = point ~= nil and point.GetPosition(shallowcopy(point)) or nil
    return {
        action_id = action.action.id,
        actor = values.entity_ref(action.doer),
        target = values.entity_ref(action.target),
        initial_target_owner = values.entity_ref(action.initialtargetowner),
        inventory_object = values.item_ref(action.invobject),
        position = values.position(point),
        recipe = values.text(action.recipe, 128),
        forced = action.forced == true,
    }
end

function actions.install()
    if BufferedAction == nil or type(BufferedAction.Do) ~= "function" then
        error("BufferedAction.Do is unavailable")
    end
    local original = BufferedAction.Do
    local capture_action = telemetry.guard("action.capture", capture)
    local publish = telemetry.guard("action.emit", function(snapshot, success, reason)
        if snapshot == nil then return end
        snapshot.success = not not success
        snapshot.reason = values.text(reason, 256)
        telemetry.emit("dst.player.action", snapshot)
    end)
    BufferedAction.Do = function(...)
        local action = ...
        if not state.telemetry_active
            or not state.action_allowlist[action.action ~= nil and action.action.id or nil]
            or action.doer == nil or not action.doer:HasTag("player") then
            return original(...)
        end
        local snapshot = capture_action(action)
        local results = telemetry.pack(original(...))
        publish(snapshot, results[1], results[2])
        return telemetry.unpack(results)
    end
end

return actions
