local values = require("dst_server.values")
local commands = {}
local MAX_GIVE_ITEMS = 64
local save_pending = false

function commands.announce(args)
    local message = values.required_string(args, "message")
    local count = args.count == nil and 1 or values.required_integer(args, "count", 1)
    if count > 9007199254740991 then error("count must be <= 9007199254740991") end
    local interval = values.optional_number(args, "interval") or 30
    if interval <= 0 then error("interval must be greater than zero") end
    if count == 1 then
        c_announce(message)
    else
        c_announce(message, interval)
        TheWorld.__announcementtask.limit = count
    end
    return true
end

function commands.save(args, callback)
    if TheWorld == nil or not TheWorld.ismastershard then
        error("coordinated saves require the master shard")
    end
    if save_pending then error("a native save is still pending") end
    assert(type(callback) == "function", "save requires a completion callback")
    local world = TheWorld
    local session = values.required_string(world.meta, "session_identifier")
    local snapshot = TheNet:GetCurrentSnapshot()
    save_pending = true
    local ok = pcall(function()
        -- Preserve the native autosaver's snapshot coordination with other shards.
        TheWorld:PushEvent("master_autosaverupdate", { snapshot = snapshot })
        ShardGameIndex:SaveCurrent(function()
            local confirmed, path = pcall(function()
                local path = TheNet:GetWorldSessionFile(session)
                local prefix = "session/" .. session .. "/"
                assert(TheWorld == world and world.meta.session_identifier == session
                    and type(path) == "string" and path:sub(1, #prefix) == prefix
                    and path:sub(#prefix + 1):match("^%d+$") ~= nil)
                return path
            end)
            save_pending = false
            if not confirmed then
                callback(nil, "indeterminate")
                return
            end
            callback({ snapshot = path })
        end)
    end)
    -- A failed native call can already have changed disk or other shards.
    -- Keep the guard until its callback or the next world if completion is unknown.
    if not ok then require("dst_server.wire").indeterminate() end
end

function commands.pause(args)
    local paused = values.required_boolean(args, "paused")
    SetServerPaused(paused)
    return TheNet:IsServerPaused(true) == paused
end

function commands.reset()
    c_reset()
    return true
end

function commands.regenerate(args)
    if args.expected_session_id ~= nil then
        local expected = values.required_string(args, "expected_session_id")
        if TheWorld == nil or TheWorld.meta == nil or TheWorld.meta.session_identifier ~= expected then
            error("world regeneration requires the expected session")
        end
    end
    if args.require_empty ~= nil and values.required_boolean(args, "require_empty")
        and #GetPlayerClientTable() > 0 then
        error("world regeneration requires an empty room")
    end
    c_regenerateworld()
    return true
end

function commands.regenerate_shard(args)
    c_regenerateshard(not values.required_boolean(args, "preserve_settings"))
    return true
end

function commands.rollback(args)
    c_rollback(values.required_integer(args, "count", 0))
    return true
end

function commands.rollback_to_snapshot(args)
    local session_id = values.required_string(args, "session_id")
    local snapshot_id = values.required_integer(args, "snapshot_id", 1)
    if TheWorld == nil or not TheWorld.ismastershard or TheWorld.meta == nil
        or TheWorld.meta.session_identifier ~= session_id then
        error("snapshot rollback requires the current master session")
    end

    local target
    local fetch = 100
    while target == nil do
        local snapshots, has_more = TheNet:ListSnapshots(session_id, TheNet:IsOnlineMode(), fetch)
        for _, snapshot in ipairs(snapshots) do
            if snapshot.snapshot_id == snapshot_id then
                target = values.required_string(snapshot, "world_file")
                break
            end
        end
        if target == nil and not has_more then
            error("snapshot is no longer available")
        end
        fetch = fetch * 2
    end

    local current_snapshot = TheNet:GetCurrentSnapshot()
    if current_snapshot <= snapshot_id then
        error("snapshot must precede the current snapshot")
    end

    local ok = pcall(function()
        -- The live API resolves a negative argument relative to the current snapshot.
        TheNet:TruncateSnapshots(session_id, snapshot_id - current_snapshot)
        if TheNet:GetWorldSessionFile(session_id) ~= target then
            error("snapshot truncation did not select the requested world file")
        end
        WorldRollbackFromSim(0)
    end)
    if not ok then require("dst_server.wire").indeterminate() end
    return true
end

function commands.kick(args)
    TheNet:Kick(values.required_string(args, "userid"))
    return true
end

function commands.ban(args)
    local userid = values.required_string(args, "userid")
    if args.seconds == nil then
        TheNet:Ban(userid)
    else
        TheNet:BanForTime(userid, values.required_integer(args, "seconds", 1))
    end
    return true
end

function commands.blocklist()
    local result = {}
    for _, entry in ipairs(TheNet:GetBlacklist() or {}) do
        if type(entry) == "table" then
            local userid = type(entry.userid) == "string" and entry.userid or nil
            local netid = type(entry.netid) == "string" and entry.netid or nil
            local identifier = userid ~= "" and userid or netid
            if identifier ~= nil and identifier ~= "" then
                result[#result + 1] = identifier
            end
        end
    end
    table.sort(result)
    return result
end

function commands.is_blocked(args)
    local userid = values.required_string(args, "userid")
    for _, entry in ipairs(TheNet:GetBlacklist() or {}) do
        if type(entry) == "table"
            and (entry.userid == userid or entry.netid == userid) then
            return true
        end
    end
    return false
end

function commands.unban(args)
    local userid = values.required_string(args, "userid")
    local blacklist = TheNet:GetBlacklist() or {}
    local changed = false
    for index = #blacklist, 1, -1 do
        local entry = blacklist[index]
        if type(entry) == "table"
            and (entry.userid == userid or entry.netid == userid) then
            table.remove(blacklist, index)
            changed = true
        end
    end
    if changed then
        TheNet:SetBlacklist(blacklist)
    end
    return changed
end

function commands.is_whitelisted(args)
    return TheNet:IsWhiteListed(values.required_string(args, "userid")) == true
end

function commands.whitelist(args)
    local userid = values.required_string(args, "userid")
    TheNet:AddToWhiteList(userid)
    return TheNet:IsWhiteListed(userid) == true
end

function commands.unwhitelist(args)
    local userid = values.required_string(args, "userid")
    TheNet:RemoveFromWhiteList(userid)
    return TheNet:IsWhiteListed(userid) ~= true
end

function commands.set_vitals(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player:HasTag("playerghost") then
        return false
    end
    local percentages = {}
    for _, name in ipairs({ "health", "hunger", "sanity", "moisture" }) do
        local value = values.optional_number(args, name)
        if value ~= nil and (value < 0 or value > 1) then
            error(name .. " must be between 0 and 1")
        end
        percentages[name] = value
    end
    local temperature = values.optional_number(args, "temperature")
    local changed = false
    for _, name in ipairs({ "health", "hunger", "sanity", "moisture" }) do
        local value = percentages[name]
        if value ~= nil and player.components[name] ~= nil then
            player.components[name]:SetPercent(value)
            changed = true
        end
    end
    if temperature ~= nil and player.components.temperature ~= nil then
        player.components.temperature:SetTemperature(temperature)
        changed = true
    end
    return changed
end

function commands.kill_player(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player.components.health == nil then
        return false
    end
    player.components.health:ForceKill()
    return true
end

function commands.revive(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil then
        return false
    end
    if player:HasTag("playerghost") then
        player:PushEvent("respawnfromghost")
        return true
    end
    if player:HasTag("corpse") then
        player:PushEvent("respawnfromcorpse")
        return true
    end
    return false
end

function commands.despawn(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or not player:IsValid() then
        return false
    end
    player:DoTaskInTime(0, function(value)
        TheWorld:PushEvent("ms_playerdespawnanddelete", value)
    end)
    return true
end

function commands.migrate(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    local world_id = values.required_string(args, "shard_id")
    local portal_id = values.required_integer(args, "portal_id", 1)
    if player == nil
        or not player:IsValid()
        or player._despawning
        or not Shard_IsWorldAvailable(world_id) then
        return false
    end
    TheWorld:PushEvent("ms_playerdespawnandmigrate", {
        player = player,
        portalid = portal_id,
        worldid = world_id,
    })
    return true
end

function commands.teleport(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil then
        return false
    end
    local x = values.optional_number(args, "x")
    local y = values.optional_number(args, "y")
    local z = values.optional_number(args, "z")
    if x == nil or y == nil or z == nil then
        error("x, y, and z are required")
    end
    if player.Physics ~= nil then
        player.Physics:Teleport(x, y, z)
    else
        player.Transform:SetPosition(x, y, z)
    end
    return true
end

function commands.give(args)
    local userid = values.required_string(args, "userid")
    local prefab = string.lower(values.required_string(args, "item"))
    local count = values.required_integer(args, "count", 1)
    if count > MAX_GIVE_ITEMS then
        error("count must not exceed " .. tostring(MAX_GIVE_ITEMS))
    end
    local player = LookupPlayerInstByUserID(userid)
    if player == nil or player.components.inventory == nil then
        return 0
    end
    if not PrefabExists(prefab) then
        return 0
    end
    local created = 0
    for _ = 1, count do
        local item = SpawnPrefab(prefab)
        if item ~= nil and item.components.inventoryitem ~= nil then
            player.components.inventory:GiveItem(item)
            created = created + 1
        elseif item ~= nil then
            item:Remove()
        end
    end
    return created
end

function commands.remove(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player.components.inventory == nil then
        return 0
    end
    local prefab = string.lower(values.required_string(args, "item"))
    local count = values.required_integer(args, "count", 1)
    local inventory = player.components.inventory
    local _, found = inventory:Has(prefab, count)
    local removed = math.min(found, count)
    inventory:ConsumeByName(prefab, removed)
    return removed
end

function commands.execute_json(args)
    local source = values.required_string(args, "source")
    local callback, message = loadstring(source, "@dst-server-sdk")
    if callback == nil then
        error(message)
    end
    local result = callback()
    return result == nil and json.null or result
end

return commands
