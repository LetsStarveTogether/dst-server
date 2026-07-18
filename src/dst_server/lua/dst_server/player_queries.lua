local player_values = require("dst_server.player_values")
local values = require("dst_server.values")
local queries = {}

function queries.get_players()
    local result = {}
    for _, client in ipairs(GetPlayerClientTable()) do
        local player = LookupPlayerInstByUserID(client.userid)
        result[#result + 1] = player_values.player(client, player)
    end
    return result
end

function queries.get_player(args)
    local userid = values.required_string(args, "userid")
    local player = LookupPlayerInstByUserID(userid)
    local client = nil
    for _, item in ipairs(GetPlayerClientTable()) do
        if item.userid == userid then
            client = item
            break
        end
    end
    if client == nil and player == nil then
        return json.null
    end
    client = client or {
        userid = player.userid,
        name = player:GetDisplayName() or "",
        prefab = player.prefab,
        admin = false,
        moderator = false,
    }
    return player_values.player(client, player)
end

function queries.get_player_inventory(args)
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
