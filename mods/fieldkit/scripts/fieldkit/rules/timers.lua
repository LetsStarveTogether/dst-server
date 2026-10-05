local M = {}
local effects = {
    voltgoatjelly = { "electricattack", "BUFF_ELECTRICATTACK_DURATION" },
    frogfishbowl = { "moistureimmunity", "BUFF_MOISTUREIMMUNITY_DURATION" },
    glowberrymousse = { "light", "WORMLIGHT_DURATION", 4 },
    wormlight = { "light", "WORMLIGHT_DURATION" }, wormlight_lesser = { "light", "WORMLIGHT_DURATION", .25 },
    shroomcake = { "sleepresistance", "SLEEPRESISTBUFF_TIME" },
    jellybean = { "healthregen", "JELLYBEAN_DURATION" }, sweettea = { "sweettea", "SWEETTEA_DURATION" },
    ancientfruit_nightvision = { "nightvision", "ANCIENTTREE_NIGHTVISION_FRUIT_BUFF_DURATION" },
}
local spices = { chili = { "attack", "BUFF_ATTACK_DURATION" }, garlic = { "absorption", "BUFF_PLAYERABSORPTION_DURATION" },
    sugar = { "work", "BUFF_WORKEFFECTIVENESS_DURATION" } }
local temperatures = {
    watermelon = { "COLD_FOOD_BONUS_TEMP", "FOOD_TEMP_BRIEF" }, ice = { "COLD_FOOD_BONUS_TEMP", "FOOD_TEMP_BRIEF", 1.5 },
    carnivalfood_corntea = { "COLD_FOOD_BONUS_TEMP", "FOOD_TEMP_LONG" },
    winter_food7 = { "HOT_FOOD_BONUS_TEMP", "FOOD_TEMP_LONG" }, winter_food8 = { "HOT_FOOD_BONUS_TEMP", "FOOD_TEMP_LONG" },
    winter_food9 = { "COLD_FOOD_BONUS_TEMP", "FOOD_TEMP_LONG" },
}
M.buff_names = {
    electricattack = { "攻击带电", "Electric attacks" }, moistureimmunity = { "免疫潮湿", "Moisture immunity" },
    light = { "发光", "Light" }, sleepresistance = { "抗困倦", "Sleep resistance" },
    healthregen = { "生命恢复", "Health regeneration" }, sweettea = { "理智恢复", "Sanity regeneration" },
    nightvision = { "夜视", "Night vision" }, attack = { "伤害提升", "Extra damage" },
    absorption = { "伤害减免", "Damage absorption" }, work = { "工作效率", "Work effectiveness" },
    warm = { "食物升温", "Food warming" }, cool = { "食物降温", "Food cooling" },
}
M.bosses = {
    beequeen = { "BEEQUEEN_RESPAWN_TIME" }, dragonfly = { "DRAGONFLY_RESPAWN_TIME" },
    toadstool = { "TOADSTOOL_RESPAWN_TIME" }, toadstool_dark = { "TOADSTOOL_RESPAWN_TIME", key = "toadstool" },
    crabking = { "CRABKING_RESPAWN_TIME" }, malbatross = { "MALBATROSS_SPAWNDELAY_BASE", "MALBATROSS_SPAWNDELAY_RANDOM" },
    walrus = { "WALRUS_REGEN_PERIOD", season = "winter" },
}
function M.finite(value) return type(value) == "number" and value == value and math.abs(value) < 1e12 end

function M.food(tuning, prefab, data)
    local result = {}
    local base, spice = prefab:match("^(.-)_spice_(%w+)$")
    base = base or prefab
    local function add(rule)
        if not rule then return end
        local duration = tuning[rule[2]]
        if M.finite(duration) and duration > 0 then
            result[#result + 1] = { id = rule[1], duration = duration * (rule[3] or 1) }
        end
    end
    add(effects[base]); add(spices[spice])
    local temperature = temperatures[base]
    if not data and temperature and M.finite(tuning[temperature[2]]) then
        data = { temperature = tuning[temperature[1]], temperatureduration = tuning[temperature[2]] * (temperature[3] or 1) }
    end
    if data and M.finite(data.temperature) and data.temperature ~= 0
        and M.finite(data.temperatureduration) and data.temperatureduration > 0 then
        result[#result + 1] = { id = data.temperature > 0 and "warm" or "cool", key = "temperature", duration = data.temperatureduration }
    end
    return result
end

function M.respawn(tuning, prefab)
    local rule = M.bosses[prefab]
    local base = rule and tuning[rule[1]]
    local spread = rule and rule[2] and tuning[rule[2]] or 0
    if M.finite(base) and base > 0 and M.finite(spread) and spread >= 0 then
        return math.max(0, base - spread), base + spread, rule.key or prefab, rule.season
    end
end

local function clamp(value, low, high) return math.max(low, math.min(high, value)) end
-- Integrate the public native precipitation curve in a fixed number of moisture slices.
local function rain_time(moisture, ceiling, floor, peak)
    local delta, total = math.max(0, moisture - floor) / 64, 0
    if ceiling <= floor then return end
    for i = 1, 64 do
        local sample = floor + (i - .5) * delta
        local p = clamp((sample - floor) / (ceiling - floor), 0, 1)
        local rate = math.min(peak, .1 + .9 * math.sin(p * math.pi))
        total = total + delta / (10 * rate)
    end
    return total
end

function M.weather()
    local self, last, slope, floor, candidate, peak = {}, nil, nil, nil, nil, .1
    function self:reset() last, slope, floor, candidate, peak = nil, nil, nil, nil, .1 end
    function self:update(state, now, tuning)
        local phase = state.isacidraining and "acidrain" or state.precipitation or "none"
        local result = { phase = phase, transition = phase == "none" and "start" or "stop" }
        local m, c, rate = state.moisture, state.moistureceil, state.precipitationrate
        if not M.finite(now) or not M.finite(m) or not M.finite(c) or not M.finite(rate) then return result end
        if not last or phase ~= last.phase or c ~= last.ceiling or state.season ~= last.season or now <= last.at then self:reset() end
        if phase == "lunarhail" and M.finite(state.lunarhaillevel) and M.finite(tuning.LUNARHAIL_EVENT_TIME) then
            result.low = math.max(0, state.lunarhaillevel / 100 * tuning.LUNARHAIL_EVENT_TIME)
            result.high = result.low
        elseif last and now - last.at <= 3 then
            local dt, change = now - last.at, m - last.moisture
            if phase == "none" then
                local measured = change / dt
                if measured > 0 and c > m and slope and math.abs(measured / slope - 1) < .05 then
                    result.low = (c - m) / math.max(measured, slope)
                    result.high = (c - m) / math.min(measured, slope)
                end
                slope = measured > 0 and measured or nil
            elseif math.abs(-change / dt - (rate + last.rate) * 5) < math.max(.15, rate) then
                peak = math.max(peak, rate)
                local dr = rate - last.rate
                if math.abs(dr) > .00001 and rate > .1 and rate < 1 then
                    local p = math.asin(clamp((rate - .1) / .9, 0, 1)) / math.pi
                    if dr > 0 then p = 1 - p end
                    local estimate = p < .99999 and (m - p * c) / (1 - p) or nil
                    if estimate and estimate >= 0 and estimate < m then
                        if candidate and math.abs(estimate - candidate) < math.max(2, c * .01) then floor = estimate
                        else floor = nil end
                        candidate = estimate
                    end
                end
                if floor then
                    local error = math.max(1, c * .01)
                    result.low = rain_time(m, c, math.min(m, floor + error), 1)
                    result.high = rain_time(m, c, math.max(0, floor - error), peak)
                end
            else floor, candidate = nil, nil end
        end
        last = { at = now, phase = phase, ceiling = c, moisture = m, rate = rate, season = state.season }
        return result
    end
    return self
end

return M
