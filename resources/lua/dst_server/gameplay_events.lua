local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local gameplay_events = {}

function gameplay_events.install_deliveries()
    local deliverable = require("components/mapdeliverable")
    local send = assert(deliverable.SendToPoint)
    local capture = telemetry.guard("map_delivery.started", function(component, actor)
        telemetry.emit("dst.world.map_delivery_started", {
            item = values.entity_ref(component.inst),
            actor = values.entity_ref(actor),
            origin = values.position(component.origin),
            destination = values.position(component.dest),
        })
    end)
    deliverable.SendToPoint = function(self, point, actor, ...)
        local result = telemetry.pack(send(self, point, actor, ...))
        if result[1] then capture(self, actor) end
        return telemetry.unpack(result)
    end
end

local function vault_progress(inst, trigger)
    local sockets, sparks = 0, 0
    for i = 1, 4 do
        local socket = inst.components.entitytracker:GetEntity("socket" .. i)
        local activator = inst.components.entitytracker:GetEntity("activator" .. i)
        if socket ~= nil and socket:IsSocketed() then sockets = sockets + 1 end
        if activator ~= nil and activator:GotSpark() then sparks = sparks + 1 end
    end
    telemetry.emit("dst.world.vault_trial_progress", {
        trial = values.entity_ref(inst), trigger = trigger, sockets = sockets, sparks = sparks,
    })
end

local function wrap_vault_callback(inst, name, event, capture)
    local original = assert(inst[name])
    local callback = function(...)
        local results = telemetry.pack(original(...))
        capture(...)
        return telemetry.unpack(results)
    end
    inst[name] = callback
    -- The driver may initialize after native OnLoadPostPass bound these callbacks.
    -- Keep callback order in both native listener indexes; future bindings use inst[name].
    for source, callbacks in pairs(inst.event_listening ~= nil and inst.event_listening[event] or {}) do
        for index, registered in ipairs(callbacks) do
            if registered == original then callbacks[index] = callback end
        end
        for index, registered in ipairs(source.event_listeners[event][inst]) do
            if registered == original then source.event_listeners[event][inst][index] = callback end
        end
    end
end

function gameplay_events.install_vault_trials()
    local prefab = Prefabs ~= nil and Prefabs.vault_key_trial or nil
    if prefab == nil then return false end
    local attach = telemetry.guard("vault_trial.attach", function(inst)
        -- Check native boundaries before replacing any callback.
        for _, name in ipairs({ "_onvaultsocketed_changed", "_onvaultactivator_changed", "OnLoadPostPass", "_onguarddied" }) do
            assert(type(inst[name]) == "function", name .. " is unavailable")
        end
        wrap_vault_callback(inst, "_onvaultsocketed_changed", "ms_vaultsocketed_changed",
            telemetry.guard("vault_trial.socket", function() vault_progress(inst, "socket") end))
        wrap_vault_callback(inst, "_onvaultactivator_changed", "ms_vaultactivator_changed",
            telemetry.guard("vault_trial.activator", function() vault_progress(inst, "activator") end))
        wrap_vault_callback(inst, "OnLoadPostPass", nil,
            telemetry.guard("vault_trial.loaded", function() vault_progress(inst, "loaded") end))
        wrap_vault_callback(inst, "_onguarddied", "death",
            telemetry.guard("vault_trial.guards_defeated", function(guard)
                -- The native callback alone decides whether all guards were defeated
                -- and enables the bonus loot; this is not whole-trial completion.
                if guard._vault_death_loot then
                    telemetry.emit("dst.world.vault_trial_guards_defeated", {
                        trial = values.entity_ref(inst),
                        last_guard = values.entity_ref(guard),
                        bonus_loot = true,
                    })
                end
            end))
    end, true)
    local original = prefab.fn
    prefab.fn = function(...)
        local inst = original(...)
        if inst ~= nil then attach(inst) end
        return inst
    end
    for _, inst in pairs(Ents) do
        if inst.prefab == "vault_key_trial" then attach(inst) end
    end
end

return gameplay_events
