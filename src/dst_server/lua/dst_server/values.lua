local values = {}

function values.required_string(args, name)
    local value = args[name]
    if type(value) ~= "string" or value == "" then
        error(name .. " must be a non-empty string")
    end
    return value
end

function values.required_integer(args, name, minimum)
    local value = args[name]
    if type(value) ~= "number" or value ~= math.floor(value) or value < minimum then
        error(name .. " must be an integer >= " .. tostring(minimum))
    end
    return value
end

function values.optional_number(args, name)
    local value = args[name]
    if value ~= nil and type(value) ~= "number" then
        error(name .. " must be a number")
    end
    return value
end

function values.required_boolean(args, name)
    local value = args[name]
    if type(value) ~= "boolean" then
        error(name .. " must be a boolean")
    end
    return value
end

function values.text(value, maximum)
    if value == nil then
        return json.null
    end
    value = tostring(value)
    if #value <= maximum then
        return value
    end

    while maximum > 0 do
        local byte = string.byte(value, maximum + 1)
        if byte < 128 or byte >= 192 then
            break
        end
        maximum = maximum - 1
    end
    return string.sub(value, 1, maximum)
end

function values.position(value)
    if value == nil
        or type(value.x) ~= "number"
        or type(value.y) ~= "number"
        or type(value.z) ~= "number" then
        return json.null
    end
    return { x = value.x, y = value.y, z = value.z }
end

function values.entity_position(inst)
    if inst == nil or inst.Transform == nil then
        return json.null
    end
    local x, y, z = inst.Transform:GetWorldPosition()
    return { x = x, y = y, z = z }
end

local function valid_entity(inst)
    return inst ~= nil
        and type(inst.prefab) == "string"
        and inst.prefab ~= ""
        and type(inst.GUID) == "number"
        and inst.GUID > 0
end

function values.entity_ref(inst)
    if not valid_entity(inst) then
        return json.null
    end
    return {
        prefab = inst.prefab,
        guid = inst.GUID,
        userid = inst.userid or json.null,
        position = values.entity_position(inst),
    }
end

function values.item_ref(inst)
    if not valid_entity(inst) then
        return json.null
    end
    local stack = inst.components ~= nil and inst.components.stackable or nil
    local skin = inst.GetSkinName ~= nil and inst:GetSkinName() or nil
    return {
        prefab = inst.prefab,
        guid = inst.GUID,
        skin = skin or json.null,
        stack_size = stack ~= nil and stack:StackSize() or 1,
    }
end

function values.entity(inst)
    if not valid_entity(inst) then
        return json.null
    end
    return { prefab = inst.prefab, guid = inst.GUID }
end

function values.tags(tags)
    local result = {}
    for tag in string.gmatch(tags or "", "[^,]+") do
        result[#result + 1] = tag
    end
    table.sort(result)
    return result
end

function values.special_damage(source)
    local result = {}
    if type(source) == "table" then
        for kind, value in pairs(source) do
            if type(kind) == "string" and kind ~= "" and type(value) == "number" then
                result[#result + 1] = { kind = kind, value = value }
            end
        end
        table.sort(result, function(a, b) return a.kind < b.kind end)
    end
    return result
end

function values.optional_json_number(value)
    return type(value) == "number" and value or json.null
end

function values.player_for(inst)
    if inst == nil then
        return nil
    end
    if inst.userid ~= nil and inst:HasTag("player") then
        return inst
    end
    local inventoryitem = inst.components ~= nil and inst.components.inventoryitem or nil
    local owner = inventoryitem ~= nil and inventoryitem:GetGrandOwner() or nil
    if owner ~= nil and owner.userid ~= nil and owner:HasTag("player") then
        return owner
    end
    local follower = inst.components ~= nil and inst.components.follower or nil
    local leader = follower ~= nil and follower.leader or nil
    if leader ~= nil and leader.userid ~= nil and leader:HasTag("player") then
        return leader
    end
    return nil
end

function values.current_action_sequence(player)
    local action = player ~= nil and player.bufferedaction or nil
    return action ~= nil and action._dst_action_seq or json.null
end

function values.player_data(player)
    return {
        player = values.entity_ref(player),
        caused_by_action_sequence = values.current_action_sequence(player),
    }
end

function values.combat_data(player, data)
    return {
        player = values.entity_ref(player),
        damage = values.optional_json_number(data.damage),
        weapon = values.entity_ref(data.weapon),
        stimuli = values.text(data.stimuli, 128),
        special_damage = values.special_damage(data.spdamage),
        caused_by_action_sequence = values.current_action_sequence(player),
    }
end

function values.loot_refs(loot)
    if loot == nil then
        return {}
    end
    if loot.GUID ~= nil then
        return { values.item_ref(loot) }
    end
    local result = {}
    for index, item in ipairs(loot) do
        if index > 64 then
            error("loot contains more than 64 items")
        end
        result[#result + 1] = values.item_ref(item)
    end
    return result
end

return values
