local Commands = require("usercommands")
local M = {}

local function pose(data)
    local animation = data and data.anim
    if data and data.randomanim and type(animation) == "table" then animation = animation[1] end
    if type(animation) == "table" then
        animation = animation[data.loop and #animation or 1]
    end
    return type(animation) == "string" and animation ~= "" and animation or nil
end

function M.new(ctx)
    local self, G = { favourites = {}, enabled = true }, ctx.G
    local edited, enabled_changed = {}, false
    local definitions = {}
    for name, definition in pairs(G.GetCommonEmotes()) do definitions[name] = definition end
    for _, definition in pairs(G.EMOTE_ITEMS) do
        if definition.cmd_name and not definitions[definition.cmd_name] then
            definitions[definition.cmd_name] = definition
        end
    end
    local character = ctx.player.prefab
    if not G.kleifileexists("images/emotes_" .. character .. ".xml") then character = "generic" end
    local atlas = "images/emotes_" .. character .. ".xml"

    local function label(name)
        local definition = definitions[name]
        return G.STRINGS.UI.EMOTES[name:upper()] or definition and definition.displayname or name
    end
    local function save()
        if not ctx.store or not ctx.store.ready then return end
        local slots = {}
        for i = 1, 8 do slots[i] = self.favourites[i] or false end
        ctx.store:set("emotes", { slots = slots, enabled = self.enabled })
    end
    function self:set_favourite(index, name)
        if type(index) ~= "number" or index < 1 or index > 8 or index ~= math.floor(index)
            or name and not definitions[name] then return false end
        self.favourites[index], edited[index] = name or nil, true
        save()
        return true
    end
    function self:toggle_favourites()
        self.enabled, enabled_changed = not self.enabled, true
        save()
    end
    function self:favourite_label(index)
        local name = self.favourites[index]
        return name and label(name) or ctx:text("空位", "Empty")
    end
    if ctx.store then
        ctx.scope:defer(ctx.store:on_ready(function()
            if ctx.scope.closed then return end
            local saved = ctx.store:get("emotes")
            if type(saved) == "table" then
                if not enabled_changed then self.enabled = saved.enabled ~= false end
                if type(saved.slots) == "table" then
                    for i = 1, 8 do
                        local name = saved.slots[i]
                        if not edited[i] and type(name) == "string" and #name <= 80 and name:match("^[%w_]+$") then
                            self.favourites[i] = name
                        end
                    end
                end
            end
            if next(edited) or enabled_changed then save() end
        end))
    end

    local function available(name, definition)
        local player = ctx.player
        if not definition or player:HasTag("busy") or player:HasTag("nopredict") or player:HasTag("sleeping")
            or player.sg and player.sg:HasAnyStateTag("busy", "nopredict", "sleeping", "floating") then return false end
        local data = definition.data or {}
        local rider, inventory = player.replica.rider, player.replica.inventory
        local mounted = rider and rider:IsRiding()
        return not (inventory and inventory:IsHeavyLifting())
            and (not mounted or data.mounted) and (not data.mountonly or mounted)
            and (data.beaver or not player:HasTag("beaver"))
            and (data.moose or not player:HasTag("weremoose"))
            and (data.goose or not player:HasTag("weregoose"))
            and Commands.CanUserAccessCommand(name, player)
            and Commands.CanUserStartCommand(name, player)
    end

    function self:entries()
        local entries = {}
        for name, definition in pairs(definitions) do
            if available(name, definition) then
                entries[#entries + 1] = {
                    name = name,
                    label = label(name),
                    atlas = atlas,
                    texture = "gesture_" .. character .. "_" .. name .. ".tex",
                    animation = pose(definition.data),
                }
            end
        end
        table.sort(entries, function(a, b) return a.name < b.name end)
        return entries
    end

    function self:wheel_entries()
        local catalog = self:entries()
        if not self.enabled or not next(self.favourites) then return catalog, false end
        local result, by_name = {}, {}
        for _, entry in ipairs(catalog) do by_name[entry.name] = entry end
        for i = 1, 8 do
            local name = self.favourites[i]
            result[i] = by_name[name] or { name = name, disabled = true,
                label = self:favourite_label(i) .. (name and ctx:text("（暂不可用）", " (unavailable)") or "") }
        end
        for _, entry in ipairs(catalog) do result[#result + 1] = entry end
        return result, true
    end

    function self:appearance()
        for _, prefab in ipairs(G.DST_CHARACTERLIST) do
            if prefab == ctx.player.prefab then
                local data = ctx.player.userid and G.TheNet:GetClientTableForUser(ctx.player.userid) or {}
                data = data or {}
                local modes, selected = G.GetSkinModes(prefab), G.GetSkinModeFromBuild(ctx.player)
                local mode = modes[1]
                for _, candidate in ipairs(modes) do
                    if candidate.type == selected then mode = candidate; break end
                end
                local curse
                for level = 1, 3 do
                    local tag = "MONKEY_CURSE_" .. level
                    if ctx.player:HasTag(tag) then curse = tag end
                end
                return { prefab = prefab, base = data.base_skin or ctx.player:GetSkinName(),
                    clothes = { body = data.body_skin, hand = data.hand_skin, legs = data.legs_skin, feet = data.feet_skin },
                    mode = mode, curse = curse, build = ctx.player.AnimState:GetBuild() }
            end
        end
    end

    function self:execute(name)
        if not ctx.config.gestures then return false, ctx:text("表情轮盘已关闭", "Emote wheel is disabled") end
        local ready, reason = ctx:ready()
        if not ready then return false, reason end
        if not available(name, definitions[name]) then
            return false, ctx:text("该表情当前不可用", "This emote is not currently available")
        end
        ctx.executor:cancel("replaced")
        Commands.RunUserCommand(name, {}, ctx.player)
        return true
    end
    return self
end

return M
