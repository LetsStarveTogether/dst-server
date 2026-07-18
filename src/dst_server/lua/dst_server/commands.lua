local values = require("dst_server.values")
local commands = {}

function commands.announce(args)
    c_announce(values.required_string(args, "message"))
    return true
end

function commands.save()
    c_save()
    return true
end

function commands.set_server_paused(args)
    local paused = values.required_boolean(args, "paused")
    SetServerPaused(paused)
    return TheNet:IsServerPaused(true) == paused
end

function commands.reset()
    c_reset()
    return true
end

function commands.regenerate_world()
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

function commands.kick_player(args)
    TheNet:Kick(values.required_string(args, "userid"))
    return true
end

function commands.ban_player(args)
    local userid = values.required_string(args, "userid")
    if args.seconds == nil then
        TheNet:Ban(userid)
    else
        TheNet:BanForTime(userid, values.required_integer(args, "seconds", 1))
    end
    return true
end

function commands.set_player_vitals(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player:HasTag("playerghost") then
        return false
    end
    local changed = false
    for _, name in ipairs({ "health", "hunger", "sanity", "moisture" }) do
        local value = values.optional_number(args, name)
        if value ~= nil and player.components[name] ~= nil then
            player.components[name]:SetPercent(value)
            changed = true
        end
    end
    local temperature = values.optional_number(args, "temperature")
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

function commands.revive_player(args)
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

function commands.despawn_player(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or not player:IsValid() then
        return false
    end
    player:DoTaskInTime(0, function(value)
        TheWorld:PushEvent("ms_playerdespawnanddelete", value)
    end)
    return true
end

function commands.migrate_player(args)
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

function commands.teleport_player(args)
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

function commands.give_item(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player.components.inventory == nil then
        return 0
    end
    local prefab = string.lower(values.required_string(args, "prefab"))
    local count = values.required_integer(args, "count", 1)
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

function commands.remove_item(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player.components.inventory == nil then
        return 0
    end
    local prefab = string.lower(values.required_string(args, "prefab"))
    local count = values.required_integer(args, "count", 1)
    local inventory = player.components.inventory
    local _, found = inventory:Has(prefab, count)
    local removed = math.min(found, count)
    inventory:ConsumeByName(prefab, removed)
    return removed
end

function commands.execute_script(args)
    local source = values.required_string(args, "source")
    local callback, message = loadstring(source, "@dst-server-sdk")
    if callback == nil then
        error(message)
    end
    local result = callback()
    return result == nil and json.null or result
end

return commands
