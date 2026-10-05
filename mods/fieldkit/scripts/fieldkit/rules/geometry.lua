-- A lattice lives in one coordinate frame; the game adapter owns platform transforms.
local M = {}
local sqrt3 = math.sqrt(3)
M.shapes = { "square", "diamond", "hex", "hex45", "hex90", "hex135" }
local angles = { square = 0, diamond = 45, hex = 0, hex45 = 45, hex90 = 90, hex135 = 135 }

function M.finite(n)
    return type(n) == "number" and n == n and math.abs(n) < math.huge
end

function M.new(options)
    options = options or {}
    local shape = angles[options.shape] and options.shape or "square"
    local step = options.spacing
    if not M.finite(step) or step < 0.1 or step > 32 then step = 0.5 end
    local angle = (angles[shape] + (M.finite(options.rotation) and options.rotation or 0)) * math.pi / 180
    local origin = options.origin or {}
    return {
        shape = shape, hex = shape:sub(1, 3) == "hex", spacing = step,
        c = math.cos(angle), s = math.sin(angle),
        x = M.finite(origin.x) and origin.x or 0, z = M.finite(origin.z) and origin.z or 0,
    }
end

function M.coordinates(grid, x, z)
    local dx, dz = x - grid.x, z - grid.z
    local u = (dx * grid.c + dz * grid.s) / grid.spacing
    local v = (-dx * grid.s + dz * grid.c) / grid.spacing
    if grid.hex then v = v * 2 / sqrt3; u = u - v / 2 end
    return u, v
end

function M.point(grid, col, row)
    local x = grid.spacing * (col + (grid.hex and row / 2 or 0))
    local z = grid.spacing * row * (grid.hex and sqrt3 / 2 or 1)
    return grid.x + x * grid.c - z * grid.s, grid.z + x * grid.s + z * grid.c
end

function M.snap(grid, x, z)
    local u, v = M.coordinates(grid, x, z)
    local col, row = math.floor(u + 0.5), math.floor(v + 0.5)
    if grid.hex then
        local w = -u - v
        local third = math.floor(w + 0.5)
        local du, dv, dw = math.abs(col - u), math.abs(row - v), math.abs(third - w)
        if du > dv and du > dw then col = -row - third
        elseif dv > dw then row = -col - third end
    end
    local sx, sz = M.point(grid, col, row)
    return sx, sz, col, row
end

-- Includes the boundary of a convex polygon, but rejects collapsed selections.
function M.contains(polygon, x, z)
    local direction, area, origin = nil, 0, polygon[1]
    for i, p in ipairs(polygon) do
        local q = polygon[i % #polygon + 1]
        area = area + (p.x - origin.x) * (q.z - origin.z) - (q.x - origin.x) * (p.z - origin.z)
        local cross = (q.x - p.x) * (z - p.z) - (q.z - p.z) * (x - p.x)
        if math.abs(cross) > 1e-7 then
            local sign = cross > 0
            if direction ~= nil and direction ~= sign then return false end
            direction = sign
        end
    end
    return #polygon >= 3 and math.abs(area) > 1e-7
end

-- Convex selection polygons are scanned in alternating rows; no truncated plans.
function M.area(grid, polygon, limit)
    limit = math.max(1, math.floor(tonumber(limit) or 100))
    if #polygon < 3 then return nil, "invalid_area" end
    local umin, umax, vmin, vmax = math.huge, -math.huge, math.huge, -math.huge
    for _, p in ipairs(polygon) do
        if not M.finite(p.x) or not M.finite(p.z) then return nil, "invalid_area" end
        local u, v = M.coordinates(grid, p.x, p.z)
        umin, umax, vmin, vmax = math.min(umin, u), math.max(umax, u), math.min(vmin, v), math.max(vmax, v)
    end
    if not M.contains(polygon, polygon[1].x, polygon[1].z) then return nil, "invalid_area" end
    umin, umax = math.ceil(umin - 1e-7), math.floor(umax + 1e-7)
    vmin, vmax = math.ceil(vmin - 1e-7), math.floor(vmax + 1e-7)
    -- A pathological thin selection must not scan an unbounded bounding box.
    if (umax - umin + 1) * (vmax - vmin + 1) > math.max(4096, limit * 16) then
        return nil, "area_too_large"
    end
    local points = {}
    for row = vmin, vmax do
        local reverse = (row - vmin) % 2 == 1
        for col = reverse and umax or umin, reverse and umin or umax, reverse and -1 or 1 do
            local x, z = M.point(grid, col, row)
            if M.contains(polygon, x, z) then
                if #points >= limit then return nil, "too_many_points" end
                points[#points + 1] = { x = x, y = 0, z = z, col = col, row = row }
            end
        end
    end
    return points
end

return M
