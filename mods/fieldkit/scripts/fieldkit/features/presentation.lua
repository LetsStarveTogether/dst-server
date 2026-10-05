local Query = require("fieldkit/game/query")

local M = {}
local seasons = { autumn = { "秋", "Autumn", 1, .65, .25 }, winter = { "冬", "Winter", .55, .8, 1 },
    spring = { "春", "Spring", .55, 1, .55 }, summer = { "夏", "Summer", 1, .4, .35 } }

function M.fish_seasons(school, ids)
    local found, result = {}, {}
    for season in pairs(seasons) do
        for _, tile in pairs(school[ids[season:upper()]] or {}) do
            for prefab, weight in pairs(tile) do
                if type(weight) == "number" and weight > 0 then
                    found[prefab] = found[prefab] or {}
                    found[prefab][season] = true
                end
            end
        end
    end
    for prefab, present in pairs(found) do
        local season = next(present)
        if not next(present, season) then result[prefab], result[prefab .. "_inv"] = season, season end
    end
    return result
end

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

function M.name_record(player, client)
    local name = client and client.name or player:GetDisplayName()
    local colour = client and type(client.colour) == "table" and client.colour or {}
    if type(name) ~= "string" or name == "" then
        name = player:GetDisplayName() or "?"
    end
    return name, {
        finite(colour[1]) and colour[1] or 1,
        finite(colour[2]) and colour[2] or 1,
        finite(colour[3]) and colour[3] or 1,
    }
end

function M.new(ctx)
    local G, labels = ctx.G, {}
    local self = {}
    local fish = ctx.params and M.fish_seasons(require("prefabs/oceanfishdef").school, G.SEASONS) or {}

    local function display(entity, text, colour, height, size)
        local label = labels[entity]
        if not label or not label:IsValid() then
            label = G.CreateEntity()
            label.entity:AddTransform()
            label.entity:AddLabel()
            for _, tag in ipairs({ "CLASSIFIED", "NOCLICK", "NOBLOCK" }) do label:AddTag(tag) end
            label.persists = false
            label.entity:SetParent(entity.entity)
            label.Label:SetFont(G.DEFAULTFONT)
            labels[entity] = label
        end
        if label._fieldkit_text ~= text then label.Label:SetText(text); label._fieldkit_text = text end
        label.Label:SetColour(unpack(colour))
        label.Label:SetFontSize(size * (ctx.config.text_scale or 1))
        label.Label:SetWorldOffset(0, height, 0)
        label.Label:Enable(true)
    end

    local function update_names()
        local present = {}
        if ctx.config.names and ctx:ready() then
            local radius = 20
            for _, player in ipairs(G.AllPlayers or {}) do
                if player:IsValid() and ctx.player:GetDistanceSqToInst(player) <= radius * radius
                    and Query.visible(ctx, player) then
                    local client = player.userid and G.TheNet:GetClientTableForUser(player.userid)
                    local name, colour = M.name_record(player, client)
                    display(player, name, colour, player:HasTag("playerghost") and 4 or 3, 24)
                    present[player] = true
                end
            end
        end
        if ctx.params and ctx.params.fish_labels and ctx:ready() then
            local p, count = ctx.player:GetPosition(), 0
            for _, entity in ipairs(G.TheSim:FindEntities(p.x, 0, p.z, 30, { "oceanfish" }, { "INLIMBO", "CLASSIFIED" })) do
                local season = fish[entity.prefab] and seasons[fish[entity.prefab]]
                if season and Query.observed(ctx, entity, true) then
                    display(entity, ctx:text(season[1], season[2]) .. " · " .. entity:GetDisplayName(),
                        { season[3], season[4], season[5] }, 1.5, 21)
                    present[entity], count = true, count + 1
                    if count == 32 then break end
                end
            end
        end
        for player, label in pairs(labels) do
            if not present[player] then
                if label:IsValid() then label:Remove() end
                labels[player] = nil
            end
        end
    end

    ctx.scope:periodic(0.25, update_names)
    ctx.scope:defer(function()
        for _, label in pairs(labels) do
            if label:IsValid() then label:Remove() end
        end
        labels = {}
    end)

    update_names()
    return self
end

return M
