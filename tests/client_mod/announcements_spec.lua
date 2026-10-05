local T = require("tests.client_mod.support")
-- The engine supplies this C function; native.lua checks its real Unicode behavior.
function string:utf8sub(first, last)
    local chars = {}
    for char in self:gmatch("[%z\1-\127\194-\244][\128-\191]*") do chars[#chars + 1] = char end
    return table.concat(chars, "", first, math.min(last, #chars))
end
local Announcements = require("fieldkit/features/announcements")
local Metrics = require("fieldkit/game/metrics")
T.each("text budget", {
    { name = "controls and percent", text = "25% ready\n\0世界🙂abc", limit = 12, expected = "25% ready  世" },
    { name = "two codepoints", text = "世界🙂", limit = 2, expected = "世界" },
    { name = "emoji boundary", text = "世界🙂abc", limit = 3, expected = "世界🙂" },
    { name = "zero budget", text = "abc", limit = 0, expected = "" },
}, function(row) assert(Announcements.clean(row.text, row.limit) == row.expected) end)
local now, sent, keys = 0, {}, {}
local function item(prefab, size)
    return T.entity({ prefab = prefab, replica = { stackable = { StackSize = function() return size end } } })
end
local axe, berries = item("axe", 1), item("berries", 5)
axe.replica.inventoryitem = { classified = { percentused = { value = function() return 75 end } } }
berries.replica.inventoryitem = { classified = { perish = { value = function() return 31 end } } }
local slots = { berries, item("berries", 7) }
local inv = { classified = {}, GetActiveItem = function() end, GetEquippedItem = function() return axe end,
    GetNumSlots = function() return #slots end, GetItemInSlot = function(_, slot) return slots[slot] end }
local player = { replica = { inventory = inv }, components = {}, IsValid = function() return true end,
    Transform = { GetWorldPosition = function() return 2.4, 0, -3.5 end } }
local hud, active_screen = {}, nil
player.HUD = hud
local snapshot = {
    own = { health = { max = 150 }, hunger = { value = 12.4, max = 100 }, sanity = {},
        temperature = { value = 23.4 }, moisture = { value = 5, max = 100 } },
    world = { day = 12, season = "autumn", season_day = 4, remaining_days = 8 },
    nightmare = { phase = "wild", progress = .5, remaining = 90, quality = "estimated" },
    ride = { observed = 15, remaining_min = 20, remaining_max = 50, quality = "estimated" },
}
for key, value in pairs(snapshot.own) do
    local definition = Metrics.definition(key)
    value.label, value.unit = definition.label, definition.unit
end
local ctx = { player = player, hud = hud, scope = {}, config = { announcements = true },
    params = require("fieldkit/core/parameters").new(),
    text = function(_, _, en) return en end, information = { snapshot = function() return snapshot end },
    G = { GetTime = function() return 0 end, GetStaticTime = function() return now end, ThePlayer = player, KEY_CTRL = 1,
        STRINGS = { NAMES = { TWIGS = "Twigs", ROPE = "Rope" } }, EQUIPSLOTS = { HANDS = "hands" },
        TheInput = { IsKeyDown = function(_, key) return keys[key] end },
        TheFrontEnd = { GetActiveScreen = function() return active_screen or hud end },
        TheNet = { Say = function(_, message, nearby) sent[#sent + 1] = { message = message, nearby = nearby } end },
    },
}
local announcements = Announcements.new(ctx)
assert(announcements:status() and sent[1].message == "Health ?/150 · Hunger 12/100 · Sanity ?")
assert(not announcements:position() and #sent == 1)
local function expect(call, text, nearby)
    T.case(text, function()
        now = now + 5
        assert(call())
        assert(sent[#sent].message == text, sent[#sent].message)
        assert(sent[#sent].nearby == (nearby == true))
    end)
end
expect(function() return announcements:position() end, "I am at (2, -4)")
expect(function() return announcements:world() end, "Day 12 · Autumn day 4 · season days left 8")
expect(function() return announcements:equipment() end, "axe ×1 · Carrying 1 · Durability 75%")
expect(function() return announcements:item(berries) end, "berries ×5 · Carrying 12 · Freshness 50%")
expect(function() return announcements:metric("temperature") end, "Body temperature 23°")
snapshot.own.mightiness = { label = Metrics.definition("mightiness").label, value = 145, max = 150, quality = "known" }
expect(function() return announcements:metric("mightiness") end, "Mightiness 145/150")
snapshot.own.pet_health = { label = Metrics.definition("pet_health").label, value = 620, max = 700,
    bonus = { value = 100, max = 200 }, quality = "known" }
expect(function() return announcements:metric("pet_health") end, "Companion health 620/700 · bonus max health 100/200")
snapshot.own.boat_health = { label = Metrics.definition("boat_health").label, quality = "unknown" }
local before_unknown = #sent
assert(not announcements:metric("boat_health") and not announcements:metric("wereness")
    and #sent == before_unknown, "unavailable states cannot become an invented zero or send a stale value")
expect(function() return announcements:nightmare() end, "Nightmare: Wild 50% · about 90s")
expect(function() return announcements:ride() end, "Riding 15s · estimated remaining ≈20–50s")
snapshot.world.next_season = "winter"
expect(function() return announcements:world() end, "Day 12 · Autumn day 4 · season days left 8 · next season Winter")
snapshot.world.temperature = -10
expect(function() return announcements:metric("temperature") end, "Body temperature 23°")
expect(function() return announcements:world_temperature() end, "Air temperature -10°")
snapshot.world.temperature = nil
expect(function() return announcements:world_temperature() end, "Air temperature ?")
snapshot.world.moonphase, snapshot.world.waxing = "threequarter", true
snapshot.world.fullmoon = { quality = "estimated", min_day = 14, max_day = 16 }
expect(function() return announcements:moon() end, "Moon: Gibbous (waxing) · estimated full moon day 14–16")
snapshot.world.fullmoon.max_day = 14
expect(function() return announcements:moon() end, "Moon: Gibbous (waxing) · estimated full moon day 14")
snapshot.world.fullmoon.quality = "unknown"
expect(function() return announcements:moon() end, "Moon: Gibbous (waxing) · full moon day unknown")
local unobserved = #sent
assert(not announcements:naughtiness() and #sent == unobserved, "missing server observations cannot become a guessed value")
snapshot.naughtiness = { actions = 7, threshold = 31 }
expect(function() return announcements:naughtiness() end, "Naughtiness 7/31")
ctx.params.announcement_channel = "local"
expect(function() return announcements:metric("moisture") end, "Wetness 5/100", true)
keys[1] = true
expect(function() return announcements:metric("moisture") end, "Wetness 5/100", false)
ctx.params.announcement_channel = "global"
expect(function() return announcements:metric("moisture") end, "Wetness 5/100", true)
keys[1] = false
do
    local function ground(prefab, size, x)
        local e = item(prefab, size)
        e.GUID, e.x = x, x
        e.entity = { IsVisible = function() return true end }
        e.Transform = { GetWorldPosition = function() return x, 0, 0 end }
        e.HasTag = function() return false end
        e.replica.inventoryitem = { IsHeld = function() return e.held end }
        return e
    end
    local a, b, held, hidden = ground("berries", 5, 3), ground("berries", 7, 4), ground("berries", 10, 5), ground("berries", 20, 6)
    held.held, hidden.hidden = true, true
    player.GetDistanceSqToInst = function(_, entity) return entity.x ^ 2 end
    ctx.G.CanEntitySeeTarget = function(_, entity) return not entity.hidden end
    ctx.G.TheSim = { FindEntities = function() return { a, b, held, hidden, ground("twigs", 40, 2) } end }
    expect(function() return announcements:target(a) end, "Within 20m: berries ×12 · target 3m")
    b.replica.stackable.GetPreviewStackSize = function() return 10 end
    expect(function() return announcements:target(a) end, "Within 20m: berries ×≥6 · target 3m")
    assert(not announcements:target(hidden), "unseen targets cannot produce announcements")
end

ctx.G.CHARACTER_INGREDIENT = { HEALTH = "health", MAX_HEALTH = "max_health", SANITY = "sanity", MAX_SANITY = "max_sanity", OLDAGE = "oldage" }
player.HasTag = function() return false end
player.replica.health = { classified = {} }
local recipe = { name = "rope", product = "rope", level = { SCIENCE = 1 },
    ingredients = { { type = "twigs", amount = 3 } }, character_ingredients = {}, tech_ingredients = {} }
ctx.G.GetValidRecipe = function(name) return name == "rope" and recipe or nil end
ctx.G.RoundBiasedUp = function(value) return math.floor(value + .5) end
ctx.G.CanPrototypeRecipe = function() return false end
local builder = { classified = {}, CanLearn = function() return true end, IsBuildBuffered = function() return false end,
    IsFreeBuildMode = function() return false end, KnowsRecipe = function() return false end,
    GetTechTrees = function() return {} end, IngredientMod = function() return .5 end,
    HasCharacterIngredient = function() return false, 5 end, HasTechIngredient = function() return false, 1 end }
player.replica.builder = builder
inv.Has = function(_, prefab, amount, containers)
    assert(prefab == "twigs" and amount == 2 and containers == true, "native rounding, modifier and accessible containers")
    return false, 1
end
expect(function() return announcements:recipe("rope") end, "Rope · Technology required · Missing: Twigs ×1")
expect(function() return announcements:ingredient({ recipe = "rope", category = "item", index = 1 }) end,
    "Rope · Twigs 1/2 · Missing ×1")
ctx.G.CanPrototypeRecipe = function() return true end
expect(function() return announcements:recipe(recipe) end, "Rope · Can prototype · Missing: Twigs ×1")
builder.KnowsRecipe = function() return true end
recipe.character_ingredients = { { type = "health", amount = 10 } }
recipe.tech_ingredients = { { type = "sculpting_material", amount = 2 } }
expect(function() return announcements:recipe(recipe) end,
    "Rope · Technology available · Missing: Twigs ×1, health insufficient, sculpting_material 2")
expect(function() return announcements:ingredient({ recipe = "rope", category = "character", index = 1 }) end,
    "Rope · health 5 · needs >10 · Insufficient")
expect(function() return announcements:ingredient({ recipe = "rope", category = "tech", index = 1 }) end,
    "Rope · sculpting_material level 1 · needs ≥2 · Insufficient")
builder.IsBuildBuffered = function() return true end
inv.Has = function() error("buffered recipe must not report already spent materials") end
expect(function() return announcements:recipe(recipe) end, "Rope · Already buffered")
builder.CanLearn = function() return false end
inv.Has = function() return true, 2 end
recipe.character_ingredients, recipe.tech_ingredients = {}, {}
expect(function() return announcements:recipe(recipe) end, "Rope · Unavailable for this character or skill · Ingredients ready")

-- Exercise the current game replica's actual recipe, buffer and multiplier APIs.
do
    local function net(value) return { value = function() return value end } end
    local native_globals = { GetValidRecipe = ctx.G.GetValidRecipe, INGREDIENT_MOD_LOOKUP = { [1] = .5 },
        Class = T.Class,
        require = function() return { AVAILABLE_TECH = { "SCIENCE" }, AVAILABLE_TECH_BONUS_CLASSIFIED = {},
            AVAILABLE_TECH_TEMPBONUS_CLASSIFIED = {} } end,
    }
    local env = setmetatable(native_globals, { __index = _G })
    local NativeBuilder = T.native("components/builder_replica", env)
    local classified = { isfreebuildmode = net(false), ingredientmod = net(1), recipes = {}, bufferedbuilds = {},
        _bufferedbuildspreview = {} }
    player.replica.builder = setmetatable({ inst = player, classified = classified }, NativeBuilder)
    ctx.G.CanPrototypeRecipe = function() return false end
    inv.Has = function(_, _, required, all) assert(required == 2 and all); return false, 1 end
    expect(function() return announcements:recipe(recipe) end, "Rope · Technology required · Missing: Twigs ×1")
    classified.bufferedbuilds.rope = net(true)
    expect(function() return announcements:recipe(recipe) end, "Rope · Already buffered")
end
-- The cookbook is paused; chat uses wall-clock UI time and only its exact owned screen.
package.loaded.cooking = { cookbook_recipes = { cookbook = {
    soup = { health = 12.34, hunger = 75, sanity = -5, temperature = 10 },
} } }
ctx.G.STRINGS.UI = { COOKBOOK = { FOOD_EFFECTS_HOT_FOOD = "Warm", FOOD_EFFECTS_COLD_FOOD = "Cold", FOOD_EFFECTS_NONE = "None" } }
ctx.G.TheCookbook = { IsUnlocked = function() return { has_eaten = true } end }
local cookbook = { owner = player, fieldkit_announcement_context = ctx, inst = { IsValid = function() return true end } }
active_screen = cookbook
expect(function() return announcements:food({ category = "cookbook", prefab = "soup" }, cookbook) end,
    "soup · Recipe base values · Health +12.3 · Hunger +75 · Sanity -5 · Warm")
assert(not announcements:food({ category = "cookbook", prefab = "soup" }, cookbook), "paused cookbook still enforces chat cooldown")
now = now + 5
assert(not announcements:food({ category = "cookbook", prefab = "soup" }), "HUD route cannot claim an arbitrary open popup")
active_screen = {}
assert(not announcements:food({ category = "cookbook", prefab = "soup" }, cookbook), "inactive cookbook cannot send")
active_screen = cookbook
cookbook.owner = {}
assert(not announcements:food({ category = "cookbook", prefab = "soup" }, cookbook), "foreign player cookbook cannot send")
cookbook.owner, cookbook.fieldkit_announcement_context = player, nil
assert(not announcements:food({ category = "cookbook", prefab = "soup" }, cookbook), "unbound popup cannot send")
active_screen = nil
do
    local Data = require("fieldkit/game/announcement_data")
    local original = Data.circuits
    local records = { { bar = 0, index = 1, netid = 7, name = "Circuit chip", slots = 2, powered = false } }
    Data.circuits = function() return records end
    local target = { bar = 0, index = 1, netid = 7 }
    expect(function() return announcements:circuit(target) end, "Circuit 1 · Circuit chip · Slots 2 · Unpowered")
    records[1].netid = 8
    local before = #sent
    now = now + 5
    assert(not announcements:circuit(target) and #sent == before, "changed chip identities cannot announce a replacement")
    assert(not announcements:circuit({ bar = 0, index = 1 }) and #sent == before, "busy widget targets cannot send")
    Data.circuits = original
end
local count = #sent
ctx.G.TheFrontEnd.textProcessorWidget = {}
now = now + 5
assert(not announcements:status() and #sent == count, "chat focus must not send")
ctx.G.TheFrontEnd.textProcessorWidget = nil
ctx.config.announcements = false
assert(not announcements:status() and #sent == count)
ctx.config.announcements = true
ctx.scope.closed = true
assert(not announcements:status() and #sent == count)
T.done()
