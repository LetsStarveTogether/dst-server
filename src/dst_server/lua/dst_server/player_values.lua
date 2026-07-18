local values = require("dst_server.values")
local player_values = {}

local function state(client, player)
    local combat = player.components.combat
    local rider = player.components.rider
    local followers = {}
    local leader = player.components.leader
    if leader ~= nil then
        for follower in pairs(leader.followers) do
            local entity = values.entity(follower)
            if entity ~= json.null then
                followers[#followers + 1] = entity
            end
        end
        table.sort(followers, function(a, b)
            return a.prefab == b.prefab and a.guid < b.guid or a.prefab < b.prefab
        end)
    end

    local skill_xp = json.null
    local available_skill_points = json.null
    local activated_skills = json.null
    local skilltree = player.components.skilltreeupdater
    if skilltree ~= nil then
        skill_xp = skilltree:GetSkillXP()
        available_skill_points = skilltree:GetAvailableSkillPoints()
        activated_skills = {}
        for skill in pairs(skilltree:GetActivatedSkills() or {}) do
            activated_skills[#activated_skills + 1] = skill
        end
        table.sort(activated_skills)
    end

    return {
        network_score = client.netscore == nil and json.null or client.netscore,
        combat_target = values.entity(combat ~= nil and combat.target or nil),
        weapon = values.entity(combat ~= nil and combat:GetWeapon() or nil),
        mount = values.entity(rider ~= nil and rider:GetMount() or nil),
        follower_count = #followers,
        followers = followers,
        skill_xp = skill_xp,
        available_skill_points = available_skill_points,
        activated_skills = activated_skills,
    }
end

function player_values.player(client, player)
    local value = {
        userid = client.userid,
        name = client.name or "",
        prefab = player ~= nil
            and player.prefab
            or client.prefab
            or client.lobbycharacter
            or "",
        admin = client.admin == true,
        moderator = client.moderator == true,
        is_ghost = json.null,
        position = json.null,
        age = json.null,
        vitals = json.null,
        state = json.null,
    }
    if player == nil then
        return value
    end

    value.is_ghost = player:HasTag("playerghost")
    value.position = values.entity_position(player)
    if player.components.age ~= nil then
        local age = player.components.age
        value.age = {
            seconds = age:GetAge(),
            days = age:GetAgeInDays(),
            display_days = age:GetDisplayAgeInDays(),
        }
    end

    local vitals = {
        health = json.null,
        hunger = json.null,
        sanity = json.null,
        temperature = json.null,
        moisture = json.null,
    }
    local health = player.components.health
    if health ~= nil then
        vitals.health = {
            current = health.currenthealth,
            maximum = health.maxhealth,
            percent = health:GetPercent(),
            is_dead = health:IsDead(),
            is_invincible = health:IsInvincible(),
        }
    end
    local hunger = player.components.hunger
    if hunger ~= nil then
        vitals.hunger = {
            current = hunger.current,
            maximum = hunger.max,
            percent = hunger:GetPercent(),
        }
    end
    local sanity = player.components.sanity
    if sanity ~= nil then
        vitals.sanity = {
            current = sanity.current,
            maximum = sanity.max,
            percent = sanity:GetPercent(),
        }
    end
    local temperature = player.components.temperature
    if temperature ~= nil then
        vitals.temperature = {
            current = temperature:GetCurrent(),
            maximum = temperature:GetMax(),
        }
    end
    local moisture = player.components.moisture
    if moisture ~= nil then
        vitals.moisture = {
            current = moisture:GetMoisture(),
            maximum = moisture:GetMaxMoisture(),
            percent = moisture:GetMoisturePercent(),
        }
    end
    value.vitals = vitals
    value.state = state(client, player)
    return value
end

function player_values.item(item)
    local value = {
        prefab = item.prefab or "",
        guid = item.GUID,
        skin = item:GetSkinName() or json.null,
        stack_size = item.components.stackable ~= nil
            and item.components.stackable:StackSize()
            or 1,
        moisture_percent = json.null,
        uses_percent = json.null,
        freshness_percent = json.null,
        fuel_percent = json.null,
        armor_percent = json.null,
        charge_percent = json.null,
    }
    local components = item.components
    if components.inventoryitem ~= nil then
        value.moisture_percent = components.inventoryitem:GetMoisturePercent()
    end
    if components.finiteuses ~= nil then
        value.uses_percent = components.finiteuses:GetPercent()
    end
    if components.perishable ~= nil then
        value.freshness_percent = components.perishable:GetPercent()
    end
    if components.fueled ~= nil then
        value.fuel_percent = components.fueled:GetPercent()
    end
    if components.armor ~= nil then
        value.armor_percent = components.armor:GetPercent()
    end
    if components.rechargeable ~= nil then
        value.charge_percent = components.rechargeable:GetPercent()
    end
    return value
end

return player_values
