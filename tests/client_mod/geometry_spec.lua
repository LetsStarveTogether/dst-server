local T = require("tests.client_mod.support")
local G = require("fieldkit/rules/geometry")
local function near(a, b) return math.abs(a - b) < 1e-8 end
local shapes = { "square", "diamond", "hex", "hex45", "hex90", "hex135" }
local sqrt2, sqrt3, sqrt6 = math.sqrt(2), math.sqrt(3), math.sqrt(6)
-- Unit column/row vectors in world coordinates, independent of the grid implementation.
local basis = {
    square = { 1, 0, 0, 1 },
    diamond = { sqrt2 / 2, sqrt2 / 2, -sqrt2 / 2, sqrt2 / 2 },
    hex = { 1, 0, .5, sqrt3 / 2 },
    hex45 = { sqrt2 / 2, sqrt2 / 2, (sqrt2 - sqrt6) / 4, (sqrt2 + sqrt6) / 4 },
    hex90 = { 0, 1, -sqrt3 / 2, .5 },
    hex135 = { -sqrt2 / 2, sqrt2 / 2, -(sqrt2 + sqrt6) / 4, (sqrt2 - sqrt6) / 4 },
}
local function expected_point(shape, spacing, rotation, origin, u, v)
    local b = basis[shape]
    local x, z = spacing * (u * b[1] + v * b[3]), spacing * (u * b[2] + v * b[4])
    local c, s = math.cos(math.rad(rotation)), math.sin(math.rad(rotation))
    return origin.x + c * x - s * z, origin.z + s * x + c * z
end
T.case("published_shapes", function()
    assert(table.concat(G.shapes, ",") == table.concat(shapes, ","))
end)
-- Fixed (column=2, row=-1) anchors also calibrate the oracle's scale, quarter turn and translation.
T.each("known_basis", {
    { name = "square", x = 2, z = -1 },
    { name = "diamond", x = 2.121320343559643, z = .707106781186548 },
    { name = "hex", x = 1.5, z = -.866025403784439 },
    { name = "hex45", x = 1.673032607475616, z = .448287736084027 },
    { name = "hex90", x = .866025403784439, z = 1.5 },
    { name = "hex135", x = -.448287736084027, z = 1.673032607475616 },
}, function(anchor)
    local origin = { x = -7, z = 5 }
    local x, z = expected_point(anchor.name, 2, 90, origin, 2, -1)
    local sx, sz = G.point(G.new({ shape = anchor.name, spacing = 2, rotation = 90, origin = origin }), 2, -1)
    assert(near(x, -7 - 2 * anchor.z) and near(z, 5 + 2 * anchor.x), "known oracle anchor")
    assert(near(sx, x) and near(sz, z), "known world anchor")
end)
T.each("lattice", shapes, function(shape)
    T.each("spacing", { .5, 1.3 }, function(spacing)
        T.each("rotation", { 0, 17, 90 }, function(rotation)
            T.each("origin", { { name = "negative", x = -13.7, z = 8.2 }, { name = "positive", x = 7.4, z = -3.1 } }, function(origin)
                local grid = G.new({ shape = shape, spacing = spacing, rotation = rotation, origin = origin })
                for u = -4, 4 do
                    for v = -4, 4 do
                        local x, z = expected_point(shape, spacing, rotation, origin, u, v)
                        local px, pz = G.point(grid, u, v)
                        assert(near(x, px) and near(z, pz), "world coordinates")
                        local sx, sz, col, row = G.snap(grid, x, z)
                        assert(near(x, sx) and near(z, sz) and col == u and row == v, "snap known lattice point")
                    end
                end
                -- Compare against independent Euclidean enumeration, including negative coordinates.
                for i = 1, 50 do
                    local x, z = origin.x - 2.3 + i * .113, origin.z - 3.1 + i * .071
                    local sx, sz, col, row = G.snap(grid, x, z)
                    local px, pz = expected_point(shape, spacing, rotation, origin, col, row)
                    assert(col == math.floor(col) and row == math.floor(row) and near(sx, px) and near(sz, pz), "snap lies on lattice")
                    local chosen = (x - sx)^2 + (z - sz)^2
                    local nearest = math.huge
                    for u = -10, 10 do
                        for v = -10, 10 do
                            px, pz = expected_point(shape, spacing, rotation, origin, u, v)
                            nearest = math.min(nearest, (x - px)^2 + (z - pz)^2)
                        end
                    end
                    assert(near(chosen, nearest), "nearest-point distance")
                end
            end)
        end)
    end)
end)
T.each("invalid_spacing", { 0, -1, 0 / 0, math.huge, -math.huge }, function(step)
    assert(G.new({ spacing = step }).spacing == .5, "invalid spacing cannot produce NaN")
end)
T.case("bounded_polygon_plans", function()
    local square = G.new({ spacing = 1 })
    local polygon = { { x = -1, z = -1 }, { x = 1, z = -1 }, { x = 1, z = 1 }, { x = -1, z = 1 } }
    local points = assert(G.area(square, polygon, 9))
    assert(#points == 9 and points[1].x == -1 and points[4].x == 1 and points[7].x == -1, "serpentine rows")
    local over, why = G.area(square, polygon, 8)
    assert(not over and why == "too_many_points", "plans never silently truncate")
    local diamond = { { x = 0, z = -1 }, { x = 1, z = 0 }, { x = 0, z = 1 }, { x = -1, z = 0 } }
    assert(#G.area(square, diamond, 10) == 5, "screen polygon excludes bounding-box corners")
    assert(G.contains(diamond, 0, 0) and G.contains(diamond, 0, -1), "convex selection includes its boundary")
    assert(not G.contains(diamond, .8, .8), "a bounding box corner is outside the rotated selection")
    local collapsed = { { x = 0, z = 0 }, { x = 0, z = 1 }, { x = 0, z = 2 } }
    assert(not G.contains(collapsed, 0, 1), "collapsed selections contain no targets")
    local invalid, reason = G.area(square, collapsed, 10)
    assert(not invalid and reason == "invalid_area", "collapsed selections cannot become construction plans")
    local large = { { x = -1e6, z = -1e6 }, { x = 1e6, z = -1e6 }, { x = 1e6, z = 1e6 }, { x = -1e6, z = 1e6 } }
    assert(G.area(square, large, 100) == nil, "scan work is bounded before allocation")
end)
T.done()
