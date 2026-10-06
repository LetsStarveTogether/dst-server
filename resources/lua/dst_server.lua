local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local connections = require("dst_server.connections")
local driver = {}
local methods = {}

for _, source in ipairs({
    require("dst_server.commands"),
    require("dst_server.console"),
    require("dst_server.player_queries"),
    require("dst_server.world_queries"),
    require("dst_server.voting").methods,
}) do
    for name, callback in pairs(source) do
        methods[name] = callback
    end
end

function driver.health()
    local telemetry_status = state.requested_profile == "off" and "disabled"
        or state.telemetry_active and (next(state.faults) ~= nil and "degraded" or "active")
        or "failed"
    return {
        generation = state.generation,
        control_ready = state.control_ready,
        profile = state.requested_profile,
        telemetry_status = telemetry_status,
        last_error = state.last_error or json.null,
        events_emitted = state.sequence,
        errors = state.errors,
        play_queue_dropped = state.play_queue_dropped,
        revision = state.health_revision,
        capabilities = require("dst_server.wire").object(state.capabilities),
        faults = require("dst_server.wire").object(state.faults),
    }
end

function driver.install(options)
    if state.installed then error("driver is already installed") end
    if type(options) ~= "table" then
        error("driver options must be a table")
    end
    local nonce = options.nonce
    if type(nonce) ~= "string"
        or #nonce ~= 26
        or string.match(nonce, "^[0-7][0-9A-HJKMNP-TV-Z]+$") == nil then
        error("nonce must be a canonical ULID")
    end
    local profile = options.profile
    if profile ~= "off" and profile ~= "timeline" and profile ~= "play" then
        error("unknown telemetry profile")
    end
    local generation = options.generation
    if type(generation) ~= "number" or generation < 0 or generation > 9007199254740991
        or generation ~= math.floor(generation) then
        error("generation must be a non-negative safe integer")
    end
    if TheWorld == nil or not TheWorld.ismastersim then
        error("master simulation is unavailable")
    end
    state.nonce = nonce
    state.generation = generation
    state.requested_profile = profile

    local original = Networking_ModOutOfDateAnnouncement
    if type(original) ~= "function" then
        error("Networking_ModOutOfDateAnnouncement is unavailable")
    end
    Networking_ModOutOfDateAnnouncement = function(...)
        local mod = ...
        if type(mod) == "string" then
            local found = false
            for _, name in ipairs(state.outdated_mods) do if name == mod then found = true end end
            if not found then state.outdated_mods[#state.outdated_mods + 1] = mod end
        end
        return original(...)
    end
    state.installed = true
    for _, name in ipairs({ "connections", "voting", "players", "player_lifecycle", "world", "public_chat", "votes",
        "presence", "picking", "cooking", "deployment", "actions", "dice" }) do
        state.capabilities[name] = "disabled"
    end
    if profile == "off" then return driver.health() end
    if type(GetTick) ~= "function" or type(GetTimeReal) ~= "function" then
        telemetry.report("clocks.install", "installation_failed")
        return driver.health()
    end
    state.telemetry_active = true
    telemetry.install("players", function() require("dst_server.world_events").install_players() end)
    telemetry.install("player_lifecycle", function() require("dst_server.player_events").install_lifecycle() end)
    telemetry.install("world", function() require("dst_server.world_events").install_world() end)
    telemetry.install("public_chat", function() return require("dst_server.message_events").install_public_chat() end)
    if profile == "play" then
        telemetry.install("dice", function() return require("dst_server.message_events").install_dice() end)
        telemetry.install("picking", function() return require("dst_server.gameplay_events").install_picking() end)
        telemetry.install("cooking", function() return require("dst_server.gameplay_events").install_cooking() end)
        telemetry.install("deployment", function() require("dst_server.gameplay_events").install_deployment() end)
        telemetry.install("actions", function() return require("dst_server.gameplay_actions").install() end)
    end
    return driver.health()
end

methods.health = driver.health
methods.presence = connections.presence

function driver.call(name, args)
    if not state.installed then
        error("driver is not installed")
    end
    local method = methods[name]
    if method == nil then
        error("unknown driver method: " .. tostring(name))
    end
    args = args or {}
    if args._expected_session_id ~= nil or args._expected_guid ~= nil then
        local player = type(args.userid) == "string" and LookupPlayerInstByUserID(args.userid) or nil
        if TheWorld == nil or TheWorld.meta == nil
            or TheWorld.meta.session_identifier ~= args._expected_session_id
            or state.generation ~= args._expected_generation
            or player == nil or not player:IsValid() or player._despawning
            or player.GUID ~= args._expected_guid or connections.loaded(player) == false then
            require("dst_server.wire").stale_reference()
        end
    end
    return method(args)
end

return driver
