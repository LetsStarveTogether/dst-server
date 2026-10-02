local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local container_events = {}

local publish = telemetry.guard("container.transfer", function(snapshot, component, slot)
    if snapshot == nil then return end
    local before = snapshot.before
    local after = values.item_ref(component:GetItemInSlot(slot))
    if before == after or before ~= json.null and after ~= json.null
        and before.guid == after.guid and before.stack_size == after.stack_size then return end
    snapshot.after = after
    telemetry.emit("dst.player.container_transfer", snapshot)
end)

local function wrap(component, name, opener_arg, target_arg)
    local original = assert(component[name])
    local capture = telemetry.guard("container.capture", function(self, slot, ...)
        local player = opener_arg ~= nil and select(opener_arg, ...) or self.inst
        if player == nil or not player:HasTag("player") then return end
        local before = values.item_ref(self:GetItemInSlot(slot))
        -- These branches delegate to another wrapped handler, which captures the change.
        if name == "PutAllOfActiveItemInSlot" and before ~= json.null
            or name == "SwapActiveItemWithSlot" and before == json.null then return end
        return {
            player = values.entity_ref(player),
            holder = values.entity_ref(self.inst),
            target = values.entity_ref(target_arg ~= nil and select(target_arg, ...) or player),
            slot = slot,
            before = before,
        }
    end)
    component[name] = function(self, slot, ...)
        local snapshot = capture(self, slot, ...)
        local results = telemetry.pack(original(self, slot, ...))
        publish(snapshot, self, slot)
        return telemetry.unpack(results)
    end
end

function container_events.install()
    local container = require("components/container")
    local inventory = require("components/inventory")
    for _, name in ipairs({
        "PutOneOfActiveItemInSlot", "PutAllOfActiveItemInSlot",
        "TakeActiveItemFromHalfOfSlot", "TakeActiveItemFromAllOfSlot",
        "AddOneOfActiveItemToSlot", "AddAllOfActiveItemToSlot",
        "SwapActiveItemWithSlot", "SwapOneOfActiveItemWithSlot",
    }) do
        wrap(container, name, 1)
    end
    wrap(container, "TakeActiveItemFromCountOfSlot", 2)
    for _, name in ipairs({ "MoveItemFromAllOfSlot", "MoveItemFromHalfOfSlot", "MoveItemFromCountOfSlot" }) do
        wrap(container, name, name == "MoveItemFromCountOfSlot" and 3 or 2, 1)
        wrap(inventory, name, nil, 1)
    end
end

return container_events
