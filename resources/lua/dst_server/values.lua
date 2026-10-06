local state = require("dst_server.state")
local values = {}

local function finite(value)
    return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

function values.required_string(args, name)
    local value = args[name]
    if type(value) ~= "string" or value == "" then
        error(name .. " must be a non-empty string")
    end
    return value
end

function values.required_integer(args, name, minimum)
    local value = args[name]
    if not finite(value) or value ~= math.floor(value) or value < minimum then
        error(name .. " must be an integer >= " .. tostring(minimum))
    end
    return value
end

function values.optional_number(args, name)
    local value = args[name]
    if value ~= nil and not finite(value) then
        error(name .. " must be a finite number")
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

local function position(x, y, z)
    if not finite(x) or not finite(y) or not finite(z) then return json.null end
    return { x = x, y = y, z = z }
end

function values.position(value)
    if type(value) ~= "table" then return json.null end
    return position(value.x, value.y, value.z)
end

function values.entity_position(inst)
    if inst == nil or inst.Transform == nil or not inst:IsValid()
        or inst.Transform.GetWorldPosition == nil then
        return json.null
    end
    return position(inst.Transform:GetWorldPosition())
end

local function valid_entity(inst)
    return inst ~= nil
        and type(inst.prefab) == "string"
        and inst.prefab ~= ""
        and type(inst.GUID) == "number"
        and inst.GUID > 0
end

function values.userid(id)
    return type(id) == "string" and id ~= "" and id or nil
end

function values.entity_userid(inst)
    return inst and values.userid(inst.userid)
end

function values.is_player(inst)
    return inst ~= nil and (inst.isplayer == true or state.player_userids[inst] ~= nil)
end

function values.player_userid(inst)
    if values.is_player(inst) then
        return values.entity_userid(inst) or values.userid(state.player_userids[inst])
    end
end

function values.entity_ref(inst)
    if not valid_entity(inst) then
        return json.null
    end
    return {
        prefab = inst.prefab,
        guid = inst.GUID,
        userid = values.player_userid(inst) or json.null,
    }
end

local function percent(inst, component)
    local value = inst.components ~= nil and inst.components[component] or nil
    if value == nil then return nil end
    local ok, result = pcall(value.GetPercent, value)
    return ok and finite(result) and result or nil
end

function values.item_condition(inst)
    local condition = {
        uses_percent = percent(inst, "finiteuses"),
        fuel_percent = percent(inst, "fueled"),
        freshness_percent = percent(inst, "perishable"),
        armor_percent = percent(inst, "armor"),
    }
    return next(condition) ~= nil and condition or nil
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

local function catapult_userid(inst)
    if inst.prefab == "winona_catapult_projectile" or inst.prefab == "trap_vines" then
        local caster = values.player_userid(inst.caster)
        if caster then return caster end
        -- Automatic shots retain their launching catapult when the engineer is offline.
        local projectile = inst.components.complexprojectile
        inst = projectile and projectile.attacker or inst.attacker
    end
    if inst and inst.prefab == "winona_catapult" then return values.userid(inst._engineerid) end
end

function values.owner_userid(inst)
    local components = inst and inst.components
    if components ~= nil then
        if inst.prefab == "bernie_big" then
            -- The prefab keeps its last leader after the brain stops following them.
            local leader = inst.brain and inst.brain._leader
            return leader and leader:IsValid() and values.player_userid(leader) or nil
        end
        local item, follower, explosive = components.inventoryitem, components.follower, components.explosive
        local caster = inst.WINDSTAFF_CASTER
        return catapult_userid(inst)
            or (item and values.player_userid(item:GetGrandOwner()))
            or (follower and values.player_userid(follower:GetLeader()))
            or (explosive and values.player_userid(explosive.attacker or explosive.pvpattacker))
            or (caster and caster:IsValid() and values.player_userid(caster) or nil)
    end
end

function values.player_age(player)
    local age = values.is_player(player) and player.components ~= nil
        and player.components.age or nil
    if age == nil then return json.null end
    local ok, seconds = pcall(age.GetAge, age)
    return ok and finite(seconds) and seconds >= 0 and seconds or json.null
end

return values
