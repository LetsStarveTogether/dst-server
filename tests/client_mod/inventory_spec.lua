local T = require("tests.client_mod.support")
local Fixtures = require("tests.client_mod.support.inventory")
local Inventory = require("fieldkit/game/inventory")

local world = Fixtures.new()
local function item(id, prefab, count, locked)
    return world:item(prefab, count, { GUID = id, locked = locked })
end
local function owner(slots)
    local box = world:container(T.entity(), #slots, { slots = slots })
    function box:TakeActiveItemFromAllOfSlot(index) self.taken = index end
    return box
end

T.case("inventory identities, refill and recipe access", function()
    local active, active_alias = item(1, "seed", 2), item(1, "seed", 2)
    local locked, unlocked, other = item(2, "seed", 4, true), item(3, "seed", 3), item(4, "axe")
    local backpack = owner({ unlocked, other })
    local inv = owner({ active_alias, locked, other })
    function inv:GetActiveItem() return self.active end
    function inv:GetEquippedItem() return self.hands end
    function inv:GetOverflowContainer() return backpack end
    inv.active, inv.hands = active, other
    local player = { replica = { inventory = inv } }
    local ctx = { player = player, G = { EQUIPSLOTS = { HANDS = "hands" } } }

    local entries = Inventory.items(ctx)
    assert(#entries == 3 and entries[1].item == active and entries[2].item == other and entries[3].item == unlocked,
        "cursor, hands, slots and backpack share one GUID-based inventory view")
    assert(Inventory.count(ctx, "seed") == 9, "effect counts include locked items but exclude duplicate slot aliases")
    locked.locked = false
    assert(#Inventory.items(ctx) == 4 and Inventory.count(ctx, "seed") == 9,
        "changing a lock never looks like item consumption")
    locked.locked = true
    unlocked.preview = 2
    assert(Inventory.count(ctx, "seed") == nil, "predicted stack values cannot confirm consumption")
    unlocked.preview = nil
    unlocked.count = 2
    assert(Inventory.count(ctx, "seed") == 8)
    assert(Inventory.equipped(ctx, other) and not Inventory.equipped(ctx, active))
    assert(Inventory.equipped(ctx, item(other.GUID, other.prefab)), "equipped proxy identity follows native GUID")

    inv.active, inv.slots[1] = nil, locked
    inv.slots[2] = item(5, "stone")
    assert(Inventory.refill(ctx, "seed") and backpack.taken == 1 and inv.taken == nil,
        "refill skips locked slots and uses the actual unlocked backpack owner")
    unlocked.removed = true
    backpack.taken = nil
    assert(not Inventory.refill(ctx, "seed") and backpack.taken == nil, "locked supplies are not an available refill")
    inv.active = item(10, "seed")
    assert(Inventory.refill(ctx, "seed") and not Inventory.refill(ctx, "stone"))
    inv.active.removed = true
    assert(not Inventory.refill(ctx, "seed"), "a stale cursor entity cannot claim that refill succeeded")

    local recipe = { name = "axe", level = { SCIENCE = 1 } }
    local known, can_learn, ingredients, science = false, true, true, 1
    local builder = {
        KnowsRecipe = function(_, value) assert(value == recipe); return known end,
        HasIngredients = function(_, value) assert(value == recipe); return ingredients end,
        CanLearn = function(_, name) assert(name == "axe"); return can_learn end,
        GetTechTrees = function() return { SCIENCE = science } end,
    }
    player.replica.builder = builder
    ctx.G.GetValidRecipe = function(name) return name == "axe" and recipe or nil end
    ctx.G.CanPrototypeRecipe = function(level, accessible) return accessible.SCIENCE >= level.SCIENCE end
    assert(Inventory.recipe(ctx, "axe") == recipe, "an unknown recipe can be prototyped at an accessible science station")
    assert(Inventory.recipe(ctx, { name = "axe", level = {} }) == recipe, "caller recipe tables are resolved through the native registry")
    science = 0
    local value, reason = Inventory.recipe(ctx, "axe")
    assert(value == nil and reason == "unavailable")
    science, can_learn = 1, false
    assert(not Inventory.recipe(ctx, "axe"), "character and skill restrictions cannot be bypassed by sufficient technology")
    known = true
    assert(Inventory.recipe(ctx, "axe") == recipe, "already known recipes do not require learning again")
    ingredients = false
    value, reason = Inventory.recipe(ctx, "axe")
    assert(value == nil and reason == "ingredients")
    assert(not Inventory.recipe(ctx, "missing") and not Inventory.recipe(ctx, {}))
    player.replica.builder = nil
    assert(not Inventory.recipe(ctx, "axe"))

    local equipment = item(20, "axe")
    local encoded = 255
    equipment.replica.inventoryitem.classified = { percentused = { value = function() return encoded end } }
    assert(Inventory.usage(equipment) == nil, "255 is the native unknown sentinel, not 255% durability")
    encoded = 73
    assert(Inventory.usage(equipment) == .73)
    encoded = 0
    assert(Inventory.usage(equipment) == 0, "a known empty durability is not unknown")
    T.each("unknown replica durability", { -1, 101, 254, math.huge, 0 / 0, "50" }, function(bad)
        encoded = bad
        assert(Inventory.usage(equipment) == nil)
    end)
    T.each("authoritative durability", { "armor", "finiteuses", "fueled" }, function(name)
        equipment.components = { [name] = { GetPercent = function() return .375 end } }
        assert(Inventory.usage(equipment) == .375, "authoritative native components keep their exact percent")
    end)
    equipment.components.armor = { GetPercent = function() return .125 end }
    assert(Inventory.usage(equipment) == .125, "native usage codec gives armor priority over finite uses and fuel")
    equipment.components.armor.GetPercent = function() return 1.1 end
    assert(Inventory.usage(equipment) == nil, "invalid authoritative values cannot become announcements or receipts")
    equipment.removed = true
    assert(Inventory.usage(equipment) == nil and Inventory.usage(nil) == nil)
end)

-- Exercise dependency selection against the real native HasIngredients and rounding rules.
T.case("native ingredient chains", function()
    local env = { require = function() return {} end, Class = T.Class }
    T.native("mathutil", env)
    local NativeBuilder = T.native("components/builder_replica", env)
    local stock, registry, locked, unknown = {}, {}, {}, {}
    local calls, modifier, science = {}, 1, 1
    local function recipe(name, ingredients)
        local value = { name = name, ingredients = ingredients or {}, character_ingredients = {}, tech_ingredients = {}, level = { SCIENCE = 1 } }
        registry[name] = value
        return value
    end
    local function ing(name, amount) return { type = name, amount = amount } end
    local inv = { Has = function(_, name, amount, include_active)
        assert(include_active == true)
        calls[#calls + 1] = { name = name, amount = amount }
        return (stock[name] or 0) >= amount
    end }
    local player = { components = {}, replica = { inventory = inv } }
    local builder = setmetatable({ inst = player, classified = { isfreebuildmode = { value = function() return false end } } }, NativeBuilder)
    function builder:KnowsRecipe(value) return not locked[value.name] and not unknown[value.name] end
    function builder:CanLearn(name) return not locked[name] end
    function builder:GetTechTrees() return { SCIENCE = science } end
    function builder:IngredientMod() return modifier end
    player.replica.builder = builder
    local G = { GetValidRecipe = function(name) return registry[name] end, RoundBiasedUp = env.RoundBiasedUp,
        CanPrototypeRecipe = function(level, tech) return level.SCIENCE <= tech.SCIENCE end }
    local ctx = { player = player, G = G }
    local boards = recipe("boards", { ing("log", 4) })
    local rope = recipe("rope", { ing("cutgrass", 3) })
    local device = recipe("device", { ing("boards", 2), ing("rope", 1) })
    stock.log, stock.cutgrass = 8, 3
    assert(Inventory.craft_step(ctx, "device") == boards, "first missing native ingredient is made before final product")
    stock.boards, stock.log = 1, 4
    assert(Inventory.craft_step(ctx, device) == boards, "intermediate quantity is re-evaluated for each craft")
    stock.boards = 2
    assert(Inventory.craft_step(ctx, device) == rope)
    stock.rope = 1
    assert(Inventory.craft_step(ctx, device) == device, "final recipe is returned only once native ingredients suffice")
    modifier, stock.boards, stock.rope, stock.cutgrass = .4, 1, 0, 1
    assert(Inventory.craft_step(ctx, device) == rope, "ingredient discounts use native biased rounding, not ceil")
    modifier, stock.boards, stock.rope = 0, 0, 0
    assert(Inventory.craft_step(ctx, device) == boards, "minimum quantity is one even with zero ingredient multiplier")
    modifier = 1

    local crate = recipe("crate", { ing("boards", 1) })
    crate.placer = "crate_placer"
    local alternate = recipe("woodie_boards", { ing("log", 2) })
    alternate.product, boards.forward_ingredients = "boards", { "missing", "woodie_boards" }
    stock.boards, stock.log, locked.boards = 0, 2, true
    assert(Inventory.craft_step(ctx, crate) == alternate, "native forwarded recipes handle character/skill locks")
    locked.boards = nil
    assert(Inventory.craft_step(ctx, crate) == nil, "forwarding does not choose a cheaper alternative to an accessible recipe")
    locked.crate, stock.log = true, 10
    local no, why = Inventory.craft_step(ctx, crate)
    assert(no == nil and why == "unavailable", "an inaccessible final recipe cannot consume intermediate resources")
    locked.crate, unknown.boards, unknown.woodie_boards, science = nil, true, true, 0
    assert(Inventory.craft_step(ctx, crate) == nil, "intermediate prototyping still needs native technology access")
    science = 1
    assert(Inventory.craft_step(ctx, crate) == boards)
    unknown.boards, unknown.woodie_boards = nil, nil

    local transistor = recipe("transistor", { ing("boards", 1) })
    local machine = recipe("machine", { ing("transistor", 1) })
    assert(Inventory.craft_step(ctx, machine) == boards, "small ingredient chains are traversed in order")
    stock.boards = 1
    assert(Inventory.craft_step(ctx, machine) == transistor)
    stock.transistor = 1
    assert(Inventory.craft_step(ctx, machine) == machine)
    local raw_first = recipe("raw_first", { ing("missing_raw", 1), ing("rope", 1) })
    stock.cutgrass = 3
    assert(Inventory.craft_step(ctx, raw_first) == rope, "scan follows native ingredient order to the next craftable missing ingredient")

    local cycle_a = recipe("cycle_a", { ing("cycle_b", 1) })
    recipe("cycle_b", { ing("cycle_a", 1) })
    assert(Inventory.craft_step(ctx, cycle_a) == nil, "cycles terminate without spending resources")
    for index = 1, 8 do recipe("chain" .. index, { ing(index == 8 and "log" or "chain" .. (index + 1), 1) }) end
    assert(Inventory.craft_step(ctx, "chain1") == registry.chain8)
    recipe("chain0", { ing("chain1", 1) })
    assert(Inventory.craft_step(ctx, "chain0") == nil, "traversal has an explicit eight-level ceiling")
    stock.log, stock.cutgrass, stock.boards = 0, 0, 0
    no, why = Inventory.craft_step(ctx, device)
    assert(no == nil and why == "ingredients", "exhausted raw materials report a finite stop")
    assert(Inventory.craft_step(ctx, "missing") == nil and Inventory.craft_step(ctx, {}) == nil)
    assert(#calls > 0)
end)
T.done()
