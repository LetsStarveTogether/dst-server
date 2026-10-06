local connections = require("dst_server.connections")
local player_values = require("dst_server.player_values")
local values = require("dst_server.values")
local queries = {}

function queries.list_players()
    local clients = GetPlayerClientTable()
    if #clients == 0 then return {} end
    local players = {}
    for _, player in ipairs(AllPlayers) do
        local userid = values.userid(player.userid)
        if userid and players[userid] == nil and player:IsValid() then players[userid] = player end
    end
    local result = {}
    for _, client in ipairs(clients) do
        result[#result + 1] = player_values.summary(client, players[client.userid])
    end
    return result
end

local function lookup(args, include_details)
    local userid = values.required_string(args, "userid")
    local player = LookupPlayerInstByUserID(userid)
    if player ~= nil and not player:IsValid() then player = nil end
    if player and not include_details then return player end
    for _, client in ipairs(GetPlayerClientTable()) do
        if client.userid == userid then return player, client end
    end
    return player, player and {userid = userid}
end

function queries.get_player(args)
    local player, client = lookup(args, true)
    return client and player_values.player(client, player) or json.null
end

-- Take the route and any requested character details from the same observation.
function queries.locate_player(args)
    local include_details = args.include_details == true
    local player, client = lookup(args, include_details)
    local location = {
        present = player ~= nil or client ~= nil,
        guid = player and player.GUID or json.null,
        departing = player ~= nil and player._despawning == true,
        session_id = TheWorld.meta.session_identifier,
    }
    if include_details and client then
        location.player, location.loaded = player_values.player(client, player)
    else
        location.loaded = connections.loaded(player)
    end
    return location
end

function queries.inventory(args)
    local player = LookupPlayerInstByUserID(values.required_string(args, "userid"))
    if player == nil or player.components.inventory == nil then
        return json.null
    end
    local inventory = player.components.inventory
    local items = {}
    for slot, item in pairs(inventory.itemslots) do
        items[#items + 1] = { slot = slot, item = player_values.item(item) }
    end
    table.sort(items, function(a, b) return a.slot < b.slot end)
    local equipment = {}
    for slot, item in pairs(inventory.equipslots) do
        equipment[#equipment + 1] = {
            slot = slot,
            item = player_values.item(item),
        }
    end
    table.sort(equipment, function(a, b) return a.slot < b.slot end)
    local active = inventory.activeitem ~= nil
        and player_values.item(inventory.activeitem)
        or json.null
    local overflow = json.null
    local container = inventory:GetOverflowContainer()
    if container ~= nil then
        local slots = {}
        for slot, item in pairs(container.slots) do
            slots[#slots + 1] = { slot = slot, item = player_values.item(item) }
        end
        table.sort(slots, function(a, b) return a.slot < b.slot end)
        overflow = {
            prefab = container.inst.prefab or "",
            guid = container.inst.GUID,
            slots = slots,
        }
    end
    return {
        userid = player.userid,
        items = items,
        equipment = equipment,
        active_item = active,
        overflow = overflow,
    }
end

return queries
