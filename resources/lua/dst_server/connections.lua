local telemetry = require("dst_server.telemetry")
local state = require("dst_server.state")
local values = require("dst_server.values")
local last_active, observed_since
local installed = false
local reliable = false
local observation_epoch = 0
local identities = {}

local function activity()
    local ok, now = pcall(GetTimeReal)
    if ok and type(now) == "number" then
        last_active = now / 1000
    else
        reliable = false
    end
end
local connections = {}

local function client_record(userid)
    return {
        userid = userid or json.null, steam_id = json.null, name = json.null,
        admin = json.null, moderator = json.null,
    }
end

local function is_steam_id(value)
    -- Public individual accounts; Lua numbers cannot preserve SteamID64 precision.
    return type(value) == "string" and #value == 17 and value:match("^%d+$")
        and value >= "76561197960265729" and value <= "76561202255233023"
end

function connections.steam_id(userid, netid)
    if type(userid) ~= "string" or userid == "" then return json.null end
    local client = identities[userid] or client_record(userid)
    identities[userid] = client
    if is_steam_id(netid) then
        client.steam_id = netid
    elseif client.steam_id == json.null then
        local ok, value = pcall(function() return TheNet:GetNetIdForUser(userid) end)
        if ok and is_steam_id(value) then client.steam_id = value end
    end
    return client.steam_id
end

function connections.client(userid, native)
    if type(userid) ~= "string" or userid == "" then return client_record(nil) end
    local client = identities[userid] or client_record(userid)
    identities[userid] = client
    if native == nil then
        local ok, value = pcall(function() return TheNet:GetClientTableForUser(userid) end)
        if ok and type(value) == "table" then native = value end
    end
    if native ~= nil then
        if type(native.name) == "string" then client.name = values.text(native.name, 256) end
        if type(native.admin) == "boolean" then client.admin = native.admin end
        if type(native.moderator) == "boolean" then client.moderator = native.moderator end
    end
    connections.steam_id(userid, native ~= nil and native.netid or nil)
    return client
end

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
            local client
            if event_name == "dst.client.authenticated" then
                identities[userid] = nil
                client = connections.client(userid)
            else
                client = identities[userid] or client_record(userid)
                identities[userid] = nil
            end
            telemetry.emit(event_name, client)
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
            clients[#clients + 1] = connections.client(userid, client)
        end
    end
    for userid in pairs(identities) do
        if not seen[userid] then identities[userid] = nil end
    end
    table.sort(clients, function(a, b) return a.userid < b.userid end)
    local players = {}
    for _, player in ipairs(AllPlayers) do
        if player:IsValid() and type(player.userid) == "string" and player.userid ~= "" then
            players[#players + 1] = {
                userid = player.userid, guid = player.GUID, prefab = player.prefab,
                is_ghost = player:HasTag("playerghost"),
            }
        end
    end
    table.sort(players, function(a, b) return a.guid < b.guid end)
    if #native_clients > 0 or #players > 0 then activity() end
    local world = TheWorld.state
    local cave = TheWorld:HasTag("cave")
    return {
        clients = clients,
        players = players,
        world = {
            season = values.text(world.season, 128),
            phase = values.text(world[cave and "cavephase" or "phase"], 128),
            moonphase = values.text(world[cave and "cavemoonphase" or "moonphase"], 128),
            nightmarephase = values.text(world.nightmarephase, 128),
            israining = world.israining == nil and json.null or world.israining,
            issnowing = world.issnowing == nil and json.null or world.issnowing,
            isacidraining = world.isacidraining == nil and json.null or world.isacidraining,
            islunarhailing = world.islunarhailing == nil and json.null or world.islunarhailing,
        },
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
        observation_epoch = observation_epoch,
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
