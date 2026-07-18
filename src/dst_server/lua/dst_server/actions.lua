local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local actions = {}

local function capture(action)
    local point = action.GetActionPoint ~= nil and action:GetActionPoint() or nil
    return {
        action_id = tostring(action.action.id),
        action_sequence = action._dst_action_seq,
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
    if BufferedAction._dst_original_do ~= nil then
        state.action_hook = true
        return
    end

    local original_do = BufferedAction.Do
    BufferedAction._dst_original_do = original_do
    BufferedAction.Do = function(action, ...)
        local actor = action.doer
        local action_id = action.action ~= nil and action.action.id or nil
        if actor == nil
            or not actor:HasTag("player")
            or not state.action_allowlist[action_id] then
            return original_do(action, ...)
        end

        state.action_sequence = state.action_sequence + 1
        action._dst_action_seq = state.action_sequence
        local captured, snapshot = pcall(capture, action)
        local results = telemetry.pack(original_do(action, ...))
        if captured then
            snapshot.success = results[1] == true
            snapshot.reason = values.text(results[2], 256)
            telemetry.safe_emit("dst.player.action", snapshot)
        else
            state.errors = state.errors + 1
        end
        return telemetry.unpack(results)
    end
    state.action_hook = true
end

return actions
