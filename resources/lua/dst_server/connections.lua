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

function connections.loaded(player)
    local handshake = rawget(_G, "POSTACTIVATEHANDSHAKE")
    local ready = type(handshake) == "table" and handshake.READY
    local step = player and player._PostActivateHandshakeState_Server
    if type(step) == "number" and type(ready) == "number" and step >= 0 and step <= ready then
        return step == ready
    end
    return json.null
end

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

function connections.client(userid, native)
    if type(userid) ~= "string" or userid == "" then return client_record(nil) end
    local client = identities[userid]
    if client == nil then
        client = client_record(userid)
        identities[userid] = client
    end
    if native ~= nil then
        if is_steam_id(native.netid) then client.steam_id = native.netid end
        if type(native.name) == "string" then client.name = values.text(native.name, 256) end
        if type(native.admin) == "boolean" then client.admin = native.admin end
        if type(native.moderator) == "boolean" then client.moderator = native.moderator end
    end
    return client
end

function connections.install(inst)
    local capture = state.telemetry_active
    for name, paused in pairs({ OnSimPaused = true, OnSimUnpaused = false }) do
        local original = _G[name]
        assert(type(original) == "function", name .. " is unavailable")
        local emit_pause = capture and telemetry.guard("world." .. string.lower(name), function()
            telemetry.emit("dst.server.pause_changed", { domain = "simulation", paused = paused })
        end)
        local function after_pause(...)
            emit_pause()
            return ...
        end
        _G[name] = function(...)
            state.sim_paused = paused
            if emit_pause then return after_pause(original(...)) end
            return original(...)
        end
    end
    activity()
    assert(last_active ~= nil, "real time is unavailable")
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
            local userid = type(data) == "table" and values.userid(data.userid)
            if not userid then return end
            identities[userid] = nil
            if capture then telemetry.emit(event_name, { userid = userid }) end
        end, true))
    end
    installed = true
end

local function optional_flag(value)
    if type(value) == "boolean" then return value end
    return json.null
end

local function rift_flag(spawner, method)
    if spawner ~= nil and type(spawner[method]) == "function" then
        local ok, value = pcall(spawner[method], spawner)
        if ok then return optional_flag(value) end
    end
    return json.null
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
    for userid in pairs(identities) do
        if not seen[userid] then identities[userid] = nil end
    end
    local players = {}
    for _, player in ipairs(AllPlayers) do
        local userid = player:IsValid() and values.entity_userid(player)
        if userid and type(player.prefab) == "string" and player.prefab ~= "" then
            players[#players + 1] = {
                userid = userid, prefab = player.prefab, loaded = connections.loaded(player),
                is_ghost = player:HasTag("playerghost"),
            }
        end
    end
    local world = TheWorld.state
    local cave = TheWorld:HasTag("cave")
    local rifts = TheWorld.components and TheWorld.components.riftspawner
    return {
        clients = clients,
        players = players,
        world = {
            season = values.text(world.season, 128),
            phase = values.text(world[cave and "cavephase" or "phase"], 128),
            moonphase = values.text(world[cave and "cavemoonphase" or "moonphase"], 128),
            nightmarephase = values.text(world.nightmarephase, 128),
            israining = optional_flag(world.israining),
            issnowing = optional_flag(world.issnowing),
            isacidraining = optional_flag(world.isacidraining),
            islunarhailing = optional_flag(world.islunarhailing),
            lunar_rifts_enabled = rift_flag(rifts, "GetLunarRiftsEnabled"),
            shadow_rifts_enabled = rift_flag(rifts, "GetShadowRiftsEnabled"),
        },
    }
end

function connections.presence()
    local client_count = #GetPlayerClientTable()
    local player_count = 0
    for _, player in ipairs(AllPlayers) do
        if player:IsValid() and values.entity_userid(player) then player_count = player_count + 1 end
    end
    local now = GetTimeReal() / 1000
    if not reliable then
        observation_epoch = observation_epoch + 1
        observed_since, last_active, reliable = now, now, true
    elseif client_count > 0 or player_count > 0 then
        last_active = now
    end
    local cycles = TheWorld.state and TheWorld.state.cycles
    return {
        day = type(cycles) == "number" and cycles + 1 or json.null,
        observation_epoch = observation_epoch,
        session_id = TheWorld.meta.session_identifier,
        client_count = client_count,
        player_count = player_count,
        max_players = TheNet:GetServerMaxPlayers(),
        reliable = installed and reliable and observed_since ~= nil,
        idle_seconds = math.max(0, now - (last_active or now)),
        observed_seconds = math.max(0, now - (observed_since or now)),
        outdated_mods = state.outdated_mods,
    }
end

function connections.start(inst)
    if not state.telemetry_active then return nil end
    local reason = "startup"
    -- Static tasks keep observing lobby connections while the simulation is paused.
    -- The first task runs after PostInit and temporary snapshot-player restoration.
    return inst:DoStaticPeriodicTask(60, telemetry.guard("presence.snapshot", function()
        local data = snapshot()
        data.reason = reason
        telemetry.emit("dst.server.presence", data)
        reason = "periodic"
    end), 0)
end

return connections
