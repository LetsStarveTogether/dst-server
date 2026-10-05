local T = require("tests.client_mod.support")
T.case("name label lifecycle", function()
    local Presentation = require("fieldkit/features/presentation")
    local Scope = require("fieldkit/core/scope")

    local visible = true
    local created = {}
    local function entity()
        local e = T.entity()
        e.entity = { IsVisible = function() return visible end }
        function e.entity:AddTransform() end
        function e.entity:AddLabel()
            e.Label = {
                SetFont = function() end, SetColour = function() end,
                SetFontSize = function() end, SetWorldOffset = function() end,
                SetText = function(_, text) e.text = text end,
                Enable = function(_, enabled) e.enabled = enabled end,
            }
        end
        function e.entity:SetParent(parent) e.parent = parent end
        return e
    end
    local player = entity()
    player.userid = "local"
    function player:GetDisplayName() return "Wilson" end
    function player:GetDistanceSqToInst() return 0 end
    local tasks = T.tasks(player)
    local function tick() tasks[1].fn() end
    local ctx = { player = player, config = {
        names = true, text_scale = 1,
    }, G = {
        AllPlayers = { player }, DEFAULTFONT = "font",
        TheNet = { GetClientTableForUser = function() return { name = "Nickname" } end },
        CanEntitySeeTarget = function() return visible end,
        CreateEntity = function() local label = entity(); created[#created + 1] = label; return label end,
    } }
    function ctx:text(_, en) return en end
    function ctx:ready() return true end
    ctx.scope = Scope.new(player, error)
    local feature = Presentation.new(ctx)
    assert(#created == 1 and created[1].text == "Nickname" and created[1].parent == player.entity)
    assert(created[1].persists == false and created[1].tags.NOCLICK)
    tick()
    assert(#created == 1, "updates reuse the owned name label")
    T.each("name records", {
        { name = "missing", client = nil, text = "Wilson", colour = { 1, 1, 1 } },
        { name = "invalid", client = { name = false, colour = false }, text = "Wilson", colour = { 1, 1, 1 } },
        { name = "empty", client = { name = "", colour = { 0, .5, 1 } }, text = "Wilson", colour = { 0, .5, 1 } },
        { name = "nonfinite", client = { name = "Nickname", colour = { 0/0, math.huge, -math.huge } },
            text = "Nickname", colour = { 1, 1, 1 } },
        { name = "partial", client = { name = "Nickname", colour = { .2, "red" } }, text = "Nickname", colour = { .2, 1, 1 } },
    }, function(case)
        local name, colour = Presentation.name_record(player, case.client)
        assert(name == case.text)
        for index = 1, 3 do assert(colour[index] == case.colour[index]) end
    end)

    visible = false
    tick()
    assert(created[1].removed, "hidden players lose their local labels")
    ctx.scope:close()
    assert(tasks[1].cancelled and T.listener_count(player) == 0)
    tick()
    assert(#created == 1, "late periodic callbacks cannot recreate labels")
end)
T.case("native seasonal fish labels, visibility and cleanup", function()
    local Presentation = require("fieldkit/features/presentation")
    local tiles = { OCEAN_COASTAL = 1, OCEAN_SWELL = 2, OCEAN_ROUGH = 3, OCEAN_BRINEPOOL = 4, OCEAN_HAZARDOUS = 5, OCEAN_WATERLOG = 6 }
    local seasons = { AUTUMN = 0, WINTER = 1, SPRING = 2, SUMMER = 3 }
    local function copy(value)
        if type(value) ~= "table" then return value end
        local result = {}; for k, v in pairs(value) do result[k] = copy(v) end; return result
    end
    local school = T.eval(assert(T.source("prefabs/oceanfishdef"):match("(local SCHOOL_VERY_COMMON.-)local function SpecialEventSetup"))
        .. "\nreturn SCHOOL_WEIGHTS", { WORLD_TILES = tiles, GROUND = tiles, SEASONS = seasons, deepcopy = copy })
    local fish = Presentation.fish_seasons(school, seasons)
    assert(fish.oceanfish_small_6 == "autumn" and fish.oceanfish_small_7 == "spring"
        and fish.oceanfish_small_8 == "summer" and fish.oceanfish_medium_8_inv == "winter" and not fish.oceanfish_small_1)
    package.loaded["prefabs/oceanfishdef"] = { school = school }
    local entities, labels = {}, {}
    local player = T.entity({ prefab = "wilson" })
    local tasks = T.tasks(player)
    local ctx = { player = player, config = {}, params = { fish_labels = true },
        scope = require("fieldkit/core/scope").new(player, error), G = {
            SEASONS = seasons, DEFAULTFONT = "font", AllPlayers = {},
            CanEntitySeeTarget = function(_, entity) return not entity.dark end,
            TheSim = { FindEntities = function() return entities end, GetScreenPos = function(_, x, _, z) return x, z end,
                GetScreenSize = function() return 100, 100 end },
            CreateEntity = function()
                local label = T.entity()
                label.entity.AddTransform = function() end
                label.entity.SetParent = function(_, parent) label.parent = parent end
                label.entity.AddLabel = function()
                    label.Label = { SetFont = function() end, SetFontSize = function() end, SetWorldOffset = function() end,
                        Enable = function() end, SetText = function(_, value) label.text = value end,
                        SetColour = function(_, ...) label.colour = { ... } end }
                end
                labels[#labels + 1] = label
                return label
            end,
        } }
    function ctx:text(_, en) return en end
    function ctx:ready() return true end
    local fish_entity = T.entity({ prefab = "oceanfish_small_6", x = 30, z = 30, tags = { NOCLICK = true } })
    local hidden = T.entity({ prefab = "oceanfish_small_7", x = 30, z = 30, dark = true })
    local offscreen = T.entity({ prefab = "oceanfish_small_8", x = 130, z = 30 })
    entities = { fish_entity, hidden, offscreen }
    Presentation.new(ctx)
    assert(#labels == 1 and labels[1].text:find("Autumn", 1, true) and labels[1].parent == fish_entity.entity,
        "native swimming fish use NOCLICK, but unseen fish must never receive labels")
    tasks[1].fn(); assert(#labels == 1)
    ctx.params.fish_labels = false; tasks[1].fn(); assert(labels[1].removed)
    ctx.params.fish_labels, entities = true, {}
    for _ = 1, 80 do entities[#entities + 1] = T.entity({ prefab = "oceanfish_small_6", x = 30, z = 30 }) end
    tasks[1].fn(); assert(#labels == 33, "fish label allocation is bounded")
    ctx.scope:close()
    for _, label in ipairs(labels) do assert(label.removed) end
end)
T.done()
