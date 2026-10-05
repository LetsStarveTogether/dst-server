local Rules = require("fieldkit/rules/loot")
local M = {}
local DURATION = 30

local function point(entity)
    local x, _, z = entity.Transform:GetWorldPosition()
    return { x = x, z = z }
end

local function distance(a, b) return (a.x - b.x)^2 + (a.z - b.z)^2 end

local function defeated(entity)
    local anim = entity.AnimState
    if entity.prefab == "alterguardian_phase4_lunarrift" then
        -- This boss drops loot at the end of its finale, long after its health reaches zero.
        return anim and anim:IsCurrentAnimation("finale_pst") or false
    end
    local health = entity.replica and entity.replica.health
    if health and health:IsDead() then return true end
    return anim and (anim:IsCurrentAnimation("defeat") or anim:IsCurrentAnimation("defeat_idle_loop")) or false
end

function M.new(ctx)
    local G, records, sources, evidence = ctx.G, {}, {}, {}
    -- Weak identity sets prevent repeated alerts while leaving entity lifetime to the engine.
    local seen = setmetatable({}, { __mode = "k" })
    local seen_effects = setmetatable({}, { __mode = "k" })
    local names = Rules.blueprint_names(G)
    local baseline, serial, last_sound = false, 0, -math.huge
    local self = {}

    local function eligible(entity)
        local item = entity.replica and entity.replica.inventoryitem
        if not item or item:IsHeld() or entity:HasTag("NOCLICK") then return false end
        local prefab = entity.prefab
        if prefab == "blueprint" then
            -- Blueprint factories erase their specific prefab. Only the replicated name identifies a recipe.
            local recipe = names[entity.name] or nil
            local builder = ctx.player.replica.builder
            if recipe and builder and builder:KnowsRecipe(recipe, true) then return false end
            return true, recipe, not recipe
        end
        return (Rules.items[prefab] or Rules.materials[prefab]) ~= nil
    end

    local function add_evidence(entity, now)
        local radius = entity.GetPhysicsRadius and entity:GetPhysicsRadius(1.75) or 1.75
        evidence[#evidence + 1] = { prefab = entity.prefab, point = point(entity),
            -- Ancient Fuelweaver's death3 drops its loot at frame 304, after a 12-frame prelude.
            expires = now + (entity.prefab == "stalker_atrium" and 14 or 6), radius = math.max(3, radius + 2) }
        if #evidence > 64 then table.remove(evidence, 1) end
    end

    local function observe_sources(entities, now)
        for i = #evidence, 1, -1 do
            if now > evidence[i].expires then table.remove(evidence, i) end
        end
        local current, count = {}, 0
        for _, entity in ipairs(entities) do
            if Rules.sources[entity.prefab] then
                if Rules.effects[entity.prefab] then
                    local relevant = entity.prefab ~= "collapse_small"
                    if not relevant then
                        local center = point(entity)
                        for _, candidate in ipairs(entities) do
                            if candidate:HasTag("junk_pile_big") and distance(center, point(candidate)) <= 225 then
                                relevant = true; break
                            end
                        end
                    end
                    if baseline and relevant and not seen_effects[entity] then add_evidence(entity, now) end
                    seen_effects[entity] = true
                elseif count < 128 then
                    local finished, previous = defeated(entity), sources[entity]
                    if previous and previous.serial == serial - 1 and not previous.finished and finished then
                        add_evidence(entity, now)
                    end
                    current[entity] = { finished = finished, serial = serial }
                    count = count + 1
                end
            end
        end
        sources = current
    end

    local function has_evidence(entity, recipe, position)
        -- on_loot_dropped is server-local. Nearby visible events are evidence, not proof of origin.
        for _, event in ipairs(evidence) do
            if Rules.matches(entity.prefab, recipe, event.prefab)
                and distance(position, event.point) <= event.radius^2 then return true end
        end
        return false
    end

    function self:observe(entities, now)
        serial = serial + 1
        observe_sources(entities, now)
        local visible, count, materials = {}, 0, 0
        for _, entity in ipairs(entities) do visible[entity] = true end
        for entity, record in pairs(records) do
            if not visible[entity] or not entity:IsValid() or now >= record.expires or not eligible(entity) then
                records[entity] = nil
            else
                count = count + 1
                if Rules.materials[entity.prefab] then materials = materials + 1 end
            end
        end
        local frame, sound = {}, false
        for _, entity in ipairs(entities) do
            local enabled, recipe, unknown = eligible(entity)
            if enabled then
                local record = records[entity]
                if not seen[entity] then
                    seen[entity] = true
                    -- Reserve tracking capacity for rare drops in crowded material piles.
                    local material = Rules.materials[entity.prefab]
                    if count < 256 and (not material or materials < 64) then
                        record = { first = now, expires = now + DURATION, recent = baseline }
                        records[entity] = record
                        count = count + 1
                        if material then materials = materials + 1 end
                    end
                end
                if record then
                    local position = point(entity)
                    if record.recent and not record.glow and now - record.first <= 3
                        and not unknown and has_evidence(entity, recipe, position) then
                        record.glow = true
                        record.expires = now + DURATION
                        sound = true
                    end
                    local suffix = unknown and ctx:text(" · 配方未识别", " · recipe unidentified")
                        or record.glow and ctx:text(" · 疑似新掉落", " · likely new drop")
                        or record.recent and ctx:text(" · 新近看到", " · newly observed") or ""
                    frame[#frame + 1] = { key = "loot:" .. entity.GUID, point = position, entity = entity,
                        label = entity:GetDisplayName() .. suffix, glow = record.glow == true,
                        quality = record.glow and "inferred" or "observed", expires = record.expires,
                        first = record.first }
                end
            end
        end
        baseline = true
        local origin = point(ctx.player)
        table.sort(frame, function(a, b)
            if a.glow ~= b.glow then return a.glow end
            if a.first ~= b.first then return a.first > b.first end
            local da, db = distance(a.point, origin), distance(b.point, origin)
            return da < db or da == db and a.entity.GUID < b.entity.GUID
        end)
        for i = #frame, 25, -1 do frame[i] = nil end
        if sound and ctx.config.loot_sound and now - last_sound >= 5 then
            local emitter = G.TheFocalPoint and G.TheFocalPoint.SoundEmitter
            if emitter then emitter:PlaySound("dontstarve/HUD/click_mouseover"); last_sound = now end
        end
        return frame
    end

    ctx.scope:defer(function() records, sources, evidence, seen, seen_effects = {}, {}, {}, {}, {} end)
    return self
end

return M
