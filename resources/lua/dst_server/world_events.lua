local player_events = require("dst_server.player_events")
local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local world_events = {}

function world_events.install_players()
    local attach = telemetry.guard("player.attach", player_events.attach, true)
    TheWorld:ListenForEvent("ms_playerjoined", function(_, player) attach(player) end)
    for _, player in ipairs(AllPlayers) do attach(player) end
end

local function listen(name, callback)
    TheWorld:ListenForEvent(name, telemetry.guard("world." .. name, callback))
end

local function watch(name)
    TheWorld:WatchWorldState(name, telemetry.guard("world." .. name, function(_, value)
        telemetry.emit("dst.world.state_changed", { name = name, value = value })
    end))
end

local owner_userid = telemetry.guard("death.owner", values.owner_userid)

function world_events.install_world()
    local play = state.requested_profile == "play"
    listen("serverpauseddirty", function(_, data)
        telemetry.emit("dst.server.pause_changed", {
            domain = "server",
            pause = not not data.pause,
            autopause = not not data.autopause,
            gameautopause = not not data.gameautopause,
            source = data.source or json.null,
        })
    end)
    listen("ms_playerleft", player_events.shard_left)
    listen("ms_playerdespawnandmigrate", function(_, data)
        telemetry.emit("dst.player.migration_started", {
            player = values.entity_ref(data.player), destination_shard_id = tostring(data.worldid),
        })
    end)
    listen("entity_death", function(_, data)
        local victim = data.inst
        if victim == nil then return end
        local is_epic = victim:HasTag("epic")
        local major = is_epic or values.is_player(victim)
        if not play and not major then return end

        local direct = values.player_userid(data.afflicter)
        local owner
        if direct == nil and data.afflicter ~= nil then owner = owner_userid(data.afflicter) end
        if not major and direct == nil and owner == nil then return end
        local emit = major and telemetry.emit or telemetry.play
        emit("dst.entity.death", {
            victim = values.entity_ref(victim), afflicter = values.entity_ref(data.afflicter),
            attributed_userid = owner or json.null, is_epic = is_epic,
            age_seconds = values.player_age(victim), cause = values.text(data.cause, 256),
            corpsing = data.corpsing == true, position = values.entity_position(victim),
        })
    end)
    if TheWorld.ismastershard then
        listen("master_shardbossdefeated", function(_, data)
            if data.bossprefab == "daywalker" then
                telemetry.emit("dst.world.daywalker_defeated", { shard_id = tostring(data.shardid) })
            end
        end)
        watch("cycles")
        watch("season")
    end

    listen("lunarrift_opened", function()
        telemetry.emit("dst.world.rift_unlocked", { kind = "lunar" })
    end)
    listen("shadowrift_opened", function()
        telemetry.emit("dst.world.rift_unlocked", { kind = "shadow" })
    end)
    local function rift_changed(rift, active)
        -- LoadPostPass adds existing rifts back to the pool while the world is populating.
        if not POPULATING then
            telemetry.emit("dst.world.rift_changed", { rift = values.entity_ref(rift), active = active })
        end
    end
    listen("ms_riftaddedtopool", function(_, data) rift_changed(data.rift, true) end)
    listen("ms_riftremovedfrompool", function(_, data) rift_changed(data.rift, false) end)

    watch("israining")
    if TheWorld:HasTag("cave") then
        watch("cavephase")
        watch("cavemoonphase")
        watch("nightmarephase")
        watch("isacidraining")
    else
        watch("phase")
        watch("moonphase")
        watch("issnowing")
        watch("islunarhailing")
    end
    if play then
        listen("itemplanted", function(_, data)
            if data.doer and data.doer:HasTag("player") then
                telemetry.play("dst.player.planted", {
                    player = values.entity_ref(data.doer), position = values.position(data.pos),
                })
            end
        end)
    end
end

return world_events
