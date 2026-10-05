local T = require("tests.client_mod.support")
local Search = require("fieldkit/rules/path_search")
local Planner = require("fieldkit/game/route_planner")
local grid = { x = 0, z = 0, size = 4 }
local function yes() return true end
local function one() return 1 end
local function finish(search)
    for _ = 1, 600 do
        local visited = search.visited
        local state, path, reason = search:step(24)
        assert(search.visited - visited <= 24, "planning is bounded per frame")
        if state ~= "planning" then return path, reason end
    end
    error("search failed to respect its total budget")
end

T.case("faster road wins over a shorter clear line", function()
    local search = Search.new({ x = 0, z = 0 }, { x = 60, z = 0 }, grid, yes, yes, yes,
        function(point) return point.z >= 3 and point.z <= 5 and 1 / 1.3 or 1 end, 1 / 1.3)
    local path = assert(finish(search))
    local road = false
    for _, point in ipairs(path) do if point.z >= 3 and point.z <= 5 then road = true end end
    assert(road and path[#path].x == 60 and path[#path].z == 0)
end)

T.case("danger circles block edges and allow escape", function()
    local circles = { { x = 10, z = 0, radius = 4 } }
    local function clear(a, b) return Planner.safe(a, b, circles) end
    assert(not clear({ x = 0, z = 0 }, { x = 20, z = 0 }), "endpoint checks alone miss a crossed hazard")
    assert(clear({ x = 8, z = 0 }, { x = 4, z = 0 }))
    assert(not clear({ x = 8, z = 0 }, { x = 12, z = 0 }))
    local previous = { x = 0, z = 0 }
    local path = assert(finish(Search.new(previous, { x = 20, z = 0 }, grid, clear, yes, yes, one, 1)))
    for _, point in ipairs(path) do assert(clear(previous, point)); previous = point end
    assert(#path > 1 and previous.x == 20 and previous.z == 0)
end)

T.case("no path stays within explored and passable cells", function()
    local queries = 0
    local function passable(point)
        return point.x >= -4 and point.x <= 20 and math.abs(point.z) <= 8
    end
    local function explored(point) return point.x < 8 or point.x > 12 end
    local function cost(point)
        queries = queries + 1
        assert(explored(point), "unseen tiles never contribute terrain costs")
        return 1
    end
    local path, reason = finish(Search.new({ x = 0, z = 0 }, { x = 20, z = 0 }, grid,
        yes, explored, passable, cost, 1))
    assert(not path and reason == "no_path" and queries > 0)
end)

T.done()
