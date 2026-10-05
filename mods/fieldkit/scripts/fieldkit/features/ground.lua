local Query = require("fieldkit/game/query")
local M = {}

local function identity(entity)
    -- Native clients expose item skins through AnimState, without Lua skin names.
    local skin = entity.AnimState and entity.AnimState:GetSkinBuild() or entity:GetSkinName() or ""
    return entity.prefab .. ":" .. skin, skin
end

local function surface(entity)
    if entity:GetCurrentPlatform() then return "platform" end
    return entity:IsOnOcean(false) and "ocean" or "land"
end

function M.new(ctx)
    local self = { groups = {}, selected = {} }

    local function on_surface(entity, terrain, platform)
        if terrain == "current" then
            return ctx.player:GetCurrentPlatform() == platform and entity:GetCurrentPlatform() == platform
                and (platform ~= nil or surface(entity) == "land")
        end
        return not terrain or terrain == "all" or surface(entity) == terrain
    end

    local function usable(entity, key, group, ignore_full)
        if not Query.visible(ctx, entity) or identity(entity) ~= key then return false end
        local item = entity.replica and entity.replica.inventoryitem
        if not item or item:IsHeld() or not item:CanBePickedUp(ctx.player)
            or ctx.player:GetDistanceSqToInst(entity) > ctx.params.search_radius ^ 2
            or not on_surface(entity, group.surface, group.platform) then return false end
        local stack = entity.replica.stackable
        return not ignore_full or not stack or not stack:IsFull()
    end

    function self:held_filter()
        local inv = ctx.player.replica.inventory
        local item = inv:GetActiveItem() or inv:GetEquippedItem(ctx.G.EQUIPSLOTS.HANDS)
        if not item then return end
        return identity(item)
    end

    function self:search(text, options)
        options = options or {}
        local needle = string.lower(text or ""):match("^%s*(.-)%s*$")
        local held = options.held and self:held_filter() or nil
        local platform = ctx.player:GetCurrentPlatform()
        local ordered, groups = {}, {}
        local function matches(entity)
            if options.held and (not held or identity(entity) ~= held) then return false end
            if not on_surface(entity, options.surface, platform) then return false end
            return needle == "" or string.find(string.lower(entity:GetDisplayName()), needle, 1, true) ~= nil
                or string.find(string.lower(entity.prefab), needle, 1, true) ~= nil
        end
        for _, entity in ipairs(Query.ground_items(ctx, matches)) do
            local key, skin = identity(entity)
            local group = groups[key]
            if not group then
                local item = entity.replica.inventoryitem
                local skin_names = ctx.G.STRINGS and ctx.G.STRINGS.SKIN_NAMES
                local skin_name = entity:GetSkinName() or skin
                group = { key = key, prefab = entity.prefab, skin = skin,
                    skin_name = skin ~= "" and (skin_names and skin_names[skin_name] or skin_name) or nil,
                    name = entity:GetDisplayName(), count = 0, entities = {}, nearest = entity,
                    surface = options.surface or "all", platform = platform,
                    distance = math.sqrt(ctx.player:GetDistanceSqToInst(entity)),
                    atlas = item:GetAtlas(), image = item:GetImage() }
                groups[key], ordered[#ordered + 1] = group, group
            end
            group.entities[#group.entities + 1] = entity
            local stack = entity.replica.stackable
            group.count = group.count + (stack and stack:StackSize() or 1)
        end
        table.sort(ordered, function(a, b)
            return a.name < b.name or a.name == b.name and a.key < b.key
        end)
        self.groups = groups
        return ordered
    end

    function self:toggle(key)
        if self.selected[key] then self.selected[key] = nil; return true end
        local group = self.groups[key]
        if not group then return false end
        -- Selection captures the displayed members. Refreshes never add targets to a task.
        local captured = { surface = group.surface, platform = group.platform, entities = {} }
        for _, entity in ipairs(group.entities) do captured.entities[#captured.entities + 1] = entity end
        self.selected[key] = captured
        return true
    end

    function self:clear() self.selected = {} end

    function self:selected_count()
        local count = 0
        for _ in pairs(self.selected) do count = count + 1 end
        return count
    end

    function self:pickup_selected()
        local entries = {}
        for key, group in pairs(self.selected) do
            for _, entity in ipairs(group.entities) do
                if usable(entity, key, group, ctx.params.ground_ignore_full) then
                    entries[#entries + 1] = { target = entity, button = "left", id = "PICKUP" }
                end
            end
        end
        if #entries == 0 then return false, ctx:text("所选物品已不可拾取", "Selected items are no longer available") end
        table.sort(entries, function(a, b)
            local da, db = ctx.player:GetDistanceSqToInst(a.target), ctx.player:GetDistanceSqToInst(b.target)
            return da < db or da == db and a.target.GUID < b.target.GUID
        end)
        local ok, reason = ctx.work:enqueue(entries)
        if ok then self:clear() end
        return ok, reason
    end

    function self:location(key)
        local group, nearest, distance = self.groups[key]
        if not group then return end
        for _, entity in ipairs(group.entities) do
            if usable(entity, key, group, false) then
                local d = ctx.player:GetDistanceSqToInst(entity)
                if not distance or d < distance then nearest, distance = entity, d end
            end
        end
        if not nearest then return end
        local x, _, z = nearest.Transform:GetWorldPosition()
        return { point = { x = x, z = z }, label = string.format("%s · %.1fm", group.name, math.sqrt(distance)) }
    end

    function self:pickup(key, all)
        local group = self.groups[key]
        if not group then return false, ctx:text("所选物品已不在结果中", "Selected item is no longer in the results") end
        local entries = {}
        for _, entity in ipairs(group.entities) do
            if usable(entity, key, group, ctx.params.ground_ignore_full) then
                entries[#entries + 1] = { target = entity, button = "left", id = "PICKUP" }
                if not all then break end
            end
        end
        if #entries == 0 then return false, ctx:text("所选物品已不可拾取", "Selected item is no longer available") end
        -- Preserve the displayed group, including its skin and surface filters.
        if all then return ctx.work:enqueue(entries) end
        return ctx.work:pickup(entries[1].target)
    end

    return self
end

return M
