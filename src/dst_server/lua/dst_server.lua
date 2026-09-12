local state = require("dst_server.state")
local driver = {}
local methods = {}

for _, source in ipairs({
    require("dst_server.commands"),
    require("dst_server.console"),
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
    state.nonce = nonce
    state.generation = generation
    state.requested_profile = profile
    state.action_allowlist = action_allowlist

    local original = Networking_ModOutOfDateAnnouncement
    if type(original) ~= "function" then
        error("Networking_ModOutOfDateAnnouncement is unavailable")
    end
    local telemetry = require("dst_server.telemetry")
    Networking_ModOutOfDateAnnouncement = function(...)
        local mod = ...
        -- This is required control state, including when optional telemetry is off.
        -- Capture first so announcement failures cannot hide the native detection.
        pcall(telemetry.emit, "dst.mod.outdated", { name = mod })
        return original(...)
    end
    state.installed = true
    local installed = false
    local function install(stage, callback)
        if pcall(callback) then
            installed = true
        else
            telemetry.report(stage, "installation_failed")
        end
    end
    install("players.install", function() require("dst_server.world_events").install_players() end)
    if profile == "off" then return driver.health() end
    if type(GetTick) ~= "function" or type(GetTimeReal) ~= "function" then
        telemetry.report("clocks.install", "installation_failed")
        return driver.health()
    end
    install("shards.install", function() require("dst_server.world_events").install_shard() end)
    install("world.install", function() require("dst_server.world_events").install_world() end)
    install("messages.install", function() require("dst_server.message_events").install() end)
    if profile == "history" and next(action_allowlist) ~= nil then
        install("actions.install", function() require("dst_server.actions").install() end)
    end
    state.telemetry_active = installed
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
