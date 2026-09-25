local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
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
            telemetry.emit(event_name, { userid = values.required_string(data, "userid") })
        end, true))
    end
end

local function snapshot(reason)
    local clients, seen = {}, {}
    for _, client in ipairs(GetPlayerClientTable()) do
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
    telemetry.emit("dst.server.presence", {
        reason = reason,
        clients = clients,
        players = players,
        max_players = TheNet:GetServerMaxPlayers(),
        health = require("dst_server").health(),
    })
end

function connections.start(inst)
    local reason = "startup"
    -- Static tasks keep observing lobby connections while the simulation is paused.
    -- The first task runs after PostInit and temporary snapshot-player restoration.
    return inst:DoStaticPeriodicTask(60, telemetry.guard("presence.snapshot", function()
        snapshot(reason)
        reason = "periodic"
    end, true), 0)
end

return connections
