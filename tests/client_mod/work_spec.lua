local T = require("tests.client_mod.support")
local Work = require("fieldkit/features/work")
local Executor = require("fieldkit/core/executor")
local Rules = require("fieldkit/rules/work")
local pick_range = T.eval(assert(T.source("util"):match("function distsq%b().-\nend")) .. "\n"
    .. assert(T.source("actions"):match("local function PickRangeCheck%b().-\nend")) .. "\nreturn PickRangeCheck")
local repeatable = { "FERTILIZE", "GIVE", "FEED", "ADDFUEL", "ADDWETFUEL", "HEAL", "REPAIR", "SEW", "ADDCOMPOSTABLE",
    "COOK", "UPGRADE", "POUR_WATER", "APPLYPRESERVATIVE", "BOAT_CANNON_LOAD_AMMO", "ADD_CARD_TO_DECK" }

local function context(remote)
    local ctx = { now = 0, entities = {}, calls = {}, items = {}, recipes = {}, forgotten = {},
        config = { work = true, ground = true }, params = require("fieldkit/core/parameters").new() }
    local entity_age = T.eval(assert(T.source("entityscript"):match("function EntityScript:GetTimeAlive%b().-\nend"))
        .. "\nreturn EntityScript.GetTimeAlive", { EntityScript = {}, GetTime = function() return ctx.now end })
    ctx.params.search_radius, ctx.params.fish_count = 30, 1
    local player = T.entity({ tags = { idle = true }, Physics = { GetRadius = function() return .5 end } })
    ctx.player = player
    function ctx:emit(event, source, data) (source or player):PushEvent(event, data) end
    function ctx:listener_count()
        local count = T.listener_count(player)
        for _, item in ipairs(self.items) do count = count + T.listener_count(item) end
        return count
    end
    function ctx:text(_, en) return en end
    function ctx:notify(message) self.message = message end
    function ctx:ready() return true end
    ctx.G = { ACTIONS = { WALKTO = { id = "WALKTO" }, EQUIP = { id = "EQUIP" }, UNEQUIP = { id = "UNEQUIP" } }, EQUIPSLOTS = { HANDS = "hands" },
        GetTime = function() return ctx.now end,
        RPC = { LeftClick = 1, RightClick = 2, UseItemFromInvTile = 3 }, CanEntitySeeTarget = function() return true end,
        GetValidRecipe = function(name) return ctx.recipes[name] end }
    for id in pairs(Rules.policies) do ctx.G.ACTIONS[id] = { id = id, code = id } end
    ctx.G.ACTIONS.CHOP.distance, ctx.G.ACTIONS.PICK.rangecheckfn = 1.75, pick_range
    ctx.G.SendRPCToServer = function() ctx.rpc_count = (ctx.rpc_count or 0) + 1 end
    ctx.G.TheSim = { FindEntities = function(_, x, _, z, radius, tags)
        local found = {}
        for _, entity in ipairs(ctx.entities) do
            if (entity.x-x)^2+(entity.z-z)^2 <= radius^2 and (not tags or entity.ground) then found[#found+1] = entity end
        end
        return found
    end }
    local inv = { slots = {} }
    player.replica.inventory = inv
    function inv:GetActiveItem() return self.active end
    function inv:GetEquippedItem() return self.hands end
    function inv:GetNumSlots() return 32 end
    function inv:GetItemInSlot(slot) return self.slots[slot] end
    function inv:GetOverflowContainer() return nil end
    function inv:IsOpenedBy() return true end
    function inv:IsHeavyLifting() return false end
    function inv:GetEquips() return self.hands and { hands = self.hands } or {} end
    function inv:CanTakeItemInSlot() return true end
    function inv:AcceptsStacks() return true end
    inv.classified = { IsBusy = function() return ctx.inventory_busy end }
    function ctx:confirm_equip(item)
        local old = inv.hands
        for slot, value in pairs(inv.slots) do
            if value.GUID == item.GUID then inv.slots[slot] = nil end
        end
        if old and old:IsValid() then
            for slot = 1, inv:GetNumSlots() do if not inv.slots[slot] then inv.slots[slot] = old; break end end
        end
        inv.hands = item
    end
    function inv:UseItemFromInvTile(item)
        if self.hands == item then
            self.slots[32], self.hands = item, nil
            ctx.unequips = (ctx.unequips or 0) + 1
            return
        end
        ctx.equips = (ctx.equips or 0)+1
        if not ctx.delay_equip then ctx:confirm_equip(item) end
    end
    function inv:EquipActionItem(item) assert(item==self.active); ctx:confirm_equip(item); self.active = nil end
    function inv:ReturnActiveItem()
        ctx.returns = (ctx.returns or 0)+1
        if not ctx.delay_return then self.slots[32], self.active = self.active, nil end
    end
    function inv:TakeActiveItemFromAllOfSlot(slot)
        ctx.refills = (ctx.refills or 0)+1
        if not ctx.delay_refill then self.active, self.slots[slot] = self.slots[slot], nil end
    end
    ctx.G.SendRPCToServer = function(rpc, _, item, modifiers)
        ctx.rpc_count = (ctx.rpc_count or 0) + 1
        if rpc == ctx.G.RPC.UseItemFromInvTile then assert(modifiers == 0); inv:UseItemFromInvTile(item) end
    end
    function ctx:item(name, id, equip)
        local item = T.entity({ GUID = 1000 + #self.items, prefab = name, id = id, count = 1, usage = 1 })
        self.items[#self.items + 1] = item
        item.replica.inventoryitem = { IsLockedInSlot = function() return item.locked end }
        item.replica.stackable = { StackSize = function() return item.count end, GetPreviewStackSize = function() return item.preview end,
            MaxSize = function() return 20 end, OriginalMaxSize = function() return 20 end,
            CanStackWith = function() return false end }
        item.components = { finiteuses = { GetPercent = function() return item.usage end } }
        if equip then item.replica.equippable = { EquipSlot = function() return "hands" end,
            ShouldPreventUnequipping = function() return false end } end
        return item
    end
    function ctx:rod()
        local item = self:item("fishingrod", "FISH", true)
        local replica = {}
        function replica:GetTarget() return self.target end
        function replica:HasCaughtFish() return self.caught end
        function replica:HasHookedFish() return self.hooked end
        item.replica.fishingrod = replica
        return item, replica
    end
    function ctx:entity(id, button, tool, prefab)
        local entity = T.entity({ GUID = #self.entities+1, prefab = prefab or "resource", active = true, x = #self.entities+1,
            id = id, button = button or "left", tool = tool, Physics = { GetRadius = function() return .5 end } })
        function entity:HasTag(tag) return self.tags[tag] or self.active and tag == self.id .. "_workable" end
        function entity:IsActionValid(action, right) return self.active and action.id == self.id and right == (self.button=="right") end
        entity.spawntime, entity.GetTimeAlive = self.now, entity_age
        self.entities[#self.entities+1] = entity
        return entity
    end
    local function action(target, id, item)
        local a = { doer = player, target = target, action = ctx.G.ACTIONS[id], invobject = item, rotation = 0 }
        function a:IsValid() return target:IsValid() end
        function a:GetActionPoint() return target:GetPosition() end
        function a:AddSuccessAction(fn) self.success = fn end
        function a:AddFailAction(fn) self.failure = fn end
        return a
    end
    ctx.action = action
    local function candidates(target, button, item)
        if not target.active or button ~= target.button then return {} end
        if item and item == inv.active and not target.tool then return {} end
        if target.tool and (not item or not item:IsValid() or item.id ~= target.id) then return {} end
        local id = target.id
        if id == "FISH" and item and item.replica.fishingrod:GetTarget() == target then id = "REEL" end
        return { action(target, id, item) }
    end
    local picker = {}
    function picker:GetLeftClickActions(_, target) return candidates(target, "left", inv.active or inv.hands) end
    function picker:GetRightClickActions(_, target) return candidates(target, "right", inv.active or inv.hands) end
    function picker:GetEquippedItemActions(target, item, right) return candidates(target, right and "right" or "left", item) end
    function picker:GetUseItemActions(target, item, right) return candidates(target, right and "right" or "left", item) end
    function picker:GetInventoryActions(item)
        return item.replica.equippable and { { action = inv.hands == item and ctx.G.ACTIONS.UNEQUIP or ctx.G.ACTIONS.EQUIP } } or {}
    end
    player.components.playeractionpicker = picker
    local controller = { ismastersim = not remote }
    if not remote then controller.locomotor = {} end
    function controller:IsEnabled() return true end
    function controller:IsBusy() return ctx.busy end
    function controller:ClearActionHold() end
    function controller:GetPlatformRelativePosition(x,z) return nil,x,z end
    function controller:RemoteStopWalking() end
    if controller.locomotor then
        function controller.locomotor:Stop() ctx.stops = (ctx.stops or 0)+1 end
        function controller.locomotor:Clear() self.bufferedaction = nil end
    end
    function controller:DoAction(a)
        player.bufferedaction, ctx.pending = a, a
        ctx.calls[#ctx.calls+1] = a
        if remote then a.non_preview_cb() end
    end
    player.components.playercontroller = controller
    local builder = {}
    function builder:IsBusy() return false end
    function builder:KnowsRecipe() return true end
    function builder:HasIngredients() return ctx.materials ~= false end
    function builder:MakeRecipeFromMenu(recipe) ctx.crafted = recipe.name;ctx.craft_calls=(ctx.craft_calls or 0)+1 end
    player.replica.builder = builder
    ctx.selection = { forget = function(_, target) ctx.forgotten[target] = true end }
    ctx.executor = Executor.new(function() return ctx.now end, function() return true end, function(s) ctx:notify(s) end, error)
    ctx.equipment = require("fieldkit/game/equipment").new(ctx)
    ctx.work = Work.new(ctx)
    function ctx:tick(delta)
        self.now = self.now + (delta or 1)
        self.executor:tick()
        return self.executor.current
    end
    function ctx:succeed()
        if self.pending.success then self.pending.success() else self:emit("performaction") end
        self.player.bufferedaction = nil
    end
    function ctx:finish_target(target)
        self.player.x, self.player.z = target.x, target.z
        self:succeed(); target.active = false
        self:tick()
    end
    return ctx, inv
end

-- Use the actual action registry, component collectors and picker for updated game interactions.
local function native_picker(c)
    local env = { Class = T.Class, EntityScript = {}, require = function() end,
        global = function() end, orderedPairs = pairs, EQUIPSLOTS = c.G.EQUIPSLOTS,
        TheWorld = { Map = {} }, ThePlayer = c.player, HIGH_ACTION_PRIORITY = 10,
        CONTROL_FORCE_INSPECT = 1, CONTROL_FORCE_ATTACK = 2, CONTROL_FORCE_STACK = 3,
        CONTROL_FORCE_TRADE = 4 }
    T.eval(assert(T.source("actions"):match("^(.-)ACTIONS_BY_ACTION_CODE%s*=")), env)
    for id, definition in pairs(env.ACTIONS) do definition.id, definition.code = id, id end
    for id in pairs(Rules.policies) do assert(env.ACTIONS[id], "queue policy refers to missing native action: " .. id) end
    env.TOOLACTIONS = T.eval("return " .. assert(T.source("constants"):match("TOOLACTIONS%s*=%s*(%b{})")))
    T.native("componentactions", env)
    env.BufferedAction = function(_, target, definition, item) return c.action(target, definition.id, item) end
    c.G.ACTIONS = env.ACTIONS
    local controller = c.player.components.playercontroller
    controller.IsControlPressed = function() return false end
    local register = function(entity, ...)
        entity.actioncomponents = {}
        for name, method in pairs(env.EntityScript) do entity[name] = method end
        function entity:HasTag(tag) return self.tags[tag] == true end
        function entity:is_a(class) return class == env.EntityScript end
        function entity:HasAnyTag(...)
            for _, tag in ipairs({ ... }) do if self:HasTag(tag) then return true end end
            return false
        end
        for _, component in ipairs({ ... }) do entity:RegisterComponentActions(component) end
        if entity.replica.equippable then entity.replica.equippable.IsRestricted = function() return false end end
        return entity
    end
    local picker = T.native("components/playeractionpicker", env)(c.player)
    -- Equipment admission remains the fixture's inventory transport; world action choice is native.
    picker.GetInventoryActions = c.player.components.playeractionpicker.GetInventoryActions
    c.player.components.playeractionpicker = picker
    return register, env
end

T.each("native lunar buildup work", { false, true }, function(remote)
    local c, inv = context(remote)
    local register, env = native_picker(c)
    local target = register(c:entity("REMOVELUNARBUILDUP"), "lunarhailbuildup", "workable")
    target.tags.LunarBuildup, target.tags.MINE_workable = true, true
    inv.slots[1] = register(c:item("multitool_axe_pickaxe", "unused", true), "tool")
    inv.slots[1].tags.MINE_tool = true
    local component = { workleft = 3, totalworkamount = 3, inst = target }
    function component:IsBuildupWorkable() return self.workleft > 0 end
    function component:DropRewards() self.rewarded = true end
    function component:OnWorkFinished() target:RemoveTag("LunarBuildup") end
    function component:StopTickTask() end
    target.components.lunarhailbuildup = component
    env.ShouldItemMimicBeRevealedFor = function() return false end
    env.TheWorld.state = { islunarhailing = false }
    env.math = setmetatable({ clamp = function(v, low, high) return math.max(low, math.min(high, v)) end }, { __index = math })
    env.LunarHailBuildup = component
    T.eval(assert(T.source("components/lunarhailbuildup"):match("function LunarHailBuildup:DoWorkToRemoveBuildup%b().-\nend")), env)
    T.eval(assert(T.source("actions"):match("ACTIONS.REMOVELUNARBUILDUP.fn = function%b().-\nend")), env)
    assert(c.work:start(target))
    local job = c.executor.current
    for strike = 1, 3 do
        for _ = 1, 4 do if #c.calls < strike then c:tick(.4) end end
        assert(#c.calls == strike and c.pending.action.id == "REMOVELUNARBUILDUP",
            "native priority clears the buildup instead of mining the underlying object")
        assert(c.pending.invobject == inv.hands and c.pending.action.fn(c.pending))
        c:succeed(); c:tick(.1)
    end
    c:tick()
    assert(component.rewarded and component.workleft == 0 and job.completed == 1 and not c.executor.current)
    assert(#c.calls == 3 and target:HasTag("MINE_workable"), "finishing buildup cannot change intent to ordinary mining")
end)

T.case("crafting follows native action priority", function()
    local c = context()
    local register = native_picker(c)
    local target = register(c:entity("REMOVELUNARBUILDUP"), "lunarhailbuildup", "workable")
    target.tags.LunarBuildup, target.tags.MINE_workable = true, true
    c.config.queue_craft_tools = true
    c.recipes.pickaxe = { name = "pickaxe" }
    assert(c.work:start(target))
    assert(c.executor.current.entries[1].id == "REMOVELUNARBUILDUP")
    c:tick(); assert(c.crafted == "pickaxe")
    c.executor:cancel("manual")
end)

T.each("native equipped watering", { false, true }, function(remote)
    local c, inv = context(remote)
    local register = native_picker(c)
    local target = register(c:entity("POUR_WATER", "right"))
    target.tags.canpourwateron = true
    inv.slots[1] = register(c:item("premiumwateringcan", "unused", true), "wateryprotection")
    c.params.repeat_count = 5
    assert(c.work:enqueue({ { target = target, button = "right" } }))
    local job = c.executor.current
    for use = 1, 5 do
        for _ = 1, 4 do if #c.calls < use then c:tick(.4) end end
        assert(#c.calls == use and c.pending.action.id == "POUR_WATER")
        inv.hands.usage = inv.hands.usage - .1
        c:succeed(); c:tick(.1)
    end
    c:tick()
    assert(job.applied == 5 and job.completed == 1 and #c.calls == 5 and not c.executor.current)
end)

T.each("native watering replaces depleted tools", { false, true }, function(spare)
    local c, inv = context(true)
    local register = native_picker(c)
    local target = register(c:entity("POUR_WATER", "right"))
    target.tags.canpourwateron = true
    local first = register(c:item("wateringcan", "unused", true), "wateryprotection")
    local second = register(c:item("premiumwateringcan", "unused", true), "wateryprotection")
    inv.hands, inv.slots[1] = first, spare and second or nil
    c.params.repeat_count = 5
    assert(c.work:enqueue({ { target = target, button = "right" } }))
    local job = c.executor.current
    c:tick(); c:succeed(); first.usage, first.tags.usesdepleted = 0, true
    c:tick(); c:tick(); c:tick()
    if spare then
        assert(#c.calls == 2 and c.pending.invobject == second and inv.hands == second,
            "a depleted but still valid watering can cannot shadow a usable spare")
        c.executor:cancel("manual")
    else
        assert(not c.executor.current and job.applied == 1 and job.completed == 0 and #c.calls == 1,
            "material exhaustion cannot be mistaken for a fully watered target")
    end
end)

T.case("native quantized item uses wait for their server receipts", function()
    local c, inv = context(true)
    local register = native_picker(c)
    local target = register(c:entity("POUR_WATER", "right"))
    target.tags.canpourwateron = true
    local can = register(c:item("updated_watering_tool", "unused", true), "wateryprotection")
    local encoded
    local classified = { percentused = {
        set = function(_, value) encoded = value end,
        value = function() return encoded end,
    } }
    local serialize = T.eval(assert(T.source("prefabs/inventoryitem_classified")
        :match("local function SerializePercentUsed%b().-\nend")) .. "\nreturn SerializePercentUsed", {
        math = setmetatable({ clamp = function(n, low, high) return math.max(low, math.min(high, n)) end }, { __index = math }),
    })
    local uses = tonumber(assert(T.source("tuning"):match("PREMIUMWATERINGCAN_USES%s*=%s*(%d+)")))
    can.components, can.replica.inventoryitem.classified = {}, classified
    inv.hands, c.params.repeat_count = can, 3
    serialize(classified, 1)
    assert(c.work:enqueue({ { target = target, button = "right" } }))
    local job = c.executor.current
    c:tick(0)
    serialize(classified, (uses - 1) / uses)
    c.player.bufferedaction = nil
    c:tick(1); c:tick(1)
    assert(job.applied == 0 and #c.calls == 1,
        "consume a use and its delayed server echo before the next request can own an echo")
    c:succeed(); c:tick(1); c:tick(1)
    assert(job.applied == 1 and #c.calls == 2)
    local before = encoded
    serialize(classified, (uses - 2) / uses)
    assert(encoded == before, "native serialization can hide one successful use")
    c:emit("performaction", nil, { action = c.pending })
    c.player.bufferedaction = nil
    c:tick(1)
    assert(job.applied == 1 and #c.calls == 2, "a local prediction cannot confirm an unchanged percentage")
    c:succeed(); c:tick(1); c:tick(1)
    assert(job.applied == 2 and #c.calls == 3)
    serialize(classified, (uses - 3) / uses)
    c:succeed(); c:tick(1); c:tick(1)
    assert(job.applied == 3 and job.completed == 1 and not c.executor.current)

    assert(c.work:enqueue({ { target = target, button = "right" } }))
    job = c.executor.current
    c:tick(0)
    c.player.x = 40
    c:succeed(); c:tick(1)
    assert(job.applied == 0, "an anonymous out-of-range echo cannot confirm a quantized item use")
    c.executor:cancel("manual")
    assert(c:listener_count() == 0)
end)

T.each("native new scene and item actions", {
    { id = "BOTTLE", item = "bottler", tag = "canbebottled" },
    { id = "OCEAN_TRAWLER_FIX", scene = "oceantrawler", tag = "trawler_fish_escaped" },
    { id = "BRUSH", item = "brush", tag = "brushable", equipped = true },
    { id = "UNSADDLE", item = "unsaddler", tag = "saddled", equipped = true },
    { id = "TERRAFORM_REMOVE", item = "terraformer", tag = "terraformerremoveable", button = "right", equipped = true },
}, function(row)
    local c, inv = context()
    local register = native_picker(c)
    local target = register(c:entity(row.id, row.button), row.scene)
    target.tags[row.tag] = true
    if row.item then
        local item = register(c:item("unknown_updated_item", "unused", row.equipped), row.item)
        if row.equipped then inv.slots[1] = item else inv.active = item end
    end
    assert(c.work:enqueue({ { target = target, button = row.button } }))
    local job = c.executor.current
    for _ = 1, 4 do if #c.calls == 0 then c:tick(.4) end end
    assert(#c.calls == 1 and c.pending.action.id == row.id)
    c:succeed(); c:tick(); c:tick()
    assert(job.completed == 1 and #c.calls == 1 and not c.executor.current)
end)

T.case("satisfied target finishes a bounded application intent", function()
    local c, inv = context(true)
    c.params.repeat_count = 5
    inv.active = c:item("rocks", "UPGRADE"); inv.active.count = 10
    local target = c:entity("UPGRADE", "right", true)
    assert(c.work:enqueue({ { target = target, button = "right" } }))
    local job = c.executor.current
    c:tick(); c:succeed(); inv.active.count = 9; target.active = false
    c:tick(); c:tick(); c:tick()
    assert(job.applied == 1 and job.completed == 1 and job.skipped == 0 and #c.calls == 1 and not c.executor.current)
end)

T.each("consuming an application target", { "immediate", "late removal" }, function(delivery)
    local c, inv = context(true)
    c.params.repeat_count = 5
    inv.active = c:item("playing_card", "ADD_CARD_TO_DECK"); inv.active.count = 10
    local target = c:entity("ADD_CARD_TO_DECK", "left", true)
    assert(c.work:start(target))
    local job = c.executor.current
    c:tick(); c:succeed(); inv.active.count = 9
    if delivery == "late removal" then c:tick() end
    target:Remove()
    c:tick(); c:tick()
    assert(job.applied == 1 and job.completed == 1 and job.skipped == 0 and #c.calls == 1 and not c.executor.current,
        "replacement of a consumed target finishes at its confirmed count without seeking a new target")
end)

T.case("mixed_buttons", function()
    local c = context()
    local a, b = c:entity("PICK"), c:entity("HAMMER", "right")
    assert(c.work:enqueue({ { target=a,button="left" }, { target=b,button="right" } }))
    c:tick(); assert(c.pending.action.id == "PICK"); c:finish_target(a)
    c:tick(); assert(c.pending.action.id == "HAMMER"); c:finish_target(b); c:tick()
    assert(not c.executor.current and c.forgotten[a] and c.forgotten[b])
end)

T.case("queue_limit", function()
    local c, entries = context(), {}
    for _ = 1, 129 do
        local target = c:entity("PICK")
        target.x = 1
        entries[#entries + 1] = { target = target }
    end
    local ok, reason = c.work:enqueue(entries)
    assert(ok and #c.executor.current.entries == 128 and reason == "Queue limit: 128 targets; split the work")
    local extra = c:entity("PICK")
    extra.x = 1
    ok, reason = c.work:enqueue({ { target = extra } })
    assert(not ok and reason == "Queue limit: 128 targets; split the work")
end)

T.case("unbounded_successful_strikes", function()
    local c = context(); local target = c:entity("MINE")
    assert(c.work:start(target)); c:tick()
    for _ = 1, 40 do c:succeed(); c:tick() end
    assert(#c.calls == 41)
    c:finish_target(target); c:tick(); assert(not c.executor.current)
end)

T.case("unreachable_target_skips", function()
    local c = context(); local a,b = c:entity("PICK"),c:entity("PICK")
    assert(c.work:start(a)); c:tick(); c:tick(9); c:tick()
    assert(#c.calls == 2 and c.pending.target == b and c.forgotten[a])
    c:finish_target(b); c:tick(); assert(not c.executor.current)
end)

T.case("equipment_replication_and_replacement", function()
    local c, inv = context(); local target = c:entity("MINE","left",true)
    local first, second = c:item("pickaxe","MINE",true),c:item("goldenpickaxe","MINE",true)
    inv.slots[1],inv.slots[2],c.delay_equip = first,second,true
    assert(c.work:preview(target,"left") and not c.equips)
    assert(c.work:start(target)); c:tick(); c:tick(); assert(c.equips == 1 and #c.calls == 0)
    c:confirm_equip(T.entity({ GUID = first.GUID, prefab = first.prefab, id = first.id,
        replica = first.replica, components = first.components }))
    c:tick(); assert(#c.calls == 1)
    c:succeed(); first.removed=true; inv.hands=nil
    c:tick(); assert(c.equips == 2 and #c.calls == 1)
    c:confirm_equip(second); c:tick(); assert(#c.calls == 2 and c.pending.invobject == second)
    c:finish_target(target)
end)

T.case("craft_missing_tool", function()
    local c,inv = context(); local target=c:entity("CHOP","left",true)
    assert(not c.work:preview(target,"left"))
    c.config.queue_craft_tools=true; c.recipes.axe={name="axe"}
    assert(c.work:start(target)); c:tick(); assert(c.crafted=="axe" and #c.calls==0)
    c:tick(); assert(#c.calls==0)
    inv.slots[1]=c:item("axe","CHOP",true); c:emit("builditem",nil,{recipe=c.recipes.axe})
    c:tick(); c:tick(); assert(#c.calls==1)
    c:finish_target(target)
end)

T.case("consumption_and_delayed_refill", function()
    local c, inv = context(true)
    local a,b = c:entity("FERTILIZE","right",true),c:entity("FERTILIZE","right",true)
    local one,two = c:item("poop","FERTILIZE"),c:item("poop","FERTILIZE")
    inv.active,inv.slots[1],c.delay_refill = one,two,true
    assert(c.work:enqueue({{target=a,button="right"},{target=b,button="right"}})); c:tick()
    c:emit("performaction"); one.preview=0; c:tick(); assert(#c.calls==1 and not c.forgotten[a])
    one.preview=nil; one.removed=true; inv.active=nil
    c:tick(); assert(c.forgotten[a]); c:tick(); c:tick(); assert(c.refills==1 and #c.calls==1)
    inv.active,inv.slots[1]=two,nil; c:tick(); assert(#c.calls==2 and c.pending.target==b)
    two.removed=true; inv.active=nil; c:tick(); c:tick(); assert(not c.executor.current)
end)

T.case("attempt_without_result_times_out", function()
    local c, inv = context(true); local target=c:entity("MINE","left",true)
    inv.hands=c:item("pickaxe","MINE",true)
    assert(c.work:start(target)); c:tick()
    for _=1,9 do c:emit("performaction"); c:emit("percentusedchange",inv.hands); c:tick() end
    assert(c.forgotten[target] and #c.calls<=9)
    c:tick(); assert(not c.executor.current and c:listener_count() == 0)
end)

T.case("remote_usage_is_progress", function()
    local c,inv=context(true); local target=c:entity("CHOP","left",true)
    inv.hands=c:item("axe","CHOP",true)
    assert(c.work:start(target)); c:tick()
    for _=1,25 do inv.hands.usage=inv.hands.usage-.01; c:emit("performaction"); c:tick() end
    assert(#c.calls==26 and c.executor.current)
    target.active=false; c:tick(); c:tick(); assert(not c.executor.current)
end)

T.case("append_remove_ownership", function()
    local c=context(); local a,b=c:entity("PICK"),c:entity("PICK")
    assert(c.work:enqueue({{target=a}})); local job=c.executor.current
    assert(c.work:enqueue({{target=a},{target=b}}) and c.executor.current==job and #job.entries==2)
    c:tick(); c.work:remove(a); c:tick(); assert(c.pending.target==b)
    c.executor:cancel("manual"); assert(not c.work.job)
    c.executor:start({timeout=10,step=function() return "running" end})
    assert(not c.work:enqueue({{target=a}}))
end)

T.case("pickup_feature_permissions", function()
    local c=context(); local a=c:entity("PICKUP")
    c.config.work=false; assert(c.work:pickup(a)); c.executor:cancel()
    assert(not c.work:start(a)); c.config.work=true;c.config.ground=false
    assert(not c.work:pickup(a)); assert(c.work:start(a)); c.executor:cancel()
    a.tags.player=true; assert(not c.work:preview(a,"left"))
end)

T.case("fishing_native_lifecycle", function()
    local c,inv=context(); local pond=c:entity("FISH","left",true); pond.tags.fishable=true
    local rod, replica = c:rod(); inv.hands = rod
    assert(c.work:start(pond)); c:tick(); assert(c.pending.action.id=="FISH")
    c:succeed(); replica.target=pond;c:tick();c:tick(35);assert(#c.calls==1)
    c.player.tags.nibble=true;c:tick();assert(#c.calls==2 and c.pending.action.id=="REEL")
    c:succeed();replica.hooked=true;c.player.tags.nibble=nil;c:tick();assert(#c.calls==3)
    replica.caught=true;c:tick();c:tick();assert(#c.calls==3,"catch SG owns collection; no third REEL")
    replica.target,replica.caught,replica.hooked=nil,nil,nil;c:tick();c:tick();c:tick()
    assert(not c.executor.current and c.forgotten[pond])
end)

T.case("fishing_depletion_cancel", function()
    local c,inv=context();local pond=c:entity("FISH","left",true);pond.tags.fishable=true
    local rod, replica = c:rod(); inv.hands = rod
    assert(c.work:start(pond));c:tick();replica.target=pond;c:tick();c:tick(46)
    assert(c.forgotten[pond] and #c.calls==2 and c.pending.action.id=="REEL")
end)

T.case("collect_only_new_drops", function()
    local c=context();c.config.queue_collect=true
    local tree=c:entity("CHOP");local old=c:entity("PICKUP");old.ground=true
    assert(c.work:enqueue({{target=tree}}));c:tick()
    local new=c:entity("PICKUP");new.ground=true
    c:finish_target(tree);c:tick();c:tick();assert(c.pending.target==new)
end)

T.case("working_animation_owns_input", function()
    local c=context();local target=c:entity("MINE")
    c.player.sg = { HasStateTag = function(_, tag) return tag == c.phase end, IsListeningForEvent = function() return false end }
    c.phase="idle"
    assert(c.work:start(target));c:tick();c.phase="working";c:succeed();c:tick();c:tick()
    assert(#c.calls==1,"IsBusy false must not restart an unfinished work animation")
    c.phase="idle";c:tick();assert(#c.calls==2)
    c:finish_target(target)
end)

T.case("transient_picker_outage", function()
    local c=context();local target=c:entity("MINE")
    assert(c.work:start(target));c:tick();c:succeed()
    local picker=c.player.components.playeractionpicker
    local left=picker.GetLeftClickActions
    picker.GetLeftClickActions=function() return {} end
    c:tick();assert(not c.forgotten[target])
    picker.GetLeftClickActions=left;c:tick();assert(#c.calls==2)
    c:finish_target(target)
end)

T.case("full_queue_budget", function()
    local c=context();local selected={}
    for _=1,30 do selected[#selected+1]={target=c:entity("PICK")} end
    assert(c.work:enqueue(selected) and #c.executor.current.entries==30)
    for _,entry in ipairs(selected) do c:tick();c:finish_target(entry.target) end
    c:tick();assert(#c.calls==30 and not c.executor.current)
end)

T.case("borrow_cursor_tool", function()
    local c,inv=context();local shaved=c:entity("SHAVE","right",true);local picked=c:entity("PICK")
    inv.slots[1]=c:item("razor","SHAVE")
    assert(c.work:enqueue({{target=shaved,button="right"},{target=picked}}))
    c:tick();c:tick();assert(c.pending.action.id=="SHAVE")
    c:finish_target(shaved);assert(not inv.active)
    c:tick();assert(c.pending.target==picked)
end)

T.each("remove_borrowed_cursor", { false, true }, function(last)
    local c,inv=context(true);local shaved=c:entity("SHAVE","right",true);local picked=c:entity("PICKUP")
    local razor=c:item("razor","SHAVE");inv.slots[1]=razor;c.delay_return=true
    local entries={{target=shaved,button="right"}}
    if not last then entries[#entries+1]={target=picked} end
    assert(c.work:enqueue(entries));local job=c.executor.current
    c:tick();assert(c.pending.action.id=="SHAVE" and inv.active==razor)
    inv.active=setmetatable({}, {__index=razor}) -- Slot/cursor replicas may wrap the same GUID.
    c.work:remove(shaved);assert(c.returns==1 and job.returning and #job.entries==(last and 0 or 1))
    c:tick();c:tick();assert(c.executor.current==job and #c.calls==1 and c.returns==1)
    inv.slots[32],inv.active=razor,nil;c:tick()
    if last then assert(not c.executor.current and c:listener_count() == 0)
    else
        assert(c.pending.target==picked and job.skipped==0)
        c:finish_target(picked);c:tick();assert(not c.executor.current)
    end
end)

T.case("cancel_pending_cursor_borrow", function()
    local c,inv=context(true);local shaved=c:entity("SHAVE","right",true)
    inv.slots[1]=c:item("razor","SHAVE");c.delay_refill=true
    assert(c.work:enqueue({{target=shaved,button="right"}}));c:tick()
    assert(c.refills==1 and not inv.active and #c.calls==0)
    c.work:remove(shaved);assert(c.returns==1)
    c:tick();assert(not c.executor.current and c.refills==1 and #c.calls==0)
end)

T.each("replacement_cursor_ownership", { false, true }, function(removed)
    local c,inv=context();local shaved=c:entity("SHAVE","right",true)
    inv.slots[1]=c:item("razor","SHAVE")
    assert(c.work:enqueue({{target=shaved,button="right"}}));c:tick()
    local replacement=c:item("razor","SHAVE");inv.active=replacement
    if removed then c.work:remove(shaved) else c:finish_target(shaved) end
    c:tick();assert(not c.executor.current and inv.active==replacement and not c.returns)
end)

T.case("native_equip_cursor_tool", function()
    local c,inv=context();local target=c:entity("CHOP","left",true)
    local axe=c:item("axe","CHOP",true);inv.active=axe;inv.hands=c:item("spear","ATTACK",true)
    assert(c.work:start(target));c:tick();assert(inv.hands==axe and inv.active==nil)
    c:tick();assert(c.pending.action.id=="CHOP")
end)

T.case("craft_receipt_before_replication", function()
    local c,inv=context(true);local target=c:entity("CHOP","left",true)
    c.config.queue_craft_tools=true;c.recipes.axe={name="axe"}
    assert(c.work:start(target));c:tick();c:emit("buildsuccess");c:tick();c:tick()
    assert(c.craft_calls==1 and #c.calls==0)
    inv.slots[1]=c:item("axe","CHOP",true);c:tick();c:tick()
    assert(c.craft_calls==1 and #c.calls==1)
end)

T.case("instant_reel_rpc", function()
    local c,inv=context(true);local pond=c:entity("FISH","left",true);pond.tags.fishable=true
    local rod, replica = c:rod(); inv.hands = rod
    local pc=c.player.components.playercontroller
    c.G.ACTIONS.REEL.instant=true
    function pc:DoAction(action)
        c.pending=action;c.calls[#c.calls+1]=action;c.player.bufferedaction=action
        if action.action.instant then action.preview_cb() else action.non_preview_cb() end
    end
    assert(c.work:start(pond));c:tick();replica.target=pond;c:tick()
    c.executor:cancel("cancelled")
    assert(#c.calls==2 and c.calls[2].action.id=="REEL" and c.rpc_count==2)
    assert(c:listener_count() == 0 and not c.executor.current)
end)

T.case("inventory_tool_permission", function()
    local c,inv=context();local target=c:entity("MINE","left",true)
    local tool=c:item("pickaxe","MINE",true);inv.slots[1]=tool;c.config.queue_tools=false
    assert(not c.work:preview(target,"left") and not c.work:start(target))
    inv.hands=tool;assert(c.work:start(target));c:tick();assert(#c.calls==1 and not c.equips)
    c.executor:cancel();inv.hands=nil;inv.active=tool
    assert(c.work:start(target));c:tick();c:tick();assert(#c.calls==2)
end)

T.case("pickup_visibility_receipt", function()
    local c=context();local item=c:entity("PICKUP")
    assert(c.work:pickup(item));c:tick();c:succeed();item.tags.INLIMBO=true
    local job=c.executor.current;c:tick();assert(job.completed==1 and job.skipped==0)
    local r,inv=context(true);local ground=r:entity("PICKUP")
    assert(r.work:pickup(ground));r:tick();ground.removed=true;r:tick()
    assert(not r.forgotten[ground],"wait briefly for owner-inventory replication")
    inv.slots[1]=r:item(ground.prefab);r:tick();assert(r.executor.current.completed==1)
end)

T.case("unrelated_disappearance", function()
    local c=context(true);local target=c:entity("PICKUP")
    assert(c.work:pickup(target));c:tick();target.removed=true;c:tick();c:tick()
    assert(c.executor.current.skipped==1 and c.executor.current.completed==0)
end)

T.case("missing_tool_deadline", function()
    local c,inv=context();local target=c:entity("MINE","left",true)
    local pickaxe=c:item("pickaxe","MINE",true);inv.hands=pickaxe
    assert(c.work:start(target));c:tick();c:succeed();pickaxe.removed=true;inv.hands=nil
    for _=1,12 do c:tick() end
    assert(c.forgotten[target] and not c.executor.current and #c.calls==1)
end)

local function fishing_context(remote, count)
    local c, inv = context(remote)
    c.params.fish_count = count
    c.work = Work.new(c)
    local pond = c:entity("FISH", "left", true)
    pond.tags.fishable = true
    local rod, replica = c:rod()
    inv.hands = rod
    function c:cast()
        self:tick(); assert(self.pending.action.id == "FISH")
        self:succeed(); replica.target = pond; self:tick()
    end
    function c:pull()
        self.player.tags.nibble = true; self:tick(); assert(self.pending.action.id == "REEL")
        self:succeed(); replica.hooked = true; self.player.tags.nibble = nil
        self:tick(); assert(self.pending.action.id == "REEL")
    end
    function c:collect()
        replica.caught = true; self:tick()
        replica.target, replica.hooked, replica.caught = nil, nil, nil
        self:tick(); self:tick()
    end
    return c, inv, pond, rod, replica
end

T.each("finite_fishing", { false, true }, function(remote)
    local c, _, pond = fishing_context(remote, 5)
    assert(c.work:start(pond)); local job = c.executor.current
    c.params.fish_count = 20
    for caught = 1, 5 do
        c:cast(); c:pull(); c:succeed(); c:tick()
        assert(job.caught == caught - 1, "REEL success only starts the catch animation")
        c:collect(); assert(job.caught == caught)
        assert(job.completed == (caught == 5 and 1 or 0))
    end
    c:tick(); assert(not c.executor.current and #c.calls == 15 and c:listener_count() == 0)
end)

T.each("fish_drops_before_next_target", { "pondfish", "pondeel" }, function(prefab)
    local c, _, pond, _, replica = fishing_context(false, 2)
    local other, old = c:entity("PICK"), c:entity("PICKUP", "left", false, prefab)
    old.ground, old.x = true, 1
    assert(c.work:enqueue({ { target = pond }, { target = other } }))
    local job = c.executor.current
    c:cast(); c:pull(); replica.caught = true; c:tick()
    local fresh = c:entity("PICKUP", "left", false, prefab); fresh.ground, fresh.x = true, 1
    replica.target, replica.hooked, replica.caught = nil, nil, nil
    c:tick(); assert(job.caught == 1 and not c.forgotten[pond])
    c:tick(); assert(c.pending.target == fresh)
    c:finish_target(fresh); c:tick(); assert(c.pending.target == other)
    c:finish_target(other); c:cast(); c:pull(); c:collect(); c:tick()
    assert(not c.executor.current and job.completed == 3 and job.caught == 2)
    for _, action in ipairs(c.calls) do assert(action.target ~= old) end
end)

T.case("fish_animation_before_receipt", function()
    local c, _, pond, _, replica = fishing_context(true, 2)
    assert(c.work:start(pond)); local job = c.executor.current
    c:cast(); c:pull(); replica.caught = true; c.busy = true; c:tick()
    replica.target, replica.caught, replica.hooked = nil, nil, nil
    c:tick(); c:tick(); assert(job.caught == 0 and #c.calls == 3)
    c.busy = false; c:tick(); c:tick(); assert(job.caught == 1 and #c.calls == 3)
    c:tick(); assert(#c.calls == 4 and c.pending.action.id == "FISH")
    c.executor:cancel("manual"); assert(#c.calls == 4 and c:listener_count() == 0)
end)

T.case("broken_rod_replacement", function()
    local c, inv, pond, first, replica = fishing_context(false, 2)
    local spare = c:rod(); inv.slots[1] = spare
    assert(c.work:start(pond)); local job = c.executor.current
    c:cast(); c:pull(); replica.caught = true; c:tick()
    first.removed, inv.hands = true, nil
    c:tick(); c:tick(); assert(job.caught == 1)
    c:tick(); assert(inv.hands == spare and #c.calls == 3)
    c:tick(); assert(c.pending.invobject == spare and c.pending.action.id == "FISH")
    c.executor:cancel("manual")
end)

T.case("coalesced_fish_flags", function()
    local c, _, pond, rod, replica = fishing_context(true, 2)
    assert(c.work:start(pond)); local job = c.executor.current
    c:cast(); c:pull()
    replica.target, replica.hooked, rod.usage = nil, nil, .9
    c:tick(); c:tick(); assert(job.caught == 1)
    c.executor:cancel("manual")
end)

T.case("depleted_pond_yields_queue", function()
    local c, _, pond, _, replica = fishing_context(false, 20)
    local other = c:entity("PICK")
    assert(c.work:start(pond)); local job = c.executor.current
    c:cast(); c:pull(); c:collect(); c:cast()
    assert(c.work:enqueue({ { target = other } }))
    c:tick(46); assert(job.caught == 1 and job.skipped == 1 and c.forgotten[pond])
    replica.target = nil; c:tick(); assert(c.pending.target == other)
    c:finish_target(other); c:tick(); assert(not c.executor.current)
end)

T.case("existing_fishing_and_stale_failure", function()
    local c, _, pond, _, replica = fishing_context(false, 5)
    replica.target = pond
    assert(not c.work:start(pond))
    replica.target = nil
    assert(c.work:start(pond)); local job = c.executor.current
    c:tick(); c.pending.failure(); c:tick()
    assert(job.caught == 0 and job.skipped == 1 and #c.calls == 1)
end)

T.case("lost_pond_is_not_catch", function()
    local c, _, pond = fishing_context(false, 2)
    assert(c.work:start(pond)); local job = c.executor.current
    c:cast(); pond.removed = true; c:tick()
    assert(job.caught == 0 and job.completed == 0 and job.skipped == 1)
end)

T.each("native_fishing_cancel", { false, true }, function(remote)
    T.each("hooked", { false, true }, function(hooked)
        local c, inv, pond, rod, replica = fishing_context(remote, 2)
        assert(c.work:start(pond)); local job = c.executor.current
        c:cast(); c.player.tags.nibble = true
        if hooked then
            c:tick(); c:succeed(); replica.hooked = true; c.player.tags.nibble = nil
        end
        local action, calls, rpc_count = c.pending, #c.calls, c.rpc_count
        c.player.bufferedaction = nil -- native instant REEL has already left the buffer
        c.executor:cancel("cancelled")
        assert(c.unequips == 1 and inv.hands == nil and inv.slots[32] == rod)
        assert(#c.calls == calls and c.rpc_count == rpc_count and c:listener_count() == 0)
        if action.non_preview_cb then action.non_preview_cb() end
        if action.success then action.success() end
        c:emit("performaction"); c:tick(8)
        assert(not c.executor.current and c.rpc_count == rpc_count and job.caught == 0,
            "closed callbacks cannot revive fishing or overwrite the native cancellation")
    end)
end)

T.case("fishing cancellation retains its native inventory intent", function()
    local c, inv, pond, rod = fishing_context(true, 2)
    assert(c.work:start(pond)); c:cast()
    c.player.tags.nibble, c.player.bufferedaction = true, nil
    local G, controller = c.G, c.player.components.playercontroller
    G.CONTROL_FORCE_STACK, G.CONTROL_FORCE_TRADE = 81, 82
    G.ACTIONS.DROP = { id = "DROP", code = "DROP" }
    G.ACTIONS.UNEQUIP.code = "UNEQUIP"
    function controller:IsControlPressed(control) return control == G.CONTROL_FORCE_TRADE end
    c.scope = { wrap = function(_, object, key, factory) object[key] = factory(object[key]) end }
    require("fieldkit/game/actions").attach(c)
    local env = setmetatable({ Class = T.Class, require = function() return {} end }, {
        __index = function(_, key) if G[key] ~= nil then return G[key] end; return _G[key] end,
    })
    local Picker = T.native("components/playeractionpicker", env)
    local picker = c.player.components.playeractionpicker
    picker.inst, picker.GetInventoryActions = c.player, Picker.GetInventoryActions
    function picker:SortActionList(actions)
        local result = {}
        for _, action in ipairs(actions) do result[#result + 1] = { action = action, options = {} } end
        return result
    end
    function rod:CollectActions(_, _, actions) actions[#actions + 1] = G.ACTIONS.UNEQUIP end
    function controller:GetMapTarget() end
    local request
    function controller:RemoteUseItemFromInvTile(action, item)
        assert(action.action == G.ACTIONS.UNEQUIP and item == rod,
            "held Ctrl must not change fishing cancellation into DROP when native inventory asks its picker again")
        assert(request == nil, "cancellation must issue only one equipment request")
        request = item
    end
    local use = T.eval(
        assert(T.source("prefabs/inventory_classified"):match("local function IsBusy%b().-\nend")) .. "\n"
        .. assert(T.source("prefabs/inventory_classified"):match("local function TryNonNetworkedAction%b().-\nend")) .. "\n"
        .. assert(T.source("prefabs/inventory_classified"):match("local function UseItemFromInvTile%b().-\nend"))
        .. "\nreturn UseItemFromInvTile", env)
    function inv:UseItemFromInvTile(item) use({ _parent = c.player }, item) end
    c.executor:cancel("cancelled")
    assert(request == rod and c.equipment:busy() and inv.hands == rod and c:listener_count() == 0)
    assert(not c.equipment:unequip(c.executor.current, rod), "the cancelled job cannot send another request")
    inv.hands, inv.slots[1] = nil, rod
    assert(not c.equipment:busy(), "native storage and equipment replication releases cancellation's receipt")
end)

T.each("full inventory fishing cancellation", { false, true }, function(remote)
    local c, inv, pond, rod = fishing_context(remote, 2)
    assert(c.work:start(pond)); c:cast()
    local job = c.executor.current
    c.player.tags.nibble, c.player.bufferedaction = true, nil
    for slot = 1, inv:GetNumSlots() do inv.slots[slot] = c:item("twigs") end
    function rod.replica.inventoryitem:CanGoInContainer() return true end
    local NativeInventory = T.native("components/inventory", { Class = T.Class, require = function() return {} end })
    inv.maxslots, inv.equipslots = inv:GetNumSlots(), { hands = rod }
    inv.TakeActiveItemFromEquipSlot = NativeInventory.TakeActiveItemFromEquipSlot
    inv.SelectActiveItemFromEquipSlot = NativeInventory.SelectActiveItemFromEquipSlot
    function inv:Unequip(slot)
        assert(c.executor.finishing == job and c.executor.current == nil,
            "the cancellation owns the equipment request before synchronous native callbacks run")
        local item = self.equipslots[slot]
        self.equipslots[slot], self.hands = nil, nil
        return item
    end
    function inv:GiveActiveItem(item)
        self.active, self.activeitem = item, item
        c:emit("newactiveitem", nil, { item = item })
    end
    local observed
    c.player:ListenForEvent("newactiveitem", function(_, data)
        assert(not c.executor.current, "native cursor prediction cannot revive the cancelled task")
        observed = data.item
    end)
    c.executor:cancel("cancelled")
    assert(observed == rod and inv.active == rod and inv.hands == nil and not c.executor.current)
    assert(not c.equipment:busy() and not c.unequips and job.caught == 0)
    for slot = 1, inv:GetNumSlots() do assert(inv.slots[slot].prefab == "twigs") end
end)

T.each("fishing_manual_takeover", { "manual", "invalid", "buffer", "cursor", "rod" }, function(takeover)
    local c, inv, pond, rod, replica = fishing_context(true, 2)
    assert(c.work:start(pond)); c:cast()
    c.player.tags.nibble = true; c:tick(); c:succeed()
    replica.hooked = true; c.player.tags.nibble = nil; c.player.bufferedaction = nil
    if takeover == "buffer" then c.player.bufferedaction = { action = { id = "PICKUP" } }
    elseif takeover == "cursor" then inv.active = c:item("twigs")
    elseif takeover == "rod" then
        local replacement, replacement_state = c:rod()
        inv.hands, replacement_state.target, replacement_state.hooked = replacement, pond, true
    end
    local sends, pending = c.rpc_count, c.player.bufferedaction
    c.executor:cancel((takeover == "manual" or takeover == "invalid") and takeover or "cancelled")
    assert(not c.unequips and inv.hands ~= nil and rod:IsValid() and c.rpc_count == sends)
    assert(c.player.bufferedaction == pending, "an unrelated player action retains ownership")
end)

T.case("admission_result", function()
    local c = context()
    local target = c:entity("PICKUP")
    c.executor.start = function() return false, "Executor admission rejected" end
    local ok, reason = c.work:start(target)
    assert(not ok and reason == "Executor admission rejected" and c.message == nil and c.work.job == nil)
    c.ready = function() return false, "Player is unavailable" end
    ok, reason = c.work:pickup(target)
    assert(not ok and reason == "Player is unavailable" and c.message == nil)
end)

T.each("finite_consumption", { false, true }, function(remote)
    T.each("action", repeatable, function(id)
        local c, inv = context(remote)
        c.params.repeat_count = 5
        local target = c:entity(id, "right", true)
        inv.active, inv.slots[1] = c:item("material", id), c:item("material", id)
        inv.active.count, inv.slots[1].count = 2, 3
        assert(c.work:enqueue({ { target = target, button = "right" } }))
        local job = c.executor.current
        c.params.repeat_count = 20
        for use = 1, 5 do
            for _ = 1, 4 do if #c.calls < use then c:tick() end end
            assert(#c.calls == use and c.pending.action.id == id)
            c:succeed()
            if remote then c:tick(); assert(job.applied == use - 1, "a remote attempt alone never spends the limit") end
            local item = inv.active
            item.count = item.count - 1
            if item.count == 0 then item.removed, inv.active = true, nil end
            c:tick()
            assert(job.applied == use)
        end
        c:tick()
        assert(#c.calls == 5 and c.refills == 1 and job.completed == 1 and not c.executor.current)
    end)
end)

T.each("finite_consumption_stop", { "rejected", "depleted", "manual" }, function(failure)
    local c, inv = context(true)
    c.params.repeat_count = 5
    inv.active = c:item("twigs", "ADDFUEL")
    local target = c:entity("ADDFUEL", "left", true)
    assert(c.work:start(target)); local job = c.executor.current
    c:tick()
    if failure == "rejected" then c:emit("actionfailed", nil, { action = c.pending })
    elseif failure == "manual" then c.executor:cancel("manual")
    else
        inv.active.removed, inv.active = true, nil
        c:succeed()
    end
    for _ = 1, 3 do c:tick() end
    assert(not c.executor.current and #c.calls == 1 and job.completed == 0, failure)
    assert(job.applied == (failure == "depleted" and 1 or 0))
end)

T.case("gift_needs_consumption", function()
    local c, inv = context(true)
    c.params.repeat_count = 5
    inv.active = c:item("carrot", "GIVE")
    local target = c:entity("GIVE", "left", true)
    assert(c.work:start(target)); local job = c.executor.current
    c:tick(); c:succeed(); target.active = false; c:tick(); c:tick(9)
    assert(job.applied == 0 and job.completed == 0 and #c.calls == 1)
end)

T.each("facility_direction", { "TURNON", "TURNOFF", "OCEAN_TRAWLER_LOWER", "OCEAN_TRAWLER_RAISE", "UNWRAP" }, function(id)
    local c = context()
    c.params.repeat_count = 20
    local target = c:entity(id, "right")
    assert(c.work:enqueue({ { target = target, button = "right" } }))
    c:tick(); c:succeed(); target.id = id == "TURNON" and "TURNOFF" or "TURNON"
    c:tick(); c:tick()
    assert(#c.calls == 1 and not c.executor.current)
end)

T.each("area_tool", { "SCYTHE", "NABBAG" }, function(id)
    local c, inv = context()
    local target = c:entity(id, "right", true)
    inv.slots[1] = c:item(id == "SCYTHE" and "voidcloth_scythe" or "wortox_nabbag", id, true)
    assert(c.work:enqueue({ { target = target, button = "right" } }))
    c:tick(); c:tick(); assert(c.pending.action.id == id)
    c:finish_target(target); c:tick()
    assert(#c.calls == 1 and not c.executor.current and not Rules.policies.ATTACK)
end)

T.each("stump_cleanup_permission", { false, true }, function(enabled)
    local c, inv = context()
    c.params.clear_stumps = enabled
    local tree = c:entity("CHOP", "left", true)
    inv.hands, inv.slots[1] = c:item("axe", "CHOP", true), c:item("shovel", "DIG", true)
    assert(c.work:enqueue({ { target = tree } }))
    c.params.clear_stumps = not enabled
    c:tick(); c:succeed()
    tree.id, tree.button, tree.tags.stump = "DIG", "right", true
    c:tick(); c:tick()
    if enabled then
        c:tick(); assert(#c.calls == 2 and c.pending.action.id == "DIG" and c.pending.target == tree)
        c:finish_target(tree); c:tick()
    else assert(#c.calls == 1) end
    assert(not c.executor.current)
end)

T.case("foreign_tree_is_not_ours", function()
    local c, inv = context(true)
    c.params.clear_stumps = true
    local tree = c:entity("CHOP", "left", true)
    inv.hands, inv.slots[1] = c:item("axe", "CHOP", true), c:item("shovel", "DIG", true)
    assert(c.work:enqueue({ { target = tree } })); c:tick()
    tree.id, tree.button, tree.tags.stump = "DIG", "right", true
    c:tick(); c:tick(); c:tick()
    assert(#c.calls == 1 and not c.executor.current)
end)

T.each("remote_result", { "CHOP", "UNWRAP", "FILL" }, function(id)
    T.each("delivery", { "together", "result_first", "echo_first", "echo_only", "foreign", "rejected", "cancelled" }, function(delivery)
        local c, inv = context(true)
        c.params.clear_stumps = true
        local target = c:entity(id, "left", id ~= "UNWRAP")
        local old = c:entity("PICKUP", "left", false, "old_loot")
        old.x, old.ground = target.x, true
        if id == "CHOP" then
            inv.hands, inv.slots[1] = c:item("lucy", id, true), c:item("shovel", "DIG", true)
            inv.hands.components = {} -- Lucy has no durability receipt.
        elseif id == "FILL" then
            inv.active = c:item("wateringcan", id)
            inv.active.usage = .1
        end
        assert(c.work:enqueue({ { target = target } }))
        local job = c.executor.current
        c:tick()
        local action = c.pending
        local function result()
            if id == "CHOP" then target.id, target.button, target.tags.stump = "DIG", "right", true
            elseif id == "FILL" then inv.active.usage = .9
            else
                target.removed = true
                local fresh = c:entity("PICKUP", "left", false, "bundle_loot")
                fresh.x, fresh.ground = target.x, true
            end
        end
        if delivery == "cancelled" then c.executor:cancel("manual") end
        if delivery == "rejected" then c:emit("actionfailed", nil, { action = action }) end
        if delivery == "result_first" then
            result(); c:tick(.1)
            if id ~= "FILL" then assert(job.completed == 0, "world changes need the submitted attempt") end
        end
        if delivery ~= "foreign" then c:succeed() end
        if delivery == "echo_first" or delivery == "echo_only" then
            c:tick(.4)
            assert(job.completed == 0, "an attempt echo is not an action result")
        end
        if delivery ~= "echo_only" and delivery ~= "result_first" then result() end
        c:tick(.1)
        local success = delivery ~= "echo_only" and delivery ~= "rejected" and delivery ~= "cancelled"
            and (delivery ~= "foreign" or id == "FILL")
        assert(job.completed == (success and 1 or 0), "completion requires the action's observed result")
        if success and id == "CHOP" then
            c:tick(); c:tick()
            assert(c.pending.action.id == "DIG" and c.pending.target == target,
                "a completed Lucy chop retains permission to clear the stump")
        elseif not success and delivery ~= "cancelled" then
            c:tick(9); c:tick(); c:tick()
            assert(job.completed == 0 and job.skipped == 1)
        end
        c.executor:cancel("manual")
        if action.non_preview_cb then action.non_preview_cb() end
        c:emit("performaction"); c:tick()
        assert(c:listener_count() == 0 and not c.executor.current)
        for _, call in ipairs(c.calls) do assert(call.target ~= old) end
    end)
end)

T.each("moved_drop_origin", { false, true }, function(remote)
    T.each("result", { "success", "removed", "old_only", "late_drop", "early_echo", "rejected" }, function(outcome)
        local c = context(remote)
        c.config.queue_collect = true
        local tumbleweed = c:entity("PICK", "left", false, "tumbleweed")
        local old = c:entity("PICKUP", "left", false, "old_loot")
        old.x, old.ground = 15, true
        assert(c.work:enqueue({ { target = tumbleweed } }))
        local job = c.executor.current
        c:tick()
        if outcome == "early_echo" then c:emit("performaction") end
        tumbleweed.x, c.player.x = 15, 13
        c:tick(.1) -- Observe the moved target and the pre-existing loot before it opens.
        if outcome ~= "removed" then c:succeed() end
        if outcome == "rejected" then c:emit("actionfailed", nil, { action = c.pending }) end
        tumbleweed.removed = true
        if outcome == "late_drop" then c:tick(.1); assert(job.completed == 0, "allow dropped loot to replicate") end
        local fresh
        if outcome ~= "old_only" then
            fresh = c:entity("PICKUP", "left", false, "fresh_loot")
            fresh.x, fresh.ground = 15, true
        end
        c:tick(.1); c:tick(1); c:tick()
        local success = outcome ~= "removed" and outcome ~= "rejected" and (outcome ~= "old_only" or not remote)
        assert(job.completed == (success and 1 or 0) and job.skipped == (success and 0 or 1))
        if success and fresh then
            assert(c.pending.target == fresh, "collect at the target's last observed position")
        end
        for _, action in ipairs(c.calls) do assert(action.target ~= old, "moving the origin must preserve the old-loot baseline") end
        c.executor:cancel("manual")
    end)
end)

T.each("fill_needs_water", { "unchanged", "decreased", "missing" }, function(change)
    local c, inv = context(true)
    local pond = c:entity("FILL", "left", true)
    inv.active = c:item("wateringcan", "FILL")
    inv.active.usage = .5
    assert(c.work:enqueue({ { target = pond } }))
    local job = c.executor.current
    c:tick(); c:succeed()
    if change == "decreased" then inv.active.usage = .1
    elseif change == "missing" then inv.active.removed, inv.active = true, nil end
    c:tick(); c:tick(9); c:tick()
    assert(job.completed == 0 and job.skipped == 1 and #c.calls == 1)
end)

T.each("distant anonymous echo", { "CHOP", "UNWRAP", "PICK" }, function(id)
    local c, inv = context(true)
    c.params.clear_stumps, c.config.queue_collect = true, true
    local target = c:entity(id, "left", id == "CHOP")
    target.x = 20
    if id == "CHOP" then
        inv.hands, inv.slots[1] = c:item("lucy", id, true), c:item("shovel", "DIG", true)
        inv.hands.components = {}
    end
    assert(c.work:enqueue({ { target = target } }))
    local job = c.executor.current
    c:tick(); c:emit("performaction")
    if id == "CHOP" then target.id, target.button, target.tags.stump = "DIG", "right", true
    else target.removed = true end
    local loot = c:entity("PICKUP"); loot.x, loot.ground = 20, true
    c:tick(.1); c.player.x = 19 -- Walking later must not turn the earlier distant echo into an execution.
    c:tick(1.1); c:tick()
    assert(job.completed == 0 and job.skipped == 1 and #job.entries == 0 and #c.calls == 1)
end)

T.case("each work attempt owns its retry", function()
    local c, inv = context(true)
    local tree = c:entity("CHOP", "left", true)
    inv.hands = c:item("lucy", "CHOP", true); inv.hands.components = {}
    assert(c.work:enqueue({ { target = tree } })); c:tick()
    c:succeed(); c:tick(.4); assert(#c.calls == 2)
    c:tick(.4); assert(#c.calls == 2, "an earlier swing cannot authorize repeated new submissions")
    c:succeed(); c:tick(.4); assert(#c.calls == 3)
    c.executor:cancel("manual")
end)

T.case("removed target retains native reach observation", function()
    local c = context(true)
    local target = c:entity("PICK", "left", false, "tumbleweed")
    assert(c.work:enqueue({ { target = target } }))
    local job = c.executor.current
    c:tick(); target.x, c.player.x = 15, 13; c:tick(.1)
    target.removed = true
    local loot = c:entity("PICKUP"); loot.x, loot.ground = 15, true
    c:tick(.1); assert(job.completed == 0)
    c:succeed(); c:tick(.1)
    assert(job.completed == 1 and job.skipped == 0, "a late echo uses the last native in-range observation")
end)

T.each("loot replication order", { "before removal", "after removal", "two batches" }, function(order)
    T.each("action", { "PICK", "UNWRAP" }, function(id)
        local c = context(true)
        c.config.queue_collect = true
        local target = c:entity(id, "left", false, id == "PICK" and "tumbleweed" or "bundle")
        assert(c.work:enqueue({ { target = target } }))
        local job = c.executor.current
        c:tick()
        local function drop(prefab)
            local loot = c:entity("PICKUP", "left", false, prefab)
            loot.x, loot.ground = target.x, true
            return loot
        end
        if id == "PICK" then target.x, c.player.x = 15, 14 end
        local first = drop("first_loot")
        c:succeed()
        if order ~= "before removal" then target.removed = true end
        c:tick(.1)
        assert(job.completed == 0, "the drop observation window must remain open after the first item")
        target.removed = true
        local second = order == "two batches" and drop("second_loot") or nil
        c:tick(.1); c:tick(1.1)
        local collected = {}
        for index = 1, #job.entries do collected[job.entries[index].target] = true end
        assert(job.completed == 1 and collected[first] and (not second or collected[second]),
            "all observed new drops must survive position and removal replication order")
        c.executor:cancel("manual")
    end)
end)

T.each("drop local age", {
    { name = "known old item cannot confirm opening", born = "before" },
    { name = "known old item stays beside new loot", born = "before", extra = true, completed = true },
    { name = "new entity", born = "later", completed = true, collected = true },
    { name = "same-frame age cannot prove order", born = "same", completed = true, collected = true },
    { name = "old world item first replicated later remains indistinguishable", born = "later", completed = true, collected = true },
}, function(row)
    local c = context(true)
    c.config.queue_collect = true
    local target = c:entity("PICK", "left", false, "tumbleweed")
    local function drop()
        local value = c:entity("PICKUP")
        value.x, value.ground = 5.2, true -- Outside the first observation circle; inside the final one.
        return value
    end
    local candidate = row.born == "before" and drop() or nil
    assert(c.work:enqueue({ { target = target } }))
    local job = c.executor.current
    c:tick()
    if row.born == "later" then c:tick(.1) end
    candidate = candidate or drop()
    target.x, c.player.x = 1.3, .3
    if row.extra then drop() end
    c:succeed(); c:tick(.1); target.removed = true
    c:tick(.1); c:tick(1.1)
    local collected = false
    for i = 1, #job.entries do collected = collected or job.entries[i].target == candidate end
    assert(job.completed == (row.completed and 1 or 0) and job.skipped == (row.completed and 0 or 1))
    assert(collected == (row.collected == true), "only locally known older entities can be excluded by age")
    c.executor:cancel("manual")
end)

T.case("earlier swings keep their new loot", function()
    local c, inv = context(true)
    c.config.queue_collect = true
    local tree = c:entity("CHOP", "left", true)
    inv.hands = c:item("lucy", "CHOP", true); inv.hands.components = {}
    assert(c.work:enqueue({ { target = tree } }))
    local job = c.executor.current
    c:tick(); c:succeed(); c:tick(.2)
    local loot = c:entity("PICKUP"); loot.ground = true
    c:tick(.2); assert(#c.calls == 2)
    c:succeed(); tree.id, tree.tags.stump = "DIG", true
    c:tick(.1); c:tick(1.1)
    assert(job.completed == 1 and job.entries[1] and job.entries[1].target == loot,
        "the age boundary belongs to the target's first dispatch, not its final swing")
    c.executor:cancel("manual")
end)

T.case("completed entries release queue capacity", function()
    local c, entries = context(), {}
    for _ = 1, 128 do
        local target = c:entity("PICK")
        target.x = 1
        entries[#entries + 1] = { target = target }
    end
    assert(c.work:enqueue(entries))
    local job = c.executor.current
    c:tick(); c:finish_target(entries[1].target)
    assert(job.completed == 1 and #job.entries == 127 and job.entries[1].target == entries[2].target)
    local next_target = c:entity("PICK"); next_target.x = 1
    assert(c.work:enqueue({ { target = next_target } }) and #job.entries == 128)
    assert(job.entries[128].target == next_target, "the limit bounds pending work, including appended work")
    c.executor:cancel("manual")
end)

T.each("queue owns revised intent", { "pending", "executing" }, function(phase)
    local c = context(true)
    local first, target = c:entity("PICK"), c:entity("PICK")
    if phase == "executing" then target = first end
    assert(c.work:enqueue({ { target = first }, { target = target } }))
    local job = c.executor.current
    c:tick()
    local previous, sends = c.pending, c.rpc_count
    target.id, target.button = "HAMMER", "right"
    local accepted, intent = c.work:revise(target, "right")
    assert(accepted and intent.button == "right" and intent.id == "HAMMER")
    assert(c.work:enqueue({ { target = target, button = "right" } }), "committing an accepted revision is idempotent")
    if phase == "executing" then
        assert(c.player:GetBufferedAction() == nil, "the previous action is stopped before replacing its intent")
        sends = c.rpc_count
        previous.non_preview_cb()
        assert(c.rpc_count == sends, "the retired action cannot dispatch a late callback")
    else
        assert(c.player:GetBufferedAction() == previous and c.rpc_count == sends)
        c:finish_target(first)
    end
    c:tick()
    assert(c.pending.target == target and c.pending.action.id == "HAMMER")
    assert(#job.entries == 1 and job.entries[1] == intent)
    c.executor:cancel("manual")
end)

T.case("rejected queue revision retains its accepted intent", function()
    local c = context()
    local target = c:entity("PICK")
    assert(c.work:enqueue({ { target = target } })); c:tick()
    local previous = c.pending
    local accepted = c.work:revise(target, "right")
    assert(not accepted and c.executor.current.entries[1].button == "left")
    assert(c.player:GetBufferedAction() == previous, "rejection leaves the currently accepted action intact")
    c.executor:cancel("manual")
end)

T.case("borrowed cursor waits for stable return before next borrow", function()
    local c, inv = context(true)
    local first, second = c:entity("SHAVE", "right", true), c:entity("SHAVE", "right", true)
    local razor = c:item("razor", "SHAVE"); inv.slots[1] = razor
    local take, give = inv.TakeActiveItemFromAllOfSlot, inv.ReturnActiveItem
    function inv:TakeActiveItemFromAllOfSlot(slot)
        if c.inventory_busy then c.blocked_refills = (c.blocked_refills or 0) + 1; return end
        take(self, slot)
    end
    function inv:ReturnActiveItem()
        give(self)
        c.inventory_busy = true
    end
    assert(c.work:enqueue({ { target = first, button = "right" }, { target = second, button = "right" } }))
    local job = c.executor.current
    c:tick(); c:finish_target(first)
    assert(job.completed == 1 and inv:GetActiveItem() == nil and c.inventory_busy)
    c:tick(.1); c:tick(.1)
    assert(job.returning and c.refills == 1 and not c.blocked_refills,
        "a predicted empty cursor cannot complete the inventory handoff")
    c.inventory_busy = false
    c:tick(.1)
    assert(c.refills == 2 and c.pending.target == second and not c.blocked_refills)
    c.executor:cancel("manual")
end)

T.case("final catch releases capacity before queuing its fish", function()
    local c, _, pond, _, replica = fishing_context(false, 1)
    local entries = { { target = pond } }
    for _ = 1, 127 do
        local target = c:entity("PICK"); target.x = 1
        entries[#entries + 1] = { target = target }
    end
    assert(c.work:enqueue(entries))
    local job = c.executor.current
    c:cast(); c:pull(); replica.caught = true; c:tick()
    local fish = c:entity("PICKUP", "left", false, "pondfish"); fish.ground, fish.x = true, 1
    replica.target, replica.hooked, replica.caught = nil, nil, nil
    c:tick()
    assert(job.caught == 1 and job.completed == 1 and #job.entries == 128)
    assert(job.entries[1].target == fish, "a finished pond gives its slot to the newly caught fish")
    c:tick(); assert(c.pending.target == fish)
    c.executor:cancel("manual")
end)

T.done()
