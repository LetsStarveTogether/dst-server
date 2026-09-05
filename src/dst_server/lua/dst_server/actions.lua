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

    -- GetPosition clears invalid platform references; resolve a copy.
    local point = action:GetDynamicActionPoint()
    point = point ~= nil and point.GetPosition(shallowcopy(point)) or nil
    local snapshot = {
        action_id = action_id,
        actor = values.entity_ref(actor),
        target = values.entity_ref(action.target),
        initial_target_owner = values.entity_ref(action.initialtargetowner),
        inventory_object = values.item_ref(action.invobject),
        position = values.position(point),
        recipe = values.text(action.recipe, 128),
        forced = action.forced == true,
    }
    state.action_sequence = state.action_sequence + 1
    snapshot.action_sequence = state.action_sequence
    return snapshot
end

local function trace_error(failure)
    pcall(function() print(debug.traceback("DST action failed", 3)) end)
    return failure
end

function actions.install()
    if BufferedAction == nil or type(BufferedAction.Do) ~= "function" then
        error("BufferedAction.Do is unavailable")
    end
    local original = BufferedAction.Do
    BufferedAction.Do = function(...)
        if not state.telemetry_active then
            return original(...)
        end

        local action = ...
        local captured, snapshot = pcall(capture, action)
        if not captured then
            telemetry.report("action.capture", "callback_failed")
            snapshot = nil
        end
        local previous = state.current_action
        state.current_action = snapshot ~= nil and {
            actor = action.doer,
            sequence = snapshot.action_sequence,
        } or nil
        local results = telemetry.pack(xpcall(original, trace_error, ...))
        state.current_action = previous

        if snapshot ~= nil then
            telemetry.guard("action.emit", function()
                snapshot.success = results[1] and not not results[2]
                snapshot.reason = results[1] and values.text(results[3], 256) or json.null
                snapshot.error = results[1] and json.null or "lua_error"
                telemetry.emit("dst.player.action", snapshot)
            end)()
        end
        if not results[1] then
            error(results[2], 0)
        end
        return telemetry.unpack(results, 2)
    end
end

return actions
