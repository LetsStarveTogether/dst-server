local Scope = require("fieldkit/core/scope")
local M = {}
local families = { wormhole = "worm", tentacle_pillar = "tentacle", tentacle_pillar_hole = "tentacle" }
local function distance(a, b) return (a.x - b.x)^2 + (a.z - b.z)^2 end
local function point(x, z)
    return type(x) == "number" and type(z) == "number" and x == x and z == z
        and math.abs(x) <= 100000 and math.abs(z) <= 100000
end

function M.new(ctx)
    local G, player = ctx.G, ctx.player
    local self, recent, pending, classified, classified_scope = {}
    local function nearest()
        local origin, best, length = player:GetPosition(), nil, 16
        for _, entity in ipairs(G.TheSim:FindEntities(origin.x, 0, origin.z, 4, nil, { "INLIMBO" })) do
            if families[entity.prefab] and entity:IsValid() and entity.entity:IsVisible()
                and G.CanEntitySeeTarget(player, entity) then
                local p = entity:GetPosition()
                local d = distance(origin, p)
                if d < length then
                    best, length = { x = p.x, z = p.z, family = families[entity.prefab], at = G.GetTime() }, d
                end
            end
        end
        return best
    end
    local function begin(_, kind)
        if not (kind == G.WORMHOLETYPE.WORM or kind == G.WORMHOLETYPE.TENTAPILLAR) then return end
        if pending and G.GetTime() - pending.started < 1 then return end
        local entrance = recent and G.GetTime() - recent.at < 2 and recent or nearest()
        local family = kind == G.WORMHOLETYPE.WORM and "worm" or "tentacle"
        if entrance and entrance.family == family then pending = { entrance = entrance, started = G.GetTime() } end
    end
    local function remember(a, b)
        local records, number = ctx.journal:records("wormhole"), 1
        for _, record in ipairs(records) do
            number = math.max(number, (record.number or 0) + 1)
            if point(record.x, record.z) and point(record.tx, record.tz) then
                local c, d = { x = record.x, z = record.z }, { x = record.tx, z = record.tz }
                if distance(a, c) < 1 and distance(b, d) < 1 or distance(a, d) < 1 and distance(b, c) < 1 then return end
                if distance(a, c) < 1 or distance(a, d) < 1 or distance(b, c) < 1 or distance(b, d) < 1 then
                    ctx.journal:remove(record.key)
                end
            end
        end
        ctx.journal:add("wormhole", { x = a.x, z = a.z, tx = b.x, tz = b.z, number = number, state = a.family })
        ctx.journal:flush()
        ctx:notify(ctx:text("已记录虫洞配对：", "Wormhole pair recorded: ") .. number)
    end
    function self:update()
        if classified ~= player.player_classified then
            if classified_scope then classified_scope:close() end
            classified, classified_scope = player.player_classified, nil
            if classified then
                classified_scope = Scope.new(player, function(err) ctx:report(err) end)
                classified_scope:listen("wormholetraveldirty", function()
                    begin(nil, classified.wormholetravelevent:value())
                end, classified)
            end
        end
        local nearby = nearest()
        if pending then
            if G.GetTime() - pending.started > 10 then pending = nil
            elseif nearby and nearby.family == pending.entrance.family and distance(nearby, pending.entrance) > 16 then
                remember(pending.entrance, nearby)
                pending = nil
            end
        end
        if nearby then recent = nearby end
    end
    function self:markers()
        local result = {}
        if ctx.params.wormhole_marks == false then return result end
        for _, record in ipairs(ctx.journal:records("wormhole")) do
            if point(record.x, record.z) and point(record.tx, record.tz)
                and type(record.number) == "number" and record.number >= 1 and record.number == math.floor(record.number) then
                for _, p in ipairs({ { x = record.x, z = record.z }, { x = record.tx, z = record.tz } }) do
                    if player:CanSeePointOnMiniMap(p.x, 0, p.z) then
                        p.number = record.number
                        result[#result + 1] = p
                    end
                end
            end
        end
        return result
    end
    function self:clear()
        pending, recent = nil, nil
        ctx.journal:clear("wormhole")
        ctx.journal:flush()
    end
    ctx.scope:listen("wormholetravel", begin)
    ctx.scope:periodic(.25, function() self:update() end)
    ctx.scope:defer(function() if classified_scope then classified_scope:close() end end)
    self:update()
    return self
end

return M
