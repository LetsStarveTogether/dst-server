local telemetry = require("dst_server.telemetry")
local state = require("dst_server.state")
local last_active, observed_since
local installed = false
local reliable = false
local observation_epoch = 0

local function activity()
    local ok, now = pcall(GetTimeReal)
    if ok and type(now) == "number" then
        last_active = now / 1000
    else
        reliable = false
    end
end
local connections = {}

function connections.install(inst)
    for name, paused in pairs({ OnSimPaused = true, OnSimUnpaused = false }) do
        local original = _G[name]
        assert(type(original) == "function", name .. " is unavailable")
        local emit_pause = telemetry.guard("world." .. string.lower(name), function()
            telemetry.emit("dst.server.pause_changed", { domain = "simulation", paused = paused })
        end)
        _G[name] = function(...)
            state.sim_paused = paused
            local results = telemetry.pack(original(...))
            emit_pause()
            return telemetry.unpack(results)
        end
    end
    activity()
    observed_since = last_active
    reliable = true
    for _, name in ipairs({ "ms_playerjoined", "ms_playerleft" }) do
        inst:ListenForEvent(name, activity)
    end
    for native, event in pairs({
        ms_clientauthenticationcomplete = "dst.client.authenticated",
        ms_clientdisconnected = "dst.client.disconnected",
    }) do
        local event_name = event
        inst:ListenForEvent(native, telemetry.guard(event_name, function(_, data)
            activity()
            if data == nil then return end
            assert(type(data) == "table", "invalid client event")
            local userid = data.userid
            if userid == nil or userid == "" then return end
            assert(type(userid) == "string", "invalid client identity type")
            telemetry.emit(event_name, { userid = userid })
        end, true))
    end
    installed = true
end

local function snapshot()
    local clients, seen = {}, {}
    local native_clients = GetPlayerClientTable()
    for _, client in ipairs(native_clients) do
        local userid = client.userid
        if type(userid) == "string" and userid ~= "" and not seen[userid] then
            seen[userid] = true
            clients[#clients + 1] = userid
        end
    end
    table.sort(clients)
    local players = {}
    for _, player in ipairs(AllPlayers) do
        if player:IsValid() and type(player.userid) == "string" and player.userid ~= "" then
            players[#players + 1] = { userid = player.userid, guid = player.GUID }
        end
    end
    table.sort(players, function(a, b) return a.guid < b.guid end)
    if #native_clients > 0 or #players > 0 then activity() end
    return {
        clients = clients,
        players = players,
        max_players = TheNet:GetServerMaxPlayers(),
        health = require("dst_server").health(),
    }, #native_clients
end

function connections.presence()
    local data, count = snapshot()
    local now = GetTimeReal() / 1000
    if not reliable then
        observation_epoch = observation_epoch + 1
        observed_since, last_active, reliable = now, now, true
    end
    return {
        observation = string.format("%s:%.0f:%.0f", state.nonce, state.generation, observation_epoch),
        session_id = TheWorld.meta.session_identifier,
        client_count = count,
        player_count = #data.players,
        max_players = data.max_players,
        reliable = installed and reliable and observed_since ~= nil,
        idle_seconds = math.max(0, now - (last_active or now)),
        observed_seconds = math.max(0, now - (observed_since or now)),
        outdated_mods = state.outdated_mods,
    }
end

function connections.start(inst)
    local reason = "startup"
    -- Static tasks keep observing lobby connections while the simulation is paused.
    -- The first task runs after PostInit and temporary snapshot-player restoration.
    return inst:DoStaticPeriodicTask(60, telemetry.guard("presence.snapshot", function()
        local data = snapshot()
        data.reason = reason
        telemetry.emit("dst.server.presence", data)
        reason = "periodic"
    end, true), 0)
end

return connections
