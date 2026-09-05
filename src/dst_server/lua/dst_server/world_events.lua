local player_events = require("dst_server.player_events")
local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local world_events = {}

function world_events.install_world()
    TheWorld:ListenForEvent("ms_playerjoined", telemetry.guard("world.ms_playerjoined", function(_, player)
        player_events.attach(player)
        telemetry.emit("dst.player.shard_entered", { player = values.entity_ref(player) })
    end))
    TheWorld:ListenForEvent("ms_playerleft", telemetry.guard("world.ms_playerleft", function(_, player)
        telemetry.emit("dst.player.shard_left", { player = values.entity_ref(player) })
    end))
    TheWorld:ListenForEvent("ms_playerdisconnected", telemetry.guard("world.ms_playerdisconnected", function(_, data)
        telemetry.emit("dst.player.disconnected", {
            player = values.entity_ref(data.player),
            expected = data.wasExpected == true,
        })
    end))
    TheWorld:ListenForEvent("ms_playerdespawnandmigrate", telemetry.guard("world.ms_playerdespawnandmigrate", function(_, data)
        telemetry.emit("dst.player.migration_started", {
            player = values.entity_ref(data.player),
            destination_shard_id = tostring(data.worldid),
            portal_id = type(data.portalid) == "string" and data.portalid or values.optional_json_number(data.portalid),
            destination = values.position(data),
        })
    end))
    TheWorld:ListenForEvent("ms_newplayerspawned", telemetry.guard("world.ms_newplayerspawned", function(_, player)
        -- New-spawn notification precedes final placement by playerspawner.
        local reference = values.entity_ref(player)
        reference.position = json.null
        telemetry.emit("dst.player.spawned", { player = reference })
    end))
    TheWorld:ListenForEvent("entity_death", telemetry.guard("world.entity_death", function(_, data)
        local attributed = values.player_for(data.afflicter)
        local victim = data.inst
        if victim ~= nil and (
            victim.userid ~= nil
            or victim:HasTag("player")
            or victim:HasTag("epic")
            or attributed ~= nil
        ) then
            telemetry.emit("dst.entity.death", {
                victim = values.entity_ref(victim),
                cause = values.text(data.cause, 256),
                afflicter = values.entity_ref(data.afflicter),
                attributed_player = values.entity_ref(attributed),
                corpsing = data.corpsing == true,
                caused_by_action_sequence = values.current_action_sequence(data.afflicter),
            })
        end
    end))
    TheWorld:ListenForEvent("master_shardbossdefeated", telemetry.guard("world.master_shardbossdefeated", function(_, data)
        telemetry.emit("dst.world.shard_boss_defeated", {
            prefab = tostring(data.bossprefab),
            shard_id = tostring(data.shardid),
        })
    end))
    TheWorld:ListenForEvent("lunarrift_opened", telemetry.guard("world.lunarrift_opened", function()
        telemetry.emit("dst.world.rift_unlocked", { kind = "lunar" })
    end))
    TheWorld:ListenForEvent("shadowrift_opened", telemetry.guard("world.shadowrift_opened", function()
        telemetry.emit("dst.world.rift_unlocked", { kind = "shadow" })
    end))
    TheWorld:ListenForEvent("ms_riftaddedtopool", telemetry.guard("world.ms_riftaddedtopool", function(_, data)
        telemetry.emit("dst.world.rift_changed", {
            rift = values.entity_ref(data.rift),
            active = true,
        })
    end))
    TheWorld:ListenForEvent("ms_riftremovedfrompool", telemetry.guard("world.ms_riftremovedfrompool", function(_, data)
        telemetry.emit("dst.world.rift_changed", {
            rift = values.entity_ref(data.rift),
            active = false,
        })
    end))

    if state.requested_profile == "history" then
        TheWorld:ListenForEvent("itemplanted", telemetry.guard("world.itemplanted", function(_, data)
            local player = data.doer
            if player ~= nil and player.userid ~= nil and player:HasTag("player") then
                local event = values.player_data(player)
                event.position = values.position(data.pos)
                telemetry.emit("dst.player.planted", event)
            end
        end))
    end

    local state_names = {
        "cycles",
        TheWorld:HasTag("cave") and "cavephase" or "phase",
        "season",
        TheWorld:HasTag("cave") and "cavemoonphase" or "moonphase",
        "nightmarephase",
        "israining",
        "issnowing",
        "isacidraining",
        "islunarhailing",
    }
    for _, name in ipairs(state_names) do
        local state_name = name
        TheWorld:WatchWorldState(state_name, telemetry.guard("world." .. state_name, function(_, value)
            telemetry.emit("dst.world.state_changed", {
                name = state_name,
                value = value,
            })
        end))
    end

    for _, player in ipairs(AllPlayers) do
        player_events.attach(player)
    end
end

function world_events.install_shard()
    if type(Shard_UpdateWorldState) ~= "function" then
        error("Shard_UpdateWorldState is unavailable")
    end
    local original = Shard_UpdateWorldState
    Shard_UpdateWorldState = function(...)
        if not state.telemetry_active then
            return original(...)
        end

        local world_id, shard_state, tags, _, shard_name = ...
        local results = telemetry.pack(original(...))
        local emitted = pcall(function()
            telemetry.emit("dst.shard.connection_changed", {
                shard_id = tostring(world_id),
                name = tostring(shard_name or ""),
                ready = shard_state == REMOTESHARDSTATE.READY,
                tags = values.tags(tags),
            })
        end)
        if not emitted then
            telemetry.report("shard.update", "callback_failed")
        end
        return telemetry.unpack(results)
    end
end

return world_events
