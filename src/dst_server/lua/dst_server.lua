local actions = require("dst_server.actions")
local state = require("dst_server.state")
local world_events = require("dst_server.world_events")
local driver = {}
local methods = {}

for _, source in ipairs({
    require("dst_server.commands"),
    require("dst_server.player_queries"),
    require("dst_server.world_queries"),
}) do
    for name, callback in pairs(source) do
        methods[name] = callback
    end
end

function driver.health()
    return {
        protocol = state.protocol,
        installed = state.installed,
        profile = state.profile,
        events_emitted = state.events_emitted,
        errors = state.errors,
        players = GetTableSize(state.players),
        action_hook = state.action_hook,
        shard_hook = state.shard_hook,
    }
end

function driver.install(options)
    if state.installed then
        return driver.health()
    end
    if TheWorld == nil or not TheWorld.ismastersim then
        error("master simulation is unavailable")
    end
    if type(options) ~= "table" then
        error("driver options must be a table")
    end
    if type(options.nonce) ~= "string"
        or #options.nonce < 16
        or #options.nonce > 128 then
        error("nonce must contain 16 to 128 bytes")
    end
    if options.profile ~= "off"
        and options.profile ~= "critical"
        and options.profile ~= "history" then
        error("unknown telemetry profile")
    end
    if type(options.actions) ~= "table" then
        error("actions must be a table")
    end

    state.nonce = options.nonce
    state.profile = options.profile
    for _, action_id in ipairs(options.actions) do
        if type(action_id) ~= "string" or action_id == "" then
            error("action IDs must be non-empty strings")
        end
        state.action_allowlist[action_id] = true
    end

    if state.profile ~= "off" then
        if type(TheWorld.ListenForEvent) ~= "function"
            or type(TheWorld.WatchWorldState) ~= "function"
            or type(json.encode_compliant) ~= "function"
            or type(GetTick) ~= "function"
            or type(GetTimeReal) ~= "function" then
            error("required telemetry API is unavailable")
        end
        if state.profile == "history" then
            actions.install()
        end
        world_events.install_shard()
        world_events.install_world()
    end
    state.installed = true
    return driver.health()
end

methods.health = driver.health

function driver.call(name, args)
    if not state.installed then
        error("driver is not installed")
    end
    local method = methods[name]
    if method == nil then
        error("unknown driver method: " .. tostring(name))
    end
    return method(args or {})
end

return driver
