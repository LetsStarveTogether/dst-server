local Route = require("fieldkit/rules/route")
local Search = require("fieldkit/rules/path_search")
local Query = require("fieldkit/game/query")
local M = {}

local function distance(a, b)
    return math.sqrt((a.x - b.x)^2 + (a.z - b.z)^2)
end

local radii = {
    dragonfly = "DRAGONFLY_AGGRO_DIST", walrus = "WALRUS_TARGET_DIST", spat = "SPAT_TARGET_DIST",
    bishop = "BISHOP_TARGET_DIST", bishop_nightmare = "BISHOP_TARGET_DIST",
    mushroombomb = "TOADSTOOL_MUSHROOMBOMB_RADIUS", mushroombomb_dark = "TOADSTOOL_MUSHROOMBOMB_RADIUS",
    eyeplant = "EYEPLANT_ATTACK_DIST", wasphive = 10,
}

-- These are observed avoidance circles, not server targeting predictions.
function M.hazards(ctx, movement)
    local G, player, result = ctx.G, ctx.player, {}
    local p = player:GetPosition()
    for _, entity in ipairs(G.TheSim:FindEntities(p.x, 0, p.z, 80, nil, { "INLIMBO", "CLASSIFIED" })) do
        if entity ~= player and Query.observed(ctx, entity, true)
            and not (entity.replica.health and entity.replica.health:IsDead()) then
            local r = radii[entity.prefab]
            r = type(r) == "string" and G.TUNING[r] or r
            if entity:HasTag("mineactive") and (entity:HasTag("trapdamage") or entity.prefab == "trap_teeth_maxwell")
                and not player:HasTag("notraptrigger") and not player:HasTag("flying") then
                r = entity.prefab == "trap_starfish" and G.TUNING.STARFISH_TRAP_RADIUS or G.TUNING.TRAP_TEETH_RADIUS
            elseif entity:HasTag("sporecloud") then r = G.TUNING.TOADSTOOL_SPORECLOUD_RADIUS
            elseif entity:HasTag("deer_ice_circle") or entity:HasTag("deer_fire_circle") then r = 3
            elseif entity:HasTag("miasma") then r = G.TILE_SCALE / 2
            elseif entity:HasTag("beefalo") and entity:HasTag("scarytoprey") and not player:HasTag("beefalo") then
                r = G.TUNING.BEEFALO_TARGET_DIST
            elseif entity.prefab == "cookiecutter" and movement.sailing then
                r = G.TUNING.COOKIECUTTER and G.TUNING.COOKIECUTTER.BOAT_DETECTION_SHARE_DIST
            end
            if entity.prefab == "eyeplant" and player:HasTag("eyeplant_friend") then r = nil end
            if r and r > 0 then
                local point = entity:GetPosition()
                result[#result + 1] = { x = point.x, z = point.z, radius = r + (movement.radius or .5) + .5 }
                if #result == 128 then break end
            end
        end
    end
    return result
end

function M.safe(a, b, hazards)
    local dx, dz = b.x - a.x, b.z - a.z
    local length = dx * dx + dz * dz
    for _, circle in ipairs(hazards) do
        local ax, az = a.x - circle.x, a.z - circle.z
        local start = ax * ax + az * az
        if start < circle.radius^2 then
            -- A player already in danger can leave, but cannot move deeper into it.
            if ax * dx + az * dz < 0 or distance(b, circle) <= math.sqrt(start) then return false end
        else
            local t = length > 0 and math.max(0, math.min(1, -(ax * dx + az * dz) / length)) or 0
            if (ax + t * dx)^2 + (az + t * dz)^2 < circle.radius^2 then return false end
        end
    end
    return true
end

function M.new(ctx, movement, origin, goal, options)
    local G, player, world = ctx.G, ctx.player, ctx.G.TheWorld
    local map, finder = world.Map, world.Pathfinder
    local x, _, z = map:GetTileCenterPoint(origin.x, 0, origin.z)
    local grid = { x = x, z = z, size = G.TILE_SCALE }
    local self = { origin = { x = origin.x, z = origin.z }, goal = { x = goal.x, z = goal.z }, state = "planning" }
    local hazards, checked = {}, -math.huge
    local search, handle, started
    local fast = options.fast and not movement.sailing
    local locomotor = player.components.playercontroller.locomotor
    local multiplier = locomotor and locomotor.fastmultiplier or 1.3
    multiplier = math.max(1, math.min(10, multiplier))
    local function explored(point) return player:CanSeePointOnMiniMap(point.x, 0, point.z) end
    function self:clear(a, b)
        if options.avoid then
            local now = G.GetTime()
            if now - checked >= .5 then hazards, checked = M.hazards(ctx, movement), now end
            if not M.safe(a, b, hazards) then return false end
        end
        return movement:clear(a, b)
    end
    local function clear(a, b) return self:clear(a, b) end
    local function cost(point)
        if not fast or locomotor and locomotor.enablegroundspeedmultiplier == false then return 1 end
        local rider = player.replica.rider
        local mount = rider and rider:IsRiding() and rider:GetMount() or nil
        local tile = map:GetTileAtPoint(point.x, 0, point.z)
        local road = locomotor and locomotor.FasterOnRoad and locomotor:FasterOnRoad()
            or not locomotor and (not mount or rider:GetMountFasterOnRoad())
        local turf = locomotor and locomotor.IsFasterOnGroundTile and locomotor:IsFasterOnGroundTile(tile)
            or (mount or player):HasTag("turfrunner_" .. tostring(tile))
        local creep = world.GroundCreep and world.GroundCreep:OnCreep(point.x, 0, point.z)
        local triggerscreep = locomotor and locomotor.triggerscreep
        if triggerscreep == nil then triggerscreep = not player:HasTag("spiderwhisperer") end
        if creep and triggerscreep then
            local slow = locomotor and locomotor.slowmultiplier or .6
            return player:HasTag("vigorbuff") and 1 or 1 / math.max(.01, slow)
        end
        local webfast = not mount and (locomotor and locomotor.FasterOnCreep and locomotor:FasterOnCreep()
            or not locomotor and player:HasTag("spiderwhisperer"))
        if turf or road and ((G.RoadManager and G.RoadManager:IsOnRoad(point.x, 0, point.z))
            or G.GROUND_ROADWAYS[tile]) or creep and webfast then return 1 / multiplier end
        return 1
    end
    local function kill()
        if handle then finder:KillSearch(handle) end
        handle = nil
    end
    function self:cancel() kill(); search = nil; self.state = "cancelled" end
    local function complete(points, reason)
        self.path, self.reason = points, reason
        self.state = points and "done" or "failed"
        kill()
        return self.state, self.path, self.reason
    end
    local function grid_search()
        search = Search.new(origin, goal, grid, clear, explored,
            function(point) return movement:passable(point) end, cost, fast and 1 / multiplier or 1)
    end
    function self:step(now)
        now = now or G.GetTime()
        if self.state ~= "planning" then return self.state, self.path, self.reason end
        if not movement:valid() then return complete(nil, "invalid_destination") end
        if search then
            local state, points, reason = search:step()
            if state == "planning" then return state end
            return complete(points, reason)
        end
        if handle then
            local status = finder:GetSearchStatus(handle)
            if status == 0 and now - started <= 10 then return "planning" end
            local result = status == 1 and finder:GetSearchResult(handle)
            kill()
            local points = result and result.steps and Route.plan(origin, goal, result.steps, grid, clear, explored)
            if points then return complete(points) end
            grid_search()
            return "planning"
        end
        local direct_reason
        if not fast then
            local points, reason = Route.plan(origin, goal, nil, grid, clear, explored)
            if points then return complete(points) end
            direct_reason = reason
        end
        if options.native and not fast and not movement.sailing and direct_reason == "blocked_path" then
            handle = finder:SubmitSearch(origin.x, 0, origin.z, goal.x, 0, goal.z, movement:capabilities())
            if handle then started = now; return "planning" end
        end
        grid_search()
        return "planning"
    end
    return self
end

return M
