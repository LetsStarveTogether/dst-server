local T = require("tests.client_mod.support")
local Fixtures = require("tests.client_mod.support.inventory")
local Executor = require("fieldkit/core/executor")

local Rules = require("fieldkit/rules/cooking")
local data = {
    ingredients = { meat = { tags = { meat = 1 } }, meat_cooked = { tags = { meat = 1, precook = 1 } } },
    recipes = {
        cookpot = {
            low = { priority = 0, test = function() return true end },
            alpha = { priority = 2, test = function(cooker, names, tags)
                return cooker == "cookpot" and names.meat_cooked == 1 and tags.meat == 4
            end },
            beta = { priority = 2, test = function() return true end },
        },
        portablecookpot = { special = { priority = 10, test = function(cooker) return cooker == "portablecookpot" end } },
    },
}
package.preload.cooking = function() return data end
local Cooking = require("fieldkit/features/cooking")
local Scope = require("fieldkit/core/scope")
local saved_recipe = { "meat", "meat", "meat", "meat" }

local random = math.random
math.random = function() error("Preview must not select a random recipe") end
local names, uncertain = Rules.preview(data, "cookpot", { "cook ed", "meat", "meat", "meat" })
assert(#names == 1 and names[1] == "beta" and not uncertain)
names, uncertain = Rules.preview(data, "cookpot", { "cookedmeat", "meat", "meat", "meat" })
assert(#names == 2 and names[1] == "alpha" and names[2] == "beta" and not uncertain)
data.recipes.cookpot.broken = { priority = 100, test = function() error("custom rule failure") end }
names, uncertain = Rules.preview(data, "cookpot", saved_recipe)
assert(names[1] == "beta" and uncertain)
data.recipes.cookpot.broken = nil
names = Rules.preview(data, "portablecookpot", saved_recipe)
assert(#names == 1 and names[1] == "special")
math.random = random
assert(Rules.valid_recipe("cookpot", saved_recipe))
assert(not Rules.valid_recipe("cookpot", { [1] = "meat", [2] = "meat", [4] = "meat" }))
assert(not Rules.valid_recipe("cookpot", { "meat", "meat", "meat", "meat", "meat" }))
assert(not Rules.valid_recipe("custompot", saved_recipe))
assert(Rules.valid_recipe("portablespicer", { "meatballs", "spice_chili" }))
assert(not Rules.compatible(data, "cookpot", "portablecookpot", saved_recipe),
    "a cooker that changes the highest-priority dish cannot join a batch")
assert(not Rules.compatible(data, "cookpot", "portablespicer", saved_recipe))
assert(not Rules.compatible(data, "cookpot", "archive_cookpot", saved_recipe))

local objects = Fixtures.new()
local function entity(prefab)
    objects.nextguid = objects.nextguid + 1
    return T.entity({ GUID = objects.nextguid, prefab = prefab })
end
local function item(prefab, count) return objects:item(prefab, count) end

local function setup(amount, prefab)
    local ctx = Fixtures.new()
    ctx.G = { STRINGS = { NAMES = {} }, KEY_CTRL = 1, KEY_SHIFT = 2 }
    ctx.config, ctx.params = { cooking = true }, require("fieldkit/core/parameters").new()
    ctx.moves, ctx.action_calls, ctx.pots = 0, {}, {}
    function ctx:item(prefab, count, fields) return objects:item(prefab, count, fields) end
    local player, pot = entity("wilson"), entity(prefab or "cookpot")
    ctx.player, ctx.pot, pot.x, ctx.pots[1] = player, pot, 1, pot
    local inv = ctx:inventory(player, 15)
    inv.slots[1] = item("meat", amount or 4)
    local function cooker(vessel, open)
        return ctx:container(vessel, Rules.slots[vessel.prefab] or 4, { open = open, stacks = false, type = "cooker",
            accept = function(self, value, slot)
                return (self.inst.prefab == "portablespicer" and (slot == nil and (value.prefab == "meatballs" or value.prefab == "spice_chili")
                    or value.prefab == (slot == 1 and "meatballs" or "spice_chili"))
                    or self.inst.prefab ~= "portablespicer" and value.prefab == "meat") and (not slot or slot <= self.size)
            end,
        })
    end
    local box = cooker(pot, true)
    ctx.open = { [pot] = true }
    function inv:GetOpenContainers() return ctx.open end
    local request = ctx.request
    function ctx:request(fn, owners)
        self.moves = self.moves + 1
        return request(self, fn, owners)
    end
    box.widget = { buttoninfo = {
        validfn = function(target) return not target.replica.container.reject end,
        fn = function(target, doer)
            assert(doer == player)
            ctx.cooked = (ctx.cooked or 0) + 1
            if not ctx.reject_cook then
                local container = target.replica.container
                container.slots, container.open, container.unavailable = {}, false, true
                target.finish_at, ctx.open[target] = ctx.now + 3, nil
            end
        end,
    } }
    function box:GetWidget() return self.widget end
    ctx.store = { ready = true, recipes = { [pot.prefab] = saved_recipe } }
    function ctx.store:get(key) return self[key] end
    function ctx.store:set(key, value) self[key] = value; return true end
    function ctx:ready() return not self.blocked, self.blocked and "focus blocked" end
    ctx.G.TheInput = {
        IsKeyDown = function(_, key) return ctx.keys and ctx.keys[key] or false end,
        GetHUDEntityUnderMouse = function() return ctx.hover_widget and { widget = ctx.hover_widget } end,
        GetWorldEntityUnderMouse = function() return ctx.hover_target end,
    }
    function ctx:text(_, en) return en end
    function ctx:notify(message) self.messages[#self.messages + 1], self.result = message, message end
    ctx.tasks = T.tasks(player)
    local controller = { ismastersim = true, IsEnabled = function() return true end, IsBusy = function() return ctx.actor_busy end }
    function controller:DoAction(action)
        ctx.action_calls[#ctx.action_calls + 1] = { id = action.action.id, target = action.target }
        ctx.pending_action, player.bufferedaction = action, action
    end
    function controller:ClearActionHold() end
    player.components.playercontroller = controller
    ctx.G.ACTIONS = { WALKTO = {} }
    local function candidates(_, _, target)
        local id = target:HasTag("donecooking") and "HARVEST" or target.replica.container:CanBeOpened() and "RUMMAGE"
        if not id then return {} end
        local action = { doer = player, target = target, action = { id = id }, rotation = 0,
            IsValid = function() return target:IsValid() end }
        function action:AddSuccessAction(fn) self.success = fn end
        function action:AddFailAction(fn) self.failure = fn end
        function action:GetActionPoint() return self.target:GetPosition() end
        return { action }
    end
    player.components.playeractionpicker = { GetLeftClickActions = candidates, GetRightClickActions = candidates }
    ctx.G.GetTime = function() return ctx.now end
    ctx.G.CanEntitySeeTarget = function() return true end
    ctx.G.TheSim = { FindEntities = function(_, x, y, z, radius)
        local found = {}
        for _, vessel in ipairs(ctx.pots) do
            if vessel:GetDistanceSq({ x = x, y = y, z = z }) <= radius ^ 2 then found[#found + 1] = vessel end
        end
        return found
    end }
    ctx.scope = Scope.new(player, error)
    ctx.executor = Executor.new(function() return ctx.now end, function() return ctx:ready() end,
        function(message) ctx:notify(message) end, error)
    local start = ctx.executor.start
    function ctx.executor:start(job)
        local cancel = job.cancel
        function job:cancel(reason) ctx.status = reason; if cancel then cancel(self, reason) end end
        return start(self, job)
    end
    function ctx:tick(now)
        self.now = now
        if self.auto_finish then
            for _, vessel in ipairs(self.pots) do
                if vessel.finish_at and now >= vessel.finish_at then vessel.tags.donecooking, vessel.finish_at = true, nil end
            end
        end
        self.executor:tick()
        return self.executor:is_running() and "running" or self.status, self.result
    end
    function ctx:deliver_action()
        local action = self.pending_action
        local vessel, container = action.target, action.target.replica.container
        player.x, player.z = vessel.x, vessel.z
        if action.action.id == "RUMMAGE" then container.open, self.open[vessel] = true, true
        else vessel.tags.donecooking, container.unavailable = false, false end
        action.success()
        self.pending_action, player.bufferedaction = nil, nil
        if self.after_action then self:after_action() end
    end
    function ctx:add_pot(x, prefab)
        local vessel = entity(prefab or pot.prefab)
        vessel.x = x
        local container = cooker(vessel, false)
        container.widget, container.GetWidget = box.widget, box.GetWidget
        self.pots[#self.pots + 1] = vessel
        return vessel, container
    end
    ctx.feature = Cooking.new(ctx)
    return ctx, inv, box
end

T.case("fill, confirmations, memory and source isolation", function()
    local ctx, inv, box = setup(3)
    assert(not ctx.feature:fill() and ctx.moves == 0)
    ctx = setup()
    assert(ctx.feature:fill())
    assert(not ctx.feature:fill() and ctx.moves == 0)
    for step = 1, 4 do
        assert(ctx:tick(step) == "running" and ctx.moves == step)
        assert(ctx:tick(step + 0.1) == "running" and ctx.moves == step)
        ctx:deliver()
        assert(ctx:tick(step + 0.2) == (step == 4 and "done" or "running"))
    end
    assert(ctx.moves == 4 and not ctx.cooked)
    local notifications = #ctx.messages
    local remembered, message = ctx.feature:remember()
    assert(remembered and message == "Recipe remembered for this cooking container" and #ctx.messages == notifications)
    local previewed, candidates = ctx.feature:preview()
    assert(previewed and candidates == "Highest-priority candidates: beta" and #ctx.messages == notifications)
    assert(Rules.valid_recipe("cookpot", ctx.store.recipes.cookpot))
    assert(ctx.feature:cook())
    assert(ctx:tick(5) == "running" and ctx.cooked == 1)
    assert(ctx:tick(5.1) == "done", "sending the button request is not itself confirmation")

    ctx, inv, box = setup()
    assert(ctx.feature:fill())
    ctx:tick(0)
    ctx.pending() -- Predicted contents alone must not advance while the native replicas are busy.
    assert(ctx:tick(0.1) == "running" and ctx.moves == 1)
    inv.busy, box.busy = false, false
    assert(ctx:tick(0.2) == "running")
    inv.slots[1] = item("meat", 3)
    assert(ctx:tick(0.3) == "failed" and ctx.moves == 1)

    ctx, inv, box = setup()
    assert(ctx.feature:fill())
    ctx:tick(0)
    inv.busy, box.busy = false, false -- Native prediction timeout is not a successful move.
    assert(ctx:tick(2.1) == "running")
    assert(ctx:tick(6) == "failed" and ctx.moves == 1)

    ctx, inv, box = setup()
    assert(ctx.feature:fill())
    ctx:tick(0)
    box.open = false
    assert(ctx:tick(0.1) == "failed" and ctx.moves == 1)
    T.each("fill preconditions", {
        { name = "cursor", prepare = function(c, inventory) inventory.active = item("meat") end },
        { name = "unloaded memory", prepare = function(c) c.store.ready = false end },
        { name = "incomplete recipe", prepare = function(c) c.store.recipes.cookpot = { "meat" } end },
        { name = "stacking vessel", prepare = function(c, inventory, container) container.stacks = true end },
        { name = "closed vessel", prepare = function(c, inventory, container) container.open = false end },
        { name = "read-only vessel", prepare = function(c, inventory, container) container.readonly = true end },
        { name = "busy inventory", prepare = function(c, inventory) inventory.busy = true end },
        { name = "busy vessel", prepare = function(c, inventory, container) container.busy = true end },
    }, function(row)
        local c, inventory, container = setup()
        row.prepare(c, inventory, container)
        assert(not c.feature:fill() and c.moves == 0)
        if not c.store.ready then assert(not c.feature:remember()) end
    end)
    ctx, inv, box = setup()
    local old = ctx.store.recipes
    box.slots[1] = item("meat")
    assert(not ctx.feature:remember() and ctx.store.recipes == old)
    ctx, inv, box = setup()
    for slot = 1, 4 do box.slots[slot] = item("meat") end
    assert(ctx.feature:cook())
    inv.active = item("meat")
    assert(ctx:tick(0) == "failed" and not ctx.cooked)

    ctx, inv, box = setup(1, "portablespicer")
    ctx.store.recipes.portablespicer = { "meatballs", "spice_chili" }
    inv.slots = { item("meatballs", 1), item("spice_chili", 1) }
    assert(ctx.feature:fill())
    for step = 1, 2 do
        assert(ctx:tick(step) == "running")
        ctx:deliver()
        assert(ctx:tick(step + 0.1) == (step == 2 and "done" or "running"))
    end
    assert(box.slots[1].prefab == "meatballs" and box.slots[2].prefab == "spice_chili")
    ctx, inv, box = setup(1, "portablespicer")
    ctx.store.recipes.portablespicer = { "spice_chili", "meatballs" }
    inv.slots = { item("meatballs"), item("spice_chili") }
    assert(not ctx.feature:fill() and ctx.moves == 0)

    ctx, inv, box = setup(1)
    local chest = entity("icebox")
    local source = ctx:container(chest, 4, { slots = { item("meat", 4) }, type = "chest" })
    inv.slots = {}
    ctx.open[chest] = true
    function source:MoveItemFromCountOfSlot(slot, target, amount)
        assert(slot == 1 and target == ctx.pot and amount == 1)
        ctx.moves = ctx.moves + 1
    end
    assert(ctx.feature:fill())
    assert(ctx:tick(0) == "running" and ctx.moves == 1)
    source.open = false
    assert(ctx:tick(0.1) == "failed" and ctx.moves == 1)
    source.open, source.type = true, "cooker"
    assert(not ctx.feature:fill() and ctx.moves == 1) -- Never take another pot's prepared ingredients.
end)

local function drive(context, budget)
    for _ = 1, budget or 180 do
        local status = context:tick(context.now + 1)
        if context.pending then context:deliver() end
        if context.pending_action then context:deliver_action() end
        if status ~= "running" then return status end
    end
    error("test operation failed to finish within its budget")
end

T.each("selected pots survive automatic walking", { "harvest", "batch" }, function(mode)
    T.each("remaining target", { "visible", "hidden", "removed" }, function(state)
        local ctx, _, box = setup(8)
        ctx.pot.x, box.open, ctx.open, ctx.params.search_radius = -15, false, {}, 20
        local second, second_box = ctx:add_pot(15)
        local outside, outside_box = ctx:add_pot(25)
        ctx.params.cooking_harvest = false
        if mode == "harvest" then
            for _, vessel in ipairs(ctx.pots) do
                vessel.tags.donecooking, vessel.replica.container.unavailable = true, true
            end
        end
        assert(ctx.feature[mode](ctx.feature, ctx.pot, 2))
        outside.x, ctx.params.search_radius = 1, .1
        if state == "hidden" then second.hidden = true
        elseif state == "removed" then second:Remove() end
        assert(drive(ctx) == "done")
        local expected = state == "visible" and 2 or 1
        assert(#ctx.action_calls == expected, "walking must retain selected visible pots while excluding unavailable targets")
        assert(ctx.action_calls[1].target == ctx.pot and ctx.player.x == (expected == 2 and 15 or -15))
        if expected == 2 then assert(ctx.action_calls[2].target == second) end
        assert(outside_box:IsEmpty() and not outside.finish_at, "new arrivals cannot join an already selected queue")
        if mode == "harvest" then
            assert(ctx.result:find("harvests: " .. expected) and outside.tags.donecooking)
        else assert(ctx.cooked == expected and (expected == 2 or second_box:IsEmpty())) end
    end)
end)

T.each("automatic pots use current distance", { "harvest", "batch" }, function(mode)
    local ctx, _, box = setup(12)
    ctx.pot.x, box.open, ctx.open, ctx.params.cooking_harvest = -5, false, {}, false
    local second = ctx:add_pot(6)
    local third = ctx:add_pot(mode == "harvest" and -7 or 7)
    if mode == "harvest" then
        for _, vessel in ipairs(ctx.pots) do vessel.tags.donecooking, vessel.replica.container.unavailable = true, true end
    end
    assert(ctx.feature[mode](ctx.feature, second, 3) and drive(ctx) == "done")
    local expected = mode == "harvest" and { ctx.pot, third, second } or { second, third, ctx.pot }
    assert(#ctx.action_calls == #expected)
    for i, vessel in ipairs(expected) do
        assert(ctx.action_calls[i].target == vessel, "an explicit first pot takes priority, then each choice uses the current player position")
    end
end)

T.each("unneeded ingredient sources", {
    { name = "empty auto-closed chest" },
    { name = "unrelated auto-closed chest", prefab = "flint" },
    { name = "redundant auto-closed food", prefab = "meat" },
    { name = "busy unrelated chest", prefab = "flint", busy = true },
    { name = "busy redundant food", prefab = "meat", busy = true },
}, function(row)
    local ctx, _, box = setup(4)
    ctx.pot.x, box.open, ctx.open, ctx.auto_finish = 5, false, {}, true
    local chest = entity("icebox")
    local source = ctx:container(chest, 4, { type = "chest", busy = row.busy,
        slots = row.prefab and { item(row.prefab, 4) } or {} })
    ctx.open[chest] = true
    local NativeContainer = T.native("components/container", {
        Class = T.Class, require = function() return {} end,
        CONTAINER_AUTOCLOSE_DISTANCE = 3, CanEntitySeeTarget = function() return true end,
    })
    chest.entity.GetParent = function() end
    source.opencount, source.openlist = 1, { [ctx.player] = true }
    function ctx.player:IsNear(target, distance) return self:GetDistanceSqToInst(target) <= distance ^ 2 end
    function source:Close()
        self.open, self.opencount, self.openlist, ctx.open[chest] = false, 0, {}, nil
        self.GetItemInSlot = function() error("a closed unused source must not be read") end
    end
    function ctx:after_action() if source.open then NativeContainer.OnUpdate(source, 0) end end
    assert(ctx.feature:batch(ctx.pot, 1), "an unused source cannot block a complete recipe already in inventory")
    assert(drive(ctx) == "done" and ctx.cooked == 1 and ctx.moves == 4 and not source.open)
    assert(ctx.result:find("harvests: 1"), "an unrelated automatic close cannot interrupt cooking or harvest")
end)

T.each("ingredient dependencies end after confirmed consumption", {
    { name = "pending transfer", delivered = 0, expected = "failed" },
    { name = "partial recipe", delivered = 1, expected = "failed" },
    { name = "consumed first source", delivered = 1, stock = 1, expected = "done" },
    { name = "completed recipe", delivered = 4, expected = "done" },
}, function(row)
    local ctx, inv, box = setup()
    inv.slots = {}
    local chest = entity("icebox")
    local source = ctx:container(chest, 4, { type = "chest", slots = { item("meat", row.stock or 4) } })
    ctx.open[chest] = true
    if row.stock then
        local reserve = entity("icebox")
        ctx:container(reserve, 4, { type = "chest", slots = { item("meat", 4 - row.stock) } })
        ctx.open[reserve] = true
    end
    assert(ctx.feature:repeat_once())
    for i = 1, row.delivered do
        assert(ctx:tick(i) == "running")
        ctx:deliver()
        assert(ctx:tick(i + .1) == "running")
    end
    if row.delivered == 0 then assert(ctx:tick(0) == "running" and ctx.moves == 1) end
    source.open, ctx.open[chest] = false, nil
    source.GetItemInSlot = function() error("closed ingredient storage must never be read") end
    assert(drive(ctx) == row.expected)
    if row.expected == "failed" then
        assert(ctx.moves == math.max(1, row.delivered) and not ctx.cooked and box.slots[1],
            "closing a required source preserves transferred food and never continues filling")
    else assert(ctx.moves == 4 and ctx.cooked == 1, "confirmed ingredients no longer depend on their former container") end
end)

T.each("unconfirmed source stock", { "settles", "timeout" }, function(outcome)
    local ctx, inv = setup()
    inv.slots = {}
    local chest = entity("icebox")
    local source = ctx:container(chest, 4, { type = "chest", slots = { item("meat", 4) } })
    ctx.open[chest], ctx.params.cooking_harvest = true, false
    assert(ctx.feature:batch(ctx.pot, 1))
    source.busy = true
    assert(ctx:tick(0) == "running" and ctx:tick(.1) == "running" and ctx.moves == 0)
    assert(ctx:tick(14) == "running" and ctx.moves == 0, "pending source stock cannot be classified as exhausted")
    if outcome == "settles" then
        source.busy = false
        assert(drive(ctx) == "done" and ctx.cooked == 1 and ctx.moves == 4)
    else assert(ctx:tick(15) == "failed" and ctx.moves == 0 and not ctx.cooked) end
end)

T.each("predicted ingredient counts", { "inventory", "backpack", "chest" }, function(kind)
    T.each("replication", { "settles", "timeout" }, function(outcome)
        local ctx, inv = setup()
        inv.slots = {}
        local source = inv
        if kind ~= "inventory" then
            local holder = entity(kind == "backpack" and "backpack" or "icebox")
            source = ctx:container(holder, 4, { type = "chest" })
            if kind == "backpack" then inv.overflow = source else ctx.open[holder] = true end
        end
        local ingredient = item("meat", 4)
        source.slots[1] = ingredient
        local stack = ingredient.replica.stackable
        stack.GetPreviewStackSize = T.eval(assert(T.source("components/stackable_replica")
            :match("function Stackable:GetPreviewStackSize%b().-\nend")) .. "\nreturn Stackable.GetPreviewStackSize", { Stackable = {} })
        ingredient.stackable_preview_context = "cooking"
        stack._previewstacksize = { cooking = 3 }
        ctx.params.cooking_harvest = false
        assert(ctx.feature:batch(ctx.pot, 1))
        assert(ctx:tick(0) == "running" and ctx:tick(.1) == "running")
        assert(ctx:tick(14) == "running" and ctx.moves == 0,
            "a native stack prediction is pending stock even when its container is idle")
        if outcome == "settles" then
            stack._previewstacksize = nil
            assert(drive(ctx) == "done" and ctx.cooked == 1 and ctx.moves == 4)
        else assert(ctx:tick(15) == "failed" and ctx.moves == 0 and not ctx.cooked) end
    end)
end)

T.case("one dish and finite batch", function()
    local ctx, inv, box
    -- One dish and multiple pots share the same confirmed fill/fire stages.
    ctx = setup()
    assert(ctx.feature:repeat_once())
    assert(drive(ctx) == "done" and ctx.moves == 4 and ctx.cooked == 1)
    ctx, inv, box = setup()
    for slot = 1, 4 do box.slots[slot] = item("meat") end
    ctx.reject_cook = true
    assert(ctx.feature:cook())
    assert(ctx:tick(0) == "running" and ctx:tick(1) == "running" and ctx.cooked == 1)
    assert(ctx:tick(6) == "failed" and ctx.cooked == 1, "a rejected widget request is never blindly resubmitted")

    ctx, inv, box = setup(12)
    local second, second_box = ctx:add_pot(3)
    box.open, ctx.open, ctx.auto_finish = false, {}, true
    local foreign = ctx:add_pot(2, "portablespicer")
    assert(ctx.feature:batch(second, 3))
    ctx.params.search_radius, ctx.params.cooking_count = .1, 20
    assert(drive(ctx) == "done" and ctx.cooked == 3 and ctx.moves == 12)
    assert(ctx.action_calls[1].target == second, "the explicitly selected pot takes priority over nearer pots")
    local opens, harvests = 0, 0
    for _, call in ipairs(ctx.action_calls) do
        assert(call.target ~= foreign, "a batch never mixes cookpots with spice stations")
        if call.id == "RUMMAGE" then opens = opens + 1 else harvests = harvests + 1 end
    end
    assert(opens == 3 and harvests == 3, "three recipes use three confirmed opens and three harvests without toggle spam")
    assert(not box.unavailable and not second_box.unavailable, "batch completion drains its in-progress food")

    ctx, inv, box = setup(6)
    second, second_box = ctx:add_pot(2)
    ctx.auto_finish = true
    assert(ctx.feature:batch(ctx.pot, 3) and drive(ctx) == "done")
    assert(ctx.cooked == 1 and ctx.moves == 4 and inv.slots[1].count == 2 and second_box:IsEmpty(),
        "reserve a complete recipe before filling; shortage does not strand half a second pot")
    assert(ctx.result:find("ingredients exhausted") and ctx.result:find("harvests: 1"))

    ctx, inv, box = setup(8)
    second, second_box = ctx:add_pot(2)
    local partial = item("meat")
    second_box.slots[1] = partial
    ctx.auto_finish = true
    assert(ctx.feature:batch(ctx.pot, 2) and drive(ctx) == "done")
    assert(ctx.cooked == 2 and second_box.slots[1] == partial and ctx.result:find("skipped: 1"),
        "another player's partial pot is preserved while an empty pot can be reused")
    second_box.open, ctx.open[second] = true, true
    assert(not ctx.feature:batch(second, 1), "an explicitly selected partial pot requires a clear manual decision")
end)

T.case("batch ingredient sources", function()
    local ctx, inv, box, chest, source
    -- A batch snapshots already open ordinary ingredient containers.
    ctx, inv, box = setup(1)
    inv.slots = {}
    chest = entity("icebox")
    source = ctx:container(chest, 4, { slots = { item("meat", 8) }, type = "chest" })
    ctx.open[chest] = true
    ctx.auto_finish = true
    assert(ctx.feature:batch(ctx.pot, 2) and drive(ctx) == "done" and ctx.moves == 8 and ctx.cooked == 2)
    assert(source.slots[1] == nil and not ctx.pot.tags.donecooking,
        "explicitly open sources support repeated filling and confirmed harvests")
    ctx, inv, box = setup(1)
    inv.slots = {}
    chest = entity("icebox")
    source = ctx:container(chest, 4, { slots = { item("meat", 8) }, type = "chest" })
    ctx.open[chest] = true
    assert(ctx.feature:batch(ctx.pot, 2))
    ctx:tick(0); ctx:tick(.1); ctx:tick(.2); ctx:deliver(); ctx:tick(.3)
    assert(ctx.moves == 1 and box.slots[1])
    source.open = false
    source.GetItemInSlot = function() error("closed ingredient storage must never be read") end
    assert(ctx:tick(.4) == "failed" and ctx.result:find("Ingredient source closed") and ctx.moves == 1
        and box.slots[1], "closing an ingredient source stops with already placed ingredients intact")
end)

T.case("leave completed meals", function()
    local ctx, inv, box, second, second_box
    -- Leave finished meals in place and finish immediately after the requested starts.
    ctx, inv, box = setup(8)
    second, second_box = ctx:add_pot(2)
    local finished, finished_box = ctx:add_pot(3)
    finished.tags.donecooking, finished_box.unavailable = true, true
    ctx.params.cooking_harvest, ctx.auto_finish = false, true
    assert(ctx.feature:batch(ctx.pot, 2))
    ctx.params.cooking_harvest = true
    assert(drive(ctx) == "done" and ctx.cooked == 2 and ctx.moves == 8 and finished.tags.donecooking)
    for _, call in ipairs(ctx.action_calls) do assert(call.id ~= "HARVEST", "harvest choice is captured when the batch starts") end
    ctx, inv, box = setup(4)
    box.unavailable, box.open = true, false
    ctx.params.cooking_harvest = false
    assert(ctx.feature:batch(ctx.pot, 1) and ctx:tick(0) == "done" and not ctx.cooked,
        "fill-only batches skip occupied cookers rather than wait for a meal they cannot remove")
end)

T.case("recipe and character compatibility", function()
    local ctx, inv, box, second, second_box
    -- Mix normal and portable cookpots only when local recipe candidates agree.
    local portable_recipes = data.recipes.portablecookpot
    data.recipes.portablecookpot = { beta = data.recipes.cookpot.beta }
    assert(Rules.compatible(data, "cookpot", "portablecookpot", saved_recipe))
    ctx, inv, box = setup(8)
    second, second_box = ctx:add_pot(2, "portablecookpot")
    second.tags.mastercookware, ctx.player.tags.masterchef, ctx.params.cooking_harvest = true, true, false
    assert(ctx.feature:batch(ctx.pot, 2) and drive(ctx) == "done" and ctx.cooked == 2)
    assert(ctx.store.recipes.portablecookpot and second.finish_at, "compatible cookware keeps its own recipe memory")
    ctx, inv, box = setup(8)
    second, second_box = ctx:add_pot(2, "portablecookpot")
    second.tags.mastercookware, ctx.params.cooking_harvest = true, false
    assert(not ctx.feature:batch(second, 1), "portable cooking retains the native masterchef restriction")
    assert(ctx.feature:batch(ctx.pot, 2) and drive(ctx) == "done" and ctx.cooked == 1 and second_box:IsEmpty())
    ctx, inv, box = setup(8)
    second, second_box = ctx:add_pot(2, "portablecookpot")
    ctx.params.cooking_harvest = false
    assert(ctx.feature:batch(ctx.pot, 2))
    data.recipes.portablecookpot = portable_recipes
    assert(drive(ctx) == "done" and ctx.cooked == 1 and second_box:IsEmpty(),
        "recipe compatibility is rechecked before opening each candidate")
end)

T.case("wait, cancel and externally harvested meal", function()
    local ctx, inv, box, second, second_box
    -- Busy cooking is waited on without repeatedly opening the container.
    ctx, inv, box = setup(4)
    box.unavailable, box.open, ctx.open = true, false, {}
    assert(ctx.feature:batch(ctx.pot, 1))
    assert(ctx:tick(0) == "running" and #ctx.action_calls == 0)
    assert(ctx:tick(239) == "running" and #ctx.action_calls == 0)
    assert(ctx:tick(240) == "failed", "a permanently unavailable pot has a finite waiting budget")

    ctx, inv, box = setup(4)
    box.open, ctx.open = false, {}
    assert(ctx.feature:batch(ctx.pot, 1))
    ctx:tick(0); ctx:tick(.1); ctx:tick(1)
    assert(#ctx.action_calls == 1 and ctx.action_calls[1].id == "RUMMAGE")
    ctx:tick(2)
    assert(#ctx.action_calls == 1, "an in-flight open is not a reason to toggle the pot again")
    local opening = ctx.pending_action
    ctx.executor:cancel("cancelled")
    assert(ctx.player.bufferedaction == nil)
    opening.success()
    assert(ctx.moves == 0 and not ctx.executor:is_running(), "late open acknowledgement cannot resume cancelled filling")

    ctx, inv, box = setup(4)
    box.unavailable, box.open, ctx.pot.tags.donecooking = true, false, true
    second, second_box = ctx:add_pot(3)
    second_box.unavailable, second.tags.donecooking = true, true
    assert(ctx.feature:harvest() and drive(ctx) == "done")
    assert(#ctx.action_calls == 2 and ctx.moves == 0 and not ctx.cooked)
    assert(not ctx.pot.tags.donecooking and not second.tags.donecooking)

    -- Another player harvesting the final dish must not leave a stale pending entry.
    ctx, inv, box = setup(4)
    assert(ctx.feature:batch(ctx.pot, 1))
    for now = 1, 20 do
        ctx:tick(now)
        if ctx.pending then ctx:deliver() end
        if ctx.pending_action then ctx:deliver_action() end
        if ctx.cooked then ctx:tick(now + .1); break end
    end
    box.unavailable, ctx.pot.tags.donecooking = false, false
    assert(ctx:tick(30) == "done", "an observed externally emptied pot releases the batch's wait")
end)

local function harvesting(remote, predicting)
    local ctx, _, box = setup(4)
    box.unavailable, box.open, ctx.pot.tags.donecooking = true, false, true
    local controller = ctx.player.components.playercontroller
    if remote then
        controller.ismastersim = false
        function controller:GetPlatformRelativePosition(x, z) return nil, x, z end
        function controller:RemoteStopWalking() ctx.stopped = true end
        ctx.G.RPC = { LeftClick = 1, RightClick = 2, SetMovementPredictionEnabled = 3 }
        ctx.G.SendRPCToServer = function(rpc) if rpc == 3 then ctx.reset_movement = true end end
        if predicting then
            controller.locomotor = { Stop = function() ctx.local_stop = true end,
                Clear = function(self) self.bufferedaction = nil end }
        end
        local dispatch = controller.DoAction
        function controller:DoAction(action)
            dispatch(self, action)
            if self.locomotor then self.locomotor.bufferedaction = action end
            action.non_preview_cb()
        end
    end
    assert(ctx.feature:harvest())
    local job = ctx.executor.current
    ctx:tick(0); ctx:tick(.1)
    return ctx, box, job, controller
end

T.each("externally harvested action ownership", { "host", "remote", "prediction" }, function(mode)
    local remote, predicting = mode ~= "host", mode == "prediction"
    local ctx, box, job, controller = harvesting(remote, predicting)
    local pending = ctx.pending_action
    box.unavailable, ctx.pot.tags.donecooking = false, false
    ctx:tick(.2)
    if remote then
        assert(ctx.executor:is_running(), "target state can precede the native execution echo")
        ctx.player:PushEvent("performaction") -- An unrelated echo while still walking cannot claim the harvest.
        ctx.player.x = ctx.pot.x
        ctx:tick(1.3)
    end
    ctx:tick(1.4)
    assert(job.harvested == 0 and job.skipped == 1 and not ctx.executor:is_running())
    assert(ctx.player.bufferedaction == nil, "finishing an externally satisfied goal releases its own pending action")
    assert(not predicting or ctx.local_stop and controller.locomotor.bufferedaction == nil)
    assert(not remote or ctx.stopped and ctx.reset_movement == (not predicting and true or nil))
    if pending.success then pending.success() else ctx.player:PushEvent("performaction", { action = pending }) end
    assert(job.harvested == 0 and #ctx.action_calls == 1, "late completion cannot resume an already released harvest")
end)

T.each("remote harvest replication order", { "effect first", "execution first" }, function(order)
    T.each("prediction", { false, true }, function(predicting)
        local ctx, box, job, controller = harvesting(true, predicting)
        ctx.player.x = ctx.pot.x
        if order == "effect first" then
            box.unavailable, ctx.pot.tags.donecooking = false, false
            ctx:tick(.2)
        else
            ctx.player:PushEvent("performaction")
            ctx:tick(.2)
        end
        assert(job.harvested == 0 and ctx.executor:is_running(), "both execution and the cooker result are required")
        box.unavailable, ctx.pot.tags.donecooking = false, false
        ctx.player:PushEvent("performaction")
        ctx.player.bufferedaction = nil
        if controller.locomotor then controller.locomotor.bufferedaction = nil end
        ctx:tick(.3); ctx:tick(.4)
        assert(job.harvested == 1 and job.skipped == 0 and not ctx.executor:is_running())
        assert(not ctx.stopped and not ctx.reset_movement, "confirmed native execution does not send a corrective stop")
    end)
end)

T.case("failed harvest echo cannot confirm an external result", function()
    local ctx, box, job = harvesting(true)
    ctx.player.x = ctx.pot.x
    ctx.player:PushEvent("performaction")
    ctx.player:PushEvent("actionfailed", { action = ctx.pending_action })
    box.unavailable, ctx.pot.tags.donecooking = false, false
    ctx:tick(.2); ctx:tick(.3)
    assert(job.harvested == 0 and job.skipped == 1 and ctx.player.bufferedaction == nil)
end)

T.case("manual memory and native container lifecycle", function()
    local ctx, inv, box
    -- Observe the actual native button instance, retaining all arguments and return values.
    ctx, inv, box = setup(4)
    for slot = 1, 4 do box.slots[slot] = item("meat") end
    ctx.store.recipes = {}
    local widget = { button = {} }
    widget.button.onclick = function(argument)
        assert(argument == "forwarded")
        box.widget.buttoninfo.fn(ctx.pot, ctx.player)
        return "native", nil, 7
    end
    local native_click = widget.button.onclick
    ctx.feature:observe_button(widget, ctx.pot)
    local cleanup_count = #ctx.scope.cleanups
    for _ = 1, 20 do ctx.feature:observe_button({ button = { onclick = function() end } }, ctx.pot) end
    assert(#ctx.scope.cleanups == cleanup_count, "reopening native containers does not retain one scope cleanup per dead button")
    local first, middle, last = widget.button.onclick("forwarded")
    assert(first == "native" and middle == nil and last == 7 and ctx.store.recipes.cookpot == nil)
    ctx.tasks[1].fn()
    assert(Rules.valid_recipe("cookpot", ctx.store.recipes.cookpot), "manual recipe memory follows observed cooking acceptance")
    ctx.scope:close()
    assert(widget.button.onclick == native_click)

    ctx, inv, box = setup(4)
    for slot = 1, 4 do box.slots[slot] = item("meat") end
    ctx.store.recipes, ctx.reject_cook = {}, true
    widget = { button = { onclick = function() box.widget.buttoninfo.fn(ctx.pot, ctx.player) end } }
    ctx.feature:observe_button(widget, ctx.pot)
    widget.button.onclick()
    ctx.now = 9; ctx.tasks[1].fn()
    assert(ctx.store.recipes.cookpot == nil, "failed manual cooking requests never replace remembered recipes")

    local buttons = {}
    package.preload["widgets/imagebutton"] = function()
        return function()
            local button = { kills = 0 }
            function button:SetPosition(x, y) self.x, self.y = x, y end
            function button:SetFont() end
            function button:SetTextSize() end
            function button:SetText(value) self.text = value end
            function button:SetHoverText(value) self.hint = value end
            function button:SetOnClick(fn) self.onclick = fn end
            function button:Kill() self.kills = self.kills + 1 end
            buttons[#buttons + 1] = button
            return button
        end
    end
    local hook
    Cooking.install({ AddClassPostConstruct = function(path, fn) assert(path == "widgets/containerwidget"); hook = fn end }, function() return ctx end)
    ctx.cooking = ctx.feature
    box.widget.buttoninfo.position = { x = 0, y = -165 }
    widget.AddChild = function(_, child) return child end
    widget.Close = function(self) self.closed = true; return "closed", nil, 5 end
    widget.Open = function(self, pot, doer, extra)
        assert(pot == ctx.pot and doer == ctx.player and extra == "extra")
        self.button = { onclick = function() end }
        return 1, nil, 3
    end
    hook(widget)
    first, middle, last = widget:Open(ctx.pot, ctx.player, "extra")
    assert(first == 1 and middle == nil and last == 3, "container Open preserves its full native signature")
    assert(widget.fieldkit_repeat == buttons[1] and buttons[1].x == 0 and buttons[1].y == -217)
    local close_first, close_middle, close_last = widget:Close()
    assert(close_first == "closed" and close_middle == nil and close_last == 5 and buttons[1].kills == 1)
    widget:Open(ctx.pot, ctx.player, "extra")
    assert(widget.fieldkit_repeat == buttons[2])
    ctx.scope:close()
    assert(buttons[1].kills == 1 and buttons[2].kills == 1 and widget.fieldkit_repeat == nil)
end)

T.case("quick repeat targeting", function()
    local ctx, inv, box, second, second_box
    -- A quick repeat owns exactly one explicit pot and stops after confirmed ignition.
    ctx, inv, box = setup(8)
    second, second_box = ctx:add_pot(2)
    box.open, ctx.open = false, {}
    assert(ctx.feature:quick_repeat(second))
    assert(drive(ctx) == "done" and ctx.cooked == 1 and ctx.moves == 4)
    for _, call in ipairs(ctx.action_calls) do assert(call.target == second) end
    assert(inv.slots[1].count == 4 and not ctx.pot.finish_at)
    assert(not ctx.feature:quick_repeat(second), "a cooking pot cannot be immediately repeated again")

    ctx, inv, box = setup(4)
    second, second_box = ctx:add_pot(2)
    assert(ctx.feature:quick_repeat(second))
    assert(drive(ctx) == "done" and ctx.pot.finish_at and not second.finish_at,
        "the unique open pot takes priority over a hovered world pot")
    ctx, inv, box = setup(4)
    second, second_box = ctx:add_pot(2)
    second_box.open, ctx.open[second] = true, true
    assert(not ctx.feature:quick_repeat(second), "two open cooking containers require an explicit manual resolution")
    ctx.open, box.open = {}, false
    assert(not ctx.feature:quick_repeat(), "no target never selects the nearest pot")
    second.hidden = true
    assert(not ctx.feature:quick_repeat(second))
    second.hidden, second.x = false, 30
    assert(not ctx.feature:quick_repeat(second))

    ctx, inv, box = setup(4)
    box.unavailable, box.open, ctx.open, ctx.pot.tags.donecooking = true, false, {}, true
    assert(ctx.feature:quick_repeat(ctx.pot) and drive(ctx) == "done")
    assert(ctx.action_calls[1].id == "HARVEST" and ctx.cooked == 1)
    ctx, inv, box = setup(3)
    assert(ctx.feature:quick_repeat() and drive(ctx) == "failed" and ctx.moves == 0 and not ctx.cooked,
        "a shortcut with insufficient ingredients fails before any transfer")
    ctx, inv, box = setup(4)
    box.slots[1] = item("meat")
    assert(not ctx.feature:quick_repeat() and ctx.moves == 0, "partial ingredients are never cleared")
    ctx.blocked = true
    assert(not ctx.feature:quick_repeat() and not ctx.feature:remember_inventory("inventory", 1))
end)

-- The added button uses native press/release and focus-loss semantics unchanged.
T.case("native repeat button focus and release", function()
    local ctx, inv, box, widget
    local base = { OnControl = function() return false end,
        OnLoseFocus = function(self) self.focus = false end }
    local env = setmetatable({
        require = function() return base end,
        Class = T.Class,
        TheFrontEnd = { isprimary = true },
    }, { __index = _G })
    local NativeButton = T.native("widgets/button", env)
    ctx, inv, box = setup(4)
    box.widget.buttoninfo.position = { x = 0, y = -165 }
    widget = { button = { onclick = function() end }, AddChild = function(_, child) return child end }
    ctx.feature:decorate(widget, ctx.pot)
    local repeat_button = widget.fieldkit_repeat
    setmetatable(repeat_button, NativeButton)
    repeat_button.focus, repeat_button.control, repeat_button.stopclicksound = true, 1, true
    repeat_button.clickoffset, repeat_button.text = 0, { SetColour = function() end }
    repeat_button.GetLocalPosition = function() return 0 end
    repeat_button.IsEnabled = function() return true end
    repeat_button.IsDisabledState = function() return false end
    repeat_button.StopUpdating = function() end
    assert(repeat_button:OnControl(1, true) and not ctx.executor:is_running())
    repeat_button:OnLoseFocus()
    assert(not repeat_button:OnControl(1, false) and not ctx.executor:is_running())
    repeat_button.focus = true
    assert(repeat_button:OnControl(1, true) and not ctx.executor:is_running())
    assert(repeat_button:OnControl(1, false) and ctx.executor:is_running())
    local job = ctx.executor.current
    repeat_button:OnControl(1, false)
    assert(ctx.executor.current == job and ctx.moves == 0, "release without a new press cannot queue a second recipe")
    ctx.scope:close()
    assert(repeat_button.kills == 1)
end)

T.case("recipe memory and shortcuts", function()
    local ctx, inv, box
    -- Explicit contiguous slots define quantities, not stack sizes or arbitrary ingredient scans.
    ctx, inv, box = setup(4)
    ctx.store.recipes = {}
    inv.slots = { item("flint"), item("meat", 20), item("meat"), item("meat", 4), item("meat") }
    assert(ctx.feature:remember_inventory("inventory", 2) and #ctx.store.recipes.cookpot == 4 and ctx.moves == 0)
    local previous = ctx.store.recipes
    assert(not ctx.feature:remember_inventory("inventory", 1) and ctx.store.recipes == previous)
    assert(not ctx.feature:remember_inventory("inventory", 13))
    assert(not ctx.feature:remember_inventory("inventory", 0/0))
    assert(not ctx.feature:remember_inventory("chest", 1))
    assert(not ctx.feature:remember_inventory("backpack", 1))
    local backpack = ctx:container(entity("backpack"), 8, { slots = { item("meat"), item("meat"), item("meat"), item("meat") } })
    inv.overflow = backpack
    assert(ctx.feature:remember_inventory("backpack", 1))
    backpack.busy = true
    assert(not ctx.feature:remember_inventory("backpack", 1))
    backpack.busy = false
    ctx.keys, ctx.hover_widget = { [ctx.G.KEY_CTRL] = true }, { parent = { owner = ctx.player, container = inv, num = 2 } }
    assert(ctx.feature:shortcut())
    ctx.hover_widget = { owner = ctx.player, container = box, num = 1 }
    assert(not ctx.feature:shortcut(), "Ctrl shortcut rejects cooker and foreign container slots")
    ctx.keys, ctx.hover_widget = { [ctx.G.KEY_SHIFT] = true }, nil
    assert(ctx.feature:shortcut())
    ctx.keys, ctx.hover_widget = {}, nil
    assert(ctx.feature:shortcut() and drive(ctx) == "done")

    ctx, inv, box = setup(1, "portablespicer")
    inv.slots = { item("meatballs"), item("spice_chili"), item("meat") }
    assert(ctx.feature:remember_inventory("inventory", 1))
    assert(#ctx.store.recipes.portablespicer == 2 and ctx.store.recipes.portablespicer[2] == "spice_chili")
    inv.slots[1], inv.slots[2] = inv.slots[2], inv.slots[1]
    assert(not ctx.feature:remember_inventory("inventory", 1), "the food and spice slots retain native ordering")
end)

T.case("native inventory and container forwarding", function()
    local env = { Class = T.Class, require = function() return {} end, ThePlayer = {} }
    local NativeInventory = T.native("components/inventory_replica", env)
    local NativeContainer = T.native("components/container_replica", env)
    assert(NativeInventory.IsBusy == nil and type(NativeContainer.IsBusy) == "function")
    T.each("owner", { "inventory", "container" }, function(kind)
        T.each("authority", { "replica", "master" }, function(authority)
            local called, master = false, authority == "master"
            local sink = { MoveItemFromCountOfSlot = function(_, slot, target, amount, opener)
                assert(slot == 3 and target == "pot" and amount == 1)
                if kind == "container" and master then assert(opener == env.ThePlayer) end
                called = true
            end }
            local replica = { inst = { components = {} }, classified = sink }
            if master then replica.inst.components[kind] = sink end
            local native = kind == "inventory" and NativeInventory or NativeContainer
            native.MoveItemFromCountOfSlot(replica, 3, "pot", 1)
            assert(called)
        end)
    end)
end)

T.done()
