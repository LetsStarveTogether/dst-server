local Route = require("fieldkit/rules/route")
local M = {}
local directions = { { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 },
    { 1, 1 }, { 1, -1 }, { -1, 1 }, { -1, -1 } }

local function distance(a, b)
    return math.sqrt((a.x - b.x)^2 + (a.z - b.z)^2)
end

-- Heap entries retain their score so a cheaper visit cannot invalidate heap order.
local function push(heap, node, score)
    local entry, i = { node = node, score = score, cost = node.cost }, #heap + 1
    while i > 1 do
        local parent = math.floor(i / 2)
        if heap[parent].score <= score then break end
        heap[i], i = heap[parent], parent
    end
    heap[i] = entry
end

local function pop(heap)
    local first, last = heap[1], table.remove(heap)
    if #heap > 0 then
        local i = 1
        while i * 2 <= #heap do
            local child = i * 2
            if child < #heap and heap[child + 1].score < heap[child].score then child = child + 1 end
            if last.score <= heap[child].score then break end
            heap[i], i = heap[child], child
        end
        heap[i] = last
    end
    return first
end

-- Each step expands at most budget nodes. The whole search also has a fixed memory limit.
-- Edge predicates include physics, fog and danger; rejected cells never contribute speed data.
function M.new(origin, goal, grid, clear, explored, passable, cost, minimum, spacing)
    local self = { state = "planning", visited = 0 }
    local heap, nodes, count = {}, {}, 1
    local step = spacing or 2
    local start = { x = origin.x, z = origin.z, ix = 0, iz = 0, cost = 0 }
    local finish = { x = goal.x, z = goal.z, cost = math.huge }
    nodes["0:0"] = start
    push(heap, start, distance(start, finish) * minimum)
    local function edge(a, b)
        if not passable(b) or not Route.plan(a, b, nil, grid, clear, explored) then return end
        local middle = { x = (a.x + b.x) / 2, z = (a.z + b.z) / 2 }
        local weight = (cost(a) + 2 * cost(middle) + cost(b)) / 4
        return distance(a, b) * math.max(minimum, weight)
    end
    local function visit(a, b)
        if b.closed then return end
        local length = edge(a, b)
        if length and a.cost + length < b.cost then
            b.cost, b.parent = a.cost + length, a
            push(heap, b, b.cost + distance(b, finish) * minimum)
        end
    end
    local function result()
        local reverse, node = {}, finish
        while node and node ~= start do
            reverse[#reverse + 1] = node
            if #reverse > 2048 then return nil, "complex_path" end
            node = node.parent
        end
        local points = { origin }
        for i = #reverse, 1, -1 do
            local point = reverse[i]
            local a, b = points[#points - 1], points[#points]
            -- Only merge collinear forward segments; shortcuts could remove the chosen road.
            if a and math.abs((b.x - a.x) * (point.z - b.z) - (b.z - a.z) * (point.x - b.x)) < 1e-8
                and (b.x - a.x) * (point.x - b.x) + (b.z - a.z) * (point.z - b.z) >= 0 then
                points[#points] = point
            else points[#points + 1] = point end
        end
        return Route.plan(origin, goal, points, grid, clear, explored)
    end
    function self:step(budget)
        if self.state ~= "planning" then return self.state, self.path, self.reason end
        for _ = 1, math.min(64, budget or 24) do
            local entry = pop(heap)
            if not entry then self.state, self.reason = "failed", "no_path"; break end
            local node = entry.node
            if not node.closed and entry.cost == node.cost then
                node.closed = true
                self.visited = self.visited + 1
                if node == finish then
                    self.path, self.reason = result()
                    self.state = self.path and "done" or "failed"
                    break
                end
                if self.visited >= 8192 or count >= 16384 or #heap >= 32768 then
                    self.state, self.reason = "failed", "complex_path"
                    break
                end
                if distance(node, finish) <= step * 2 then visit(node, finish) end
                for _, offset in ipairs(directions) do
                    local ix, iz = node.ix + offset[1], node.iz + offset[2]
                    local key = ix .. ":" .. iz
                    local next_node = nodes[key]
                    if not next_node then
                        count = count + 1
                        next_node = { x = origin.x + ix * step, z = origin.z + iz * step,
                            ix = ix, iz = iz, cost = math.huge }
                        nodes[key] = next_node
                    end
                    visit(node, next_node)
                end
            end
        end
        return self.state, self.path, self.reason
    end
    return self
end

return M
