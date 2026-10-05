local M = {}

local definitions = {
    health = { label = { "生命", "Health" }, widget = "heart" },
    hunger = { label = { "饥饿", "Hunger" }, widget = "stomach" },
    sanity = { label = { "理智", "Sanity" }, widget = "brain" },
    moisture = { label = { "潮湿", "Wetness" }, widget = "moisturemeter" },
    temperature = { label = { "体温", "Body temperature" }, unit = "temperature" },
    age = { label = { "年龄", "Age" }, widget = "heart" },
    boat_health = { label = { "船体耐久", "Boat health" }, widget = "boatmeter" },
    pet_health = { label = { "伙伴生命", "Companion health" }, widget = "pethealthbadge" },
    pet_hunger = { label = { "伙伴饥饿", "Companion hunger" }, widget = "pethungerbadge" },
    mightiness = { label = { "力量", "Mightiness" }, widget = "mightybadge" },
    inspiration = { label = { "灵感", "Inspiration" }, widget = "inspirationbadge" },
    wereness = { label = { "变身值", "Wereness" }, widget = "wereness" },
    energy = { label = { "电量", "Energy" }, widget = "upgrademodulesdisplay", group = "secondary_status" },
}
M.order = { "health", "hunger", "sanity", "moisture", "temperature", "age", "boat_health", "pet_health",
    "pet_hunger", "mightiness", "inspiration", "wereness", "energy" }

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

local function record(key, value, maximum)
    local definition = definitions[key]
    return { label = definition.label, widget = definition.widget, group = definition.group,
        unit = definition.unit, value = finite(value) and value or nil,
        max = finite(maximum) and maximum > 0 and maximum or nil,
        quality = finite(value) and "known" or "unknown" }
end

local function scaled(percent, maximum)
    if finite(percent) and finite(maximum) and maximum > 0 then return percent * maximum end
end

function M.definition(key) return definitions[key] end

function M.widget(ctx, widget)
    local controls = ctx.hud and ctx.hud.controls
    if not controls or not widget then return end
    for _, key in ipairs(M.order) do
        local definition = definitions[key]
        local parent = controls[definition.group or "status"]
        if definition.widget and parent and parent[definition.widget] == widget then
            return key == "health" and ctx.player.prefab == "wanda" and "age" or key
        end
    end
end

function M.get(ctx, key)
    local player, tuning = ctx.player, ctx.G.TUNING
    local components, replicas = player.components, player.replica
    if key == "health" or key == "hunger" or key == "sanity" then
        local replica = replicas[key]
        local known = replica and (replica.classified or components[key])
        return record(key, known and replica:GetCurrent(), known and replica:Max())
    elseif key == "temperature" then
        local known = player.player_classified or components.temperature
        return record(key, known and player:GetTemperature())
    elseif key == "moisture" then
        local known = player.player_classified or components.moisture
        return record(key, known and player:GetMoisture(), known and player:GetMaxMoisture())
    elseif key == "age" then
        if player.prefab ~= "wanda" then return end
        local health = replicas.health
        local known = health and (health.classified or components.health)
        local percent = known and health:GetPercent()
        local low, high = tuning.WANDA_MIN_YEARS_OLD, tuning.WANDA_MAX_YEARS_OLD
        return record(key, finite(percent) and finite(low) and finite(high) and low + (1 - percent) * (high - low), high)
    elseif key == "boat_health" then
        local boat = player.GetCurrentPlatform and player:GetCurrentPlatform()
        local health = boat and boat.components.healthsyncer
        if not health then return end
        return record(key, scaled(health:GetPercent(), health.max_health), health.max_health)
    elseif key == "pet_health" then
        local health = components.pethealthbar
        if not health then return end
        local maximum = health:GetMaxHealth()
        local result = record(key, scaled(health:GetPercent(), maximum), maximum)
        local bonus = health:GetMaxBonus()
        if finite(bonus) and bonus > 0 then
            result.bonus = { value = scaled(health:GetPercentBonus(), bonus), max = bonus }
        end
        return result
    elseif key == "pet_hunger" then
        local hunger = player.pet_hunger_classified
        if not hunger then return end
        return record(key, scaled(hunger:GetPercent(), hunger:Max()), hunger:Max())
    elseif key == "mightiness" then
        if not player.GetMightiness then return end
        local known = components.mightiness or player.player_classified and player.player_classified.currentmightiness
        local maximum = tuning.MIGHTINESS_MAX
        for level = 5, 1, -1 do
            if player:HasTag("wolfgang_overbuff_" .. level) then
                maximum = maximum + tuning.SKILLS["WOLFGANG_OVERBUFF_" .. level]
                break
            end
        end
        -- The getter uses the base maximum even while gym buffs allow values above 100%.
        return record(key, known and scaled(player:GetMightiness(), tuning.MIGHTINESS_MAX), maximum)
    elseif key == "inspiration" then
        if not player.GetInspiration then return end
        local known = components.singinginspiration or player.player_classified and player.player_classified.currentinspiration
        return record(key, known and scaled(player:GetInspiration(), tuning.INSPIRATION_MAX), tuning.INSPIRATION_MAX)
    elseif key == "wereness" then
        if not player.GetWereness or not player:HasTag("wereplayer") then return end
        local known = components.wereness or player.player_classified and player.player_classified.currentwereness
        return record(key, known and scaled(player:GetWereness(), 100), 100)
    elseif key == "energy" then
        local owner, classified = components.upgrademoduleowner, player.wx78_classified
        if not owner and not classified then return end
        -- These native getters include shadow-powered full charge; the player's wrapper does not.
        return record(key, owner and owner:GetChargeLevel() or classified:GetEnergyLevel(),
            owner and owner:GetMaxChargeLevel() or classified:GetMaxEnergy())
    end
end

function M.snapshot(ctx)
    local result = {}
    for _, key in ipairs(M.order) do result[key] = M.get(ctx, key) end
    return result
end

return M
