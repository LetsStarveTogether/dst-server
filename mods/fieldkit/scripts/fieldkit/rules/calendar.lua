local M = {}
local order = { "autumn", "winter", "spring", "summer" }
local phases = { "new", "quarter", "quarter", "quarter", "half", "half", "half",
    "threequarter", "threequarter", "threequarter", "full", "threequarter", "threequarter",
    "threequarter", "half", "half", "half", "quarter", "quarter", "quarter" }

function M.seasons(state)
    local lengths, total, current = {}, 0
    for i, name in ipairs(order) do
        local value = state[name .. "length"]
        if type(value) ~= "number" or value ~= value or value < 0 or value == math.huge then return {} end
        lengths[name], total = value, total + value
        if state.season == name then current = i end
    end
    if not current or total == 0 then return {} end
    local next_season
    if state.remainingdaysinseason ~= 10000 then
        for offset = 1, #order do
            local name = order[(current + offset - 1) % #order + 1]
            if lengths[name] > 0 then next_season = name; break end
        end
    end
    return { lengths = lengths, total = total, next_season = next_season }
end

function M.new()
    local candidates, previous, blocked
    local self = {}
    function self:fullmoon(state, cave, style)
        local day, phase, waxing = state.cycles, state.moonphase, state.iswaxingmoon
        if cave or state.isalterawake or style and style ~= "default" and style ~= "glassed_default"
            or type(day) ~= "number" or day ~= day or day < 0 or day == math.huge
            or type(waxing) ~= "boolean" then
            candidates, previous, blocked = nil, nil, nil
            return { quality = "unknown" }
        end
        local function matches(index)
            return phases[index] == phase and (index <= 10) == waxing
        end
        if not previous or previous.day ~= day or previous.phase ~= phase or previous.waxing ~= waxing then
            local next_candidates = {}
            if previous and day > previous.day and candidates then
                for index in pairs(candidates) do
                    local shifted = (index + day - previous.day - 1) % #phases + 1
                    if matches(shifted) then next_candidates[shifted] = true end
                end
            end
            if next(next_candidates) == nil then
                if previous then blocked = true end
                for index in ipairs(phases) do if matches(index) then next_candidates[index] = true end end
            elseif previous.phase ~= phase then
                blocked = false
            end
            candidates, previous = next_candidates, { day = day, phase = phase, waxing = waxing }
        end
        if blocked then return { quality = "unknown" } end
        local low, high
        for index in pairs(candidates) do
            local target = day + 1 + (11 - index) % #phases
            low, high = math.min(low or target, target), math.max(high or target, target)
        end
        -- The server's phase lock is private. Even a single surviving date is an estimate.
        return { quality = low and "estimated" or "unknown", min_day = low, max_day = high }
    end
    return self
end

return M
