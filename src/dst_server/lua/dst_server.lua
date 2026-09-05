local state = require("dst_server.state")
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
    local telemetry_status = state.requested_profile == "off" and "disabled"
        or state.telemetry_active and (state.errors > 0 and "degraded" or "active")
        or "failed"
    return {
        protocol = state.protocol,
        generation = state.generation,
        telemetry_status = telemetry_status,
        last_error = state.last_error or json.null,
        events_emitted = state.sequence,
        errors = state.errors,
    }
end

function driver.install(options)
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
    if profile ~= "off" and profile ~= "critical" and profile ~= "history" then
        error("unknown telemetry profile")
    end
    if type(options.actions) ~= "table" then
        error("actions must be a table")
    end
    local generation = options.generation
    if type(generation) ~= "number" or generation < 0 or generation > 9007199254740991
        or generation ~= math.floor(generation) then
        error("generation must be a non-negative safe integer")
    end
    local action_allowlist = {}
    for _, action_id in pairs(options.actions) do
        if type(action_id) ~= "string" or action_id == "" then
            error("action IDs must be non-empty strings")
        end
        action_allowlist[action_id] = true
    end
    if TheWorld == nil or not TheWorld.ismastersim then
        error("master simulation is unavailable")
    end
    if state.installed then
        if nonce ~= state.nonce or generation < state.generation or profile ~= state.requested_profile
            or TheWorld ~= state.world then
            error("driver is already installed with different options")
        end
        for name in pairs(action_allowlist) do
            if not state.action_allowlist[name] then error("driver action allowlist differs") end
        end
        for name in pairs(state.action_allowlist) do
            if not action_allowlist[name] then error("driver action allowlist differs") end
        end
        -- Native Session notifications can reach the host after this VM is installed.
        state.generation = generation
        return driver.health()
    end

    state.nonce = nonce
    state.generation = generation
    state.world = TheWorld
    state.requested_profile = profile
    state.action_allowlist = action_allowlist
    state.installed = true
    if profile == "off" then
        return driver.health()
    end

    local ok = pcall(function()
        if type(GetTick) ~= "function" or type(GetTimeReal) ~= "function" then
            error("required telemetry clock is unavailable")
        end
        local world_events = require("dst_server.world_events")
        world_events.install_shard()
        world_events.install_world()
        if profile == "history" and next(action_allowlist) ~= nil then
            require("dst_server.actions").install()
        end
    end)
    if ok then
        state.telemetry_active = true
    else
        require("dst_server.telemetry").report("install", "installation_failed")
    end
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
