local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local gameplay_events = {}

local function on_success(stage, publish)
    local emit = telemetry.guard(stage, publish)
    return function(player, prefab, detail, ...)
        if (...) == true then emit(player, prefab, detail) end
        return ...
    end
end

local function observe_harvest(component, method, snapshot, publish)
    local original = component[method]
    assert(type(original) == "function", method .. " is unavailable")
    local stage = "harvest." .. method:lower()
    local before = telemetry.guard(stage .. ".before", function(self, player)
        if player:HasTag("player") then return snapshot(self) end
    end)
    local completed = on_success(stage .. ".emit", publish)
    component[method] = function(...)
        local self, player = ...
        -- The scythe passes TheWorld for each plant, then reports its total to the player.
        if player == nil or rawequal(player, TheWorld) then return original(...) end
        local prefab, chef_userid = before(self, player)
        if not prefab then return original(...) end
        return completed(player, prefab, chef_userid, original(...))
    end
end

function gameplay_events.install_picking()
    observe_harvest(require("components/pickable"), "Pick", function(self)
        return self.inst.prefab
    end, function(player, source)
        telemetry.play("dst.player.picked", { player = values.entity_ref(player), source = source, count = 1 })
    end)
end

function gameplay_events.install_cooking()
    observe_harvest(require("components/stewer"), "Harvest", function(self)
        -- Harvest clears the product, including when a different player empties the pot.
        return self.product, values.userid(self.chef_id)
    end, function(player, product, chef_userid)
        telemetry.play("dst.player.dish_harvested", {
            player = values.entity_ref(player), product = product, chef_userid = chef_userid or json.null,
        })
    end)
end

function gameplay_events.install_deployment()
    local component = require("components/deployable")
    local original = component.Deploy
    assert(type(original) == "function", "Deploy is unavailable")
    local before = telemetry.guard("deployment.before", function(self, point, player)
        -- Deploy can consume the item and already emits itemplanted for plants.
        if player and player:HasTag("player") and not self.inst:HasTag("deployedplant") then
            return self.inst.prefab, values.position(point)
        end
    end)
    local completed = on_success("deployment.emit", function(player, item, position)
        telemetry.play("dst.player.item_deployed", {
            player = values.entity_ref(player), item = item, position = position,
        })
    end)
    component.Deploy = function(...)
        local self, point, player = ...
        local item, position = before(self, point, player)
        if not item then return original(...) end
        return completed(player, item, position, original(...))
    end
end

return gameplay_events
