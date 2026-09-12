local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local connections = {}

function connections.install(inst)
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
