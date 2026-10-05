local M = {}

local function finite(n)
    return type(n) == "number" and n == n and math.abs(n) < math.huge
end

local function valid(point)
    return point and finite(point.x) and finite(point.z)
end

local function distance(a, b)
    return math.sqrt((a.x - b.x) ^ 2 + (a.z - b.z) ^ 2)
end

-- Read-only predicates keep game queries outside the coordinate rules.
function M.plan(position, goal, native_steps, grid, clear, explored)
    if not valid(position) or not valid(goal) then return nil, "invalid_path" end
    local points = { position }
    for _, step in ipairs(native_steps or {}) do
        if not valid(step) then return nil, "invalid_path" end
        if distance(points[#points], step) > .01 then points[#points + 1] = step end
        if #points > 2048 then return nil, "complex_path" end
    end
    if distance(points[#points], goal) > .01 then points[#points + 1] = goal end
    local result, checks, visibility_failure = {}, 0, "unexplored_path"
    local function known(x, z)
        checks = checks + 1
        if checks > 16384 then visibility_failure = "complex_path"; return false end
        return explored({ x = x, z = z })
    end
    for index = 2, #points do
        local a, b = points[index - 1], points[index]
        if not clear(a, b) then return nil, "blocked_path" end
        local tile = grid.size
        local x = grid.x + math.floor((a.x - grid.x) / tile + .5) * tile
        local z = grid.z + math.floor((a.z - grid.z) / tile + .5) * tile
        local dx, dz = b.x - a.x, b.z - a.z
        local sx, sz = dx >= 0 and 1 or -1, dz >= 0 and 1 or -1
        local tx = dx ~= 0 and (x + sx * tile / 2 - a.x) / dx or math.huge
        local tz = dz ~= 0 and (z + sz * tile / 2 - a.z) / dz or math.huge
        local incx = dx ~= 0 and tile / math.abs(dx) or math.huge
        local incz = dz ~= 0 and tile / math.abs(dz) or math.huge
        if not known(x, z) then return nil, visibility_failure end
        while math.min(tx, tz) <= 1 do
            if math.abs(tx - tz) < 1e-9 then
                -- A diagonal corner touches both adjacent tiles; neither may hide in fog.
                if not known(x + sx * tile, z) or not known(x, z + sz * tile) then
                    return nil, visibility_failure
                end
                x, z, tx, tz = x + sx * tile, z + sz * tile, tx + incx, tz + incz
            elseif tx < tz then x, tx = x + sx * tile, tx + incx
            else z, tz = z + sz * tile, tz + incz end
            if not known(x, z) then return nil, visibility_failure end
        end
        local pieces = math.max(1, math.ceil(distance(a, b) / 24))
        for piece = 1, pieces do
            local t = piece / pieces
            result[#result + 1] = { x = a.x + dx * t, z = a.z + dz * t }
        end
    end
    return result
end

return M
