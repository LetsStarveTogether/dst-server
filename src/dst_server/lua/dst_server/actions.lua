local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local actions = {}

local function capture(action)
    local action_id = action.action ~= nil and action.action.id or nil
    if not state.action_allowlist[action_id] then
        return nil
    end
    local actor = action.doer
    if actor == nil or not actor:HasTag("player") then
        return nil
    end

    state.action_sequence = state.action_sequence + 1
    action._dst_action_seq = state.action_sequence
    local point = action.GetActionPoint ~= nil and action:GetActionPoint() or nil
    return {
        action_id = tostring(action_id),
        action_sequence = action._dst_action_seq,
        actor = values.entity_ref(actor),
        target = values.entity_ref(action.target),
        initial_target_owner = values.entity_ref(action.initialtargetowner),
        inventory_object = values.item_ref(action.invobject),
        position = values.position(point),
        recipe = values.text(action.recipe, 128),
        forced = action.forced == true,
    }
end

local function emit(snapshot, results)
    snapshot.success = results[1] == true
    snapshot.reason = values.text(results[2], 256)
    telemetry.emit("dst.player.action", snapshot)
end

function actions.install()
    if BufferedAction == nil or type(BufferedAction.Do) ~= "function" then
        error("BufferedAction.Do is unavailable")
    end
    if BufferedAction._dst_original_do ~= nil then
        return
    end

    local original_do = BufferedAction.Do
    BufferedAction._dst_original_do = original_do
    BufferedAction.Do = function(...)
        if not state.telemetry_active then
            return original_do(...)
        end

        local action = ...
        local captured, snapshot = pcall(capture, action)
        if not captured then
            state.errors = state.errors + 1
            return original_do(...)
        end
        if snapshot == nil then
            return original_do(...)
        end

        local results = telemetry.pack(original_do(...))
        local emitted = pcall(emit, snapshot, results)
        if not emitted then
            state.errors = state.errors + 1
        end
        return telemetry.unpack(results)
    end
end

return actions
