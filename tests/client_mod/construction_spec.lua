local T = require("tests.client_mod.support")
local Construction = require("fieldkit/features/construction")
local Scope = require("fieldkit/core/scope")
local NativeInventoryItem = T.native("components/inventoryitem_replica", { Class = T.Class })
local function netvar()
    local value
    return { set = function(_, v) value = v end, value = function() return value end }
end
local function noop() end
local classified_env = setmetatable({
    Prefab = function(_, fn) return fn end,
    CreateEntity = function() return { GUID = 1, AddTag = noop,
        entity = { AddTransform = noop, AddNetwork = noop, Hide = noop, SetPristine = noop } } end,
    TheWorld = { ismastersim = true }, DEPLOYMODE = { NONE = 0 }, DEPLOYSPACING = { DEFAULT = 0 },
    net_hash = netvar, net_bool = netvar, net_float = netvar, net_byte = netvar,
    net_smallbyte = netvar, net_tinybyte = netvar,
    math = setmetatable({ clamp = function(n, a, b) return math.max(a, math.min(b, n)) end }, { __index = math }),
}, { __index = _G })
local NativeClassified = T.native("prefabs/inventoryitem_classified", classified_env)

local function context(mode)
    local stack, player = {}, T.entity()
    local function emit(name, data, source) (source or player):PushEvent(name, data) end
    local ctx = { player = player, config = { work = true }, params = require("fieldkit/core/parameters").new(),
        calls = {}, soils = {}, tiles = {}, allowed = {}, buffered = false, materials = 10, now = 0 }
    ctx.params.craft_count = 3
    function ctx:text(_, en) return en end
    function ctx:ready() return true end
    ctx.scope = Scope.new(player, error)
    ctx.G = { GetTime = function() return ctx.now end,
        EQUIPSLOTS = { HANDS = "hands" }, ACTIONS = { WALKTO = {}, EQUIP = {}, DEPLOY = { id = "DEPLOY" }, TILL = { id = "TILL" } } }
    ctx.G.TheWorld = { Map = { GetTileAtPoint = function(_, x) return ctx.tiles[x] or "grass" end } }
    local function vector(x, z) return T.Vector3(x, 0, z) end
    ctx.G.TheSim = { FindEntities = function(_, x, _, _, radius)
        for soil_x in pairs(ctx.soils) do
            if math.abs(soil_x - x) <= radius then return { { IsValid = function() return true end } } end
        end
        return {}
    end }
    local inv = { slots = stack }
    function inv:IsOpenedBy() return true end
    function inv:GetActiveItem() return self.active end
    function inv:GetEquippedItem() return self.hands end
    function inv:GetOverflowContainer() return nil end
    function inv:GetNumSlots() return 4 end
    function inv:GetItemInSlot(slot) return stack[slot] end
    function inv:TakeActiveItemFromAllOfSlot(slot)
        ctx.refills = (ctx.refills or 0) + 1
        if not ctx.delay_refill then self.active, stack[slot] = stack[slot], nil end
    end
    function inv:IsHeavyLifting() return false end
    function inv:CanTakeItemInSlot() return true end
    function inv:AcceptsStacks() return true end
    function inv:GetEquips() return self.hands and { hands = self.hands } or {} end
    function inv:UseItemFromInvTile(item)
        local old = self.hands
        for slot, value in pairs(stack) do if value == item then stack[slot] = nil end end
        if old and old:IsValid() then
            for slot = 1, self:GetNumSlots() do if not stack[slot] then stack[slot] = old; break end end
        end
        self.hands = item
    end
    player.replica.inventory = inv
    inv.classified = { IsBusy = function() return false end }
    function ctx:item(name, count, hoe)
        local item = T.entity({ prefab = name, count = count or 1 })
        function item:HasActionComponent(component)
            return hoe and component == "farmtiller" or self.tags.wateringcan and component == "wateryprotection"
                or self.tags.terraformer and component == "terraformer"
        end
        item.replica.inventoryitem = { IsDeployable = function() return not hoe and item.deployable ~= false end,
            IsLockedInSlot = function() return item.locked == true end }
        item.replica.equippable = { EquipSlot = function() return "hands" end, ShouldPreventUnequipping = function() return false end }
        item.replica.inventoryitem.classified = {
            percentused = { value = function() return item.percent and math.floor(item.percent * 100 + .5) or 255 end },
        }
        item.replica.stackable = {
            StackSize = function() return item.count end,
            GetPreviewStackSize = function() return item.preview end,
            MaxSize = function() return 20 end, OriginalMaxSize = function() return 20 end, CanStackWith = function() return false end,
        }
        return item
    end
    local controller = { ismastersim = true, locomotor = {} }
    function controller:IsBusy() return ctx.busy end
    function controller:IsEnabled() return true end
    function controller:DoAction(action)
        ctx.calls[#ctx.calls + 1] = { action = action.action.id, x = action.point.x, rotation = action.rotation,
            wholestack = action.options.wholestack }
        player.bufferedaction = action
        ctx.pending = action
    end
    function controller:ClearActionHold() end
    function controller:StartBuildPlacementMode(recipe, skin) self.placer_recipe, self.placer_recipe_skin = recipe, skin end
    function controller.locomotor:Stop() ctx.stopped = true end
    function controller.locomotor:Clear() self.bufferedaction = nil end
    player.components.playercontroller = controller
    function ctx:remote()
        controller.ismastersim, controller.locomotor = false, nil
        controller.GetPlatformRelativePosition = function(_, x, z) return nil, x, z end
        local dispatch = controller.DoAction
        function controller:DoAction(action)
            dispatch(self, action); player:ClearBufferedAction(); action.non_preview_cb()
        end
        controller.RemoteStopWalking = function() end
        ctx.G.RPC, ctx.G.SendRPCToServer = { RightClick = 1, LeftClick = 2 }, function() end
    end
    local recipe = { name = "boards", placer = mode == "build" and "wall_placer" or nil }
    local builder = {}
    function builder:IsBusy() return false end
    function builder:KnowsRecipe() return ctx.knows ~= false end
    function builder:HasIngredients() return ctx.materials > 0 end
    function builder:IsBuildBuffered() return ctx.buffered end
    function builder:BufferBuild()
        ctx.buffers = (ctx.buffers or 0) + 1
        ctx.materials = ctx.materials - 1
        if not ctx.delay_buffer then ctx.buffered = true end
    end
    function builder:CanBuildAtPoint(point) return ctx.allowed[point.x] ~= false end
    function builder:MakeRecipeFromMenu(value, skin, ...)
        ctx.calls[#ctx.calls + 1] = { recipe = value, skin = skin, extra = { ... } }
        ctx.pending_recipe = value
        return 11, nil, 33
    end
    function builder:MakeRecipeAtPoint(value, point, rotation, skin, ...)
        assert(ctx.buffered, "construction must prepare each building")
        ctx.calls[#ctx.calls + 1] = { recipe = value, x = point.x, rotation = rotation, skin = skin, extra = { ... } }
        ctx.pending_recipe, ctx.buffered = value, false
    end
    player.replica.builder = builder
    ctx.G.GetValidRecipe = function(name) return name == recipe.name and recipe or nil end
    ctx.recipe = recipe
    ctx.emit = emit
    player.components.playeractionpicker = {
        GetPointActions = function(_, point, item, right)
            assert(ctx._queue_query and right == not (ctx.field_action and ctx.field_action.id == "DROP"),
                "native point query preserves the selected action's mouse button")
            local action = { doer = player, invobject = item, point = point, rotation = 0, options = { wholestack = true },
                action = ctx.field_action or (item:HasActionComponent("farmtiller") and ctx.G.ACTIONS.TILL or ctx.G.ACTIONS.DEPLOY) }
            function action:IsValid() return self.invobject:IsValid() end
            function action:GetActionPoint() return self.point end
            function action:SetActionPoint(p) self.point = p end
            function action:AddSuccessAction(fn) self.success = fn end
            function action:AddFailAction(fn) self.failure = fn end
            return { action }
        end,
        GetInventoryActions = function() return { { action = ctx.G.ACTIONS.EQUIP } } end,
    }
    local records = { { x = 1, z = 0 }, { x = mode == "till" and 3 or 2, z = 0 } }
    ctx.placement = {
        points = function(_, _, _, limit) assert(limit == 128); return records end,
        resolve = function(_, record) return not ctx.lost_platform and vector(record.x, record.z) or nil end,
        validate = function(_, point)
            return ctx.allowed[point.x] ~= false, vector(point.x + (ctx.projection or 0), point.z), 45
        end,
    }
    ctx.area = { corners = { vector(0, 0), vector(3, 0), vector(3, 3), vector(0, 3) } }
    ctx.executor = {
        is_running = function() return ctx.job ~= nil end,
        start = function(object, job) ctx.job, object.current = job, job; return true end,
        cancel = function(_, reason)
            local job = ctx.job
            ctx.job = nil
            if job then job:cancel(reason) end
        end,
    }
    ctx.equipment = require("fieldkit/game/equipment").new(ctx)
    function ctx:tick(now)
        self.now = now
        local result, message = self.job:step(now)
        if result ~= "running" then self.job:cancel(result); self.job = nil; self.message = message end
        return result, message
    end
    function ctx:complete()
        if self.pending then
            local action = self.pending
            if action.action.id == "TILL" then self.soils[action.point.x] = true
            elseif action.action.id == "TERRAFORM" then self.tiles[action.point.x] = "dirt"
            else
                action.invobject.count = action.invobject.count - 1
                if action.invobject.count == 0 then action.invobject.removed = true; inv.active = nil end
            end
            action.success()
            self.pending = nil
        elseif self.pending_recipe then
            emit("builditem", { recipe = self.pending_recipe })
            inv.slots[4] = self:item(self.pending_recipe.product or self.pending_recipe.name)
            self.materials, self.pending_recipe = self.materials - 1, nil
        end
        player.bufferedaction = nil
    end
    if mode == "build" then controller.placer_recipe, controller.placer_recipe_skin = recipe, "skin"
    elseif mode == "till" then inv.hands = ctx:item("hoe", 1, true)
    elseif mode == "deploy" then inv.active = ctx:item("pinecone", 2) end
    ctx.service = Construction.new(ctx)
    return ctx, inv, controller, builder
end

T.case("repeat_keeps_native_recipe_and_skin", function()
    local ctx, _, _, builder = context()
    local a, b, c = builder:MakeRecipeFromMenu(ctx.recipe, "skin", "forwarded")
    assert(a == 11 and b == nil and c == 33 and ctx.calls[1].extra[1] == "forwarded")
    ctx.calls, ctx.pending_recipe = {}, nil
    assert(ctx.service:repeat_recipe(2))
    assert(not ctx.service:craft(ctx.recipe, 1), "construction cannot replace an active operation")
    assert(ctx:tick(0) == "running" and #ctx.calls == 1)
    assert(ctx.calls[1].recipe == ctx.recipe and ctx.calls[1].skin == "skin",
        "repeat crafting retains the recipe and skin from native menu crafting")
    ctx:tick(1)
    assert(#ctx.calls == 1, "idle is not a crafting receipt")
    ctx:complete(); ctx:tick(2)
    assert(#ctx.calls == 2)
    ctx:complete()
    assert(ctx:tick(3) == "done" and not ctx.executor:is_running())
    assert(not ctx.service:craft(ctx.recipe, 0) and not ctx.service:craft(ctx.recipe, 129))
    ctx.scope:close()
end)

T.case("first_prototype_near_technology", function()
    local ctx, _, _, builder = context()
    ctx.knows = false
    builder.CanLearn, builder.GetTechTrees = function() return true end, function() return { SCIENCE = 1 } end
    ctx.recipe.level = { SCIENCE = 1 }
    ctx.G.CanPrototypeRecipe = function(level, tech) return level.SCIENCE <= tech.SCIENCE end
    assert(ctx.service:craft(ctx.recipe, 1))
    ctx:tick(0)
    assert(#ctx.calls == 1, "native first-time prototyping remains available near sufficient technology")
    ctx:complete()
    assert(ctx:tick(1) == "done")
end)

T.case("invalid_sites_keep_materials", function()
    local ctx = context("build")
    ctx.allowed[1] = false
    assert(ctx.service:area(ctx.area))
    ctx:tick(0)
    assert(#ctx.calls == 0 and not ctx.buffers, "illegal points skip before consuming building materials")
    ctx:tick(1); ctx:tick(2)
    assert(ctx.buffers == 1 and #ctx.calls == 1 and ctx.calls[1].x == 2 and ctx.calls[1].rotation == 45)
    ctx:complete(); ctx:tick(3)
    assert(ctx:tick(4) == "done" and ctx.message:find("skipped 1"))
end)

T.case("projected_build_positions", function()
    local ctx = context("build")
    ctx.projection = 10
    assert(ctx.service:area(ctx.area))
    ctx:tick(0); ctx:tick(1)
    assert(ctx.calls[1].x == 11, "native special projected position is submitted")
    ctx:complete(); ctx:tick(2); ctx:tick(3); ctx:tick(4)
    assert(ctx.buffers == 2 and ctx.calls[2].x == 12, "each new building prepares its own recipe")
    ctx:complete(); ctx:tick(5)
    assert(ctx:tick(6) == "done")
end)

T.case("buffer_timeout", function()
    local ctx = context("build")
    ctx.delay_buffer = true
    assert(ctx.service:area(ctx.area))
    ctx:tick(0); ctx:tick(1)
    assert(ctx.buffers == 1 and #ctx.calls == 0, "do not spam asynchronous BufferBuild")
    assert(ctx:tick(16) == "failed")
    assert(ctx.materials == 9, "cancellation never refunds or deletes native buffered ingredients")
end)

T.case("cursor_stack_refill", function()
    local ctx, inv = context("deploy")
    inv.active.count = 1
    inv.slots[1] = ctx:item("pinecone", 1)
    assert(ctx.service:area(ctx.area) and ctx.job.input.cursor == "pinecone")
    ctx:tick(0); ctx:complete(); ctx:tick(1)
    ctx:tick(2); ctx:tick(3)
    assert(ctx.refills == 1 and #ctx.calls == 2)
    ctx:complete(); ctx:tick(4)
    assert(ctx:tick(5) == "done")
end)

T.case("predicted_stack_is_not_receipt", function()
    local ctx, inv = context("deploy")
    inv.active.preview = 1
    assert(ctx.service:area(ctx.area))
    ctx:tick(0)
    assert(#ctx.calls == 0, "predicted inventory counts are not a stable consumption baseline")
    inv.active.preview = nil
    ctx:tick(1)
    assert(#ctx.calls == 1)
    ctx:tick(2)
    assert(#ctx.calls == 1)
    assert(ctx:tick(17) == "failed", "no acknowledgement cannot create more deployments")
end)

T.case("refill_timeout", function()
    local ctx, inv = context("deploy")
    inv.active.count = 1
    inv.slots[1], ctx.delay_refill = ctx:item("pinecone", 1), true
    assert(ctx.service:area(ctx.area))
    ctx:tick(0); ctx:complete(); ctx:tick(1); ctx:tick(2); ctx:tick(3)
    assert(ctx.refills == 1)
    assert(ctx:tick(9) == "failed", "refill requests are bounded")
end)

T.case("invalid_soil_and_replacement_hoe", function()
    local ctx, inv = context("till")
    ctx.allowed[1] = false
    assert(ctx.service:area(ctx.area))
    ctx:tick(0)
    assert(#ctx.calls == 0, "placement validation protects existing soil before tilling")
    inv.hands.removed, inv.hands = true, nil
    inv.slots[1] = ctx:item("another_hoe", 1, true)
    ctx:tick(1); ctx:tick(2)
    assert(#ctx.calls == 1 and ctx.calls[1].action == "TILL")
    ctx:complete(); ctx:tick(3)
    assert(ctx:tick(4) == "done")
end)

T.case("repeat_structure_prepares_placer", function()
    local ctx = context("build")
    assert(ctx.service:craft(ctx.recipe, 5, "prepared_skin"))
    ctx:tick(0)
    assert(ctx:tick(1) == "done" and ctx.player.components.playercontroller.placer_recipe_skin == "prepared_skin")
    assert(ctx.buffers == 1 and #ctx.calls == 0, "repeat a structure prepares native placement instead of inventing a position")
end)

T.case("cancelled_deploy_ignores_late_callbacks", function()
    local ctx = context("deploy")
    assert(ctx.service:area(ctx.area))
    ctx:tick(0)
    local action = ctx.pending
    ctx.executor:cancel("cancelled")
    assert(ctx.player.bufferedaction == nil and not ctx.executor:is_running())
    action.success()
    assert(not ctx.executor:is_running() and ctx.player.bufferedaction == nil and #ctx.calls == 1,
        "late callbacks cannot reactivate cancelled construction or dispatch more actions")
end)

T.case("remote_deploy_needs_consumption", function()
    local ctx, inv, controller = context("deploy")
    ctx:remote()
    assert(ctx.service:area(ctx.area))
    ctx:tick(0)
    ctx.emit("performaction")
    ctx:tick(1)
    assert(#ctx.calls == 1 and ctx.job.completed == 0, "remote performaction only acknowledges an attempt")
    inv.active.preview = 1
    ctx:tick(2)
    assert(ctx.job.completed == 0, "preview depletion does not complete a deployment")
    inv.active.preview, inv.active.count = nil, 1
    ctx:tick(3)
    assert(ctx.job.completed == 1, "non-preview inventory consumption confirms the remote effect")
    ctx.job:cancel("manual")
end)

T.each("ingredients_are_separate_crafts", { "craft", "build" }, function(mode)
    local ctx, inv, _, builder = context(mode)
    local prepared = false
    local intermediate = { name = "rope", ingredients = {} }
    ctx.recipe.ingredients = { { type = "rope", amount = 1 } }
    ctx.G.GetValidRecipe = function(name)
        return name == ctx.recipe.name and ctx.recipe or name == "rope" and intermediate or nil
    end
    ctx.G.RoundBiasedUp = math.ceil
    builder.IngredientMod = function() return 1 end
    builder.HasIngredients = function(_, recipe) return recipe == intermediate or prepared end
    inv.Has = function() return prepared end
    if mode == "craft" then assert(ctx.service:craft(ctx.recipe, 1, "requested_skin"))
    else assert(ctx.service:area(ctx.area)) end
    ctx:tick(0)
    assert(ctx.calls[1].recipe == intermediate and ctx.job.completed == 0)
    prepared = true
    ctx:complete()
    local produced = inv.slots[4]
    inv.slots[4] = nil
    ctx:tick(1)
    assert(#ctx.calls == 1, "a build event before inventory replication cannot trigger another ingredient craft")
    inv.slots[4] = produced
    ctx:tick(2)
    assert(ctx.job.completed == 0, "intermediate completion is not final completion")
    if mode == "build" then ctx:tick(3); ctx:tick(4) end
    assert(ctx.calls[2].recipe == ctx.recipe)
    ctx.emit("buildsuccess")
    ctx:tick(5)
    assert(ctx.job.completed == 0, "host net echoes cannot acknowledge the next native build")
    ctx:complete(); ctx:tick(6)
    if mode == "build" then
        assert(ctx.job.completed == 1)
    else
        assert(not ctx.executor:is_running())
        assert(ctx.service:repeat_recipe(1))
        ctx:tick(7)
        assert(ctx.calls[3].recipe == ctx.recipe and ctx.calls[3].skin == "requested_skin",
            "ingredient crafting cannot replace the remembered final recipe or its skin")
        ctx.executor:cancel("cancelled")
    end
end)

T.case("equip_inventory_hoe", function()
    local ctx, inv = context("till")
    inv.slots[1], inv.hands = inv.hands, nil
    assert(ctx.service:area(ctx.area, "till"), "area tilling can select a hoe from inventory")
    ctx:tick(0); ctx:tick(1)
    assert(inv.hands and inv.hands.prefab == "hoe" and inv.slots[1] == nil and #ctx.calls == 1)
end)

T.case("remote_build_needs_product_receipt", function()
    local Actions = require("fieldkit/game/actions")
    local ctx, inv, controller = context()
    controller.ismastersim = false
    local ticket = assert(Actions.build(ctx, ctx.recipe))
    ctx.emit("buildsuccess")
    assert(not ticket.succeeded and not ticket:result_ready() and not ticket:product_ready())
    inv.slots[1] = ctx:item(ctx.recipe.name)
    inv.slots[1].preview = 1
    assert(not ticket:result_ready(), "predicted product counts cannot acknowledge crafting")
    inv.slots[1].preview = nil
    assert(ticket:result_ready() and ticket:product_ready())
    ticket:close()
    ticket.build_hint = nil
    ctx.emit("buildsuccess")
    assert(not ticket.build_hint, "closed handles release network listeners")

    local dropped = {}
    ctx.G.TheSim.FindEntities = function() return dropped end
    ticket = assert(Actions.build(ctx, ctx.recipe))
    dropped[1] = ctx:item(ctx.recipe.name)
    assert(not ticket:result_ready(), "another nearby player's output alone is not our receipt")
    ctx.emit("buildsuccess")
    assert(ticket:result_ready() and not ticket:product_ready(), "full inventories may receive crafted output on the ground")
    ticket:close()
end)

T.case("remote_structure_needs_buffer_receipt", function()
    local Actions = require("fieldkit/game/actions")
    local ctx, _, controller, builder = context("build")
    controller.ismastersim, ctx.buffered = false, true
    local ticket = assert(Actions.build(ctx, ctx.recipe, { x = 1, z = 0 }))
    assert(not builder:IsBuildBuffered(ctx.recipe.name) and not ticket:result_ready())
    ctx.emit("buildsuccess")
    assert(ticket:result_ready(), "the consumed building buffer plus native result confirms remote construction")
    ticket:close()
end)

T.each("new_equipment_product", { { name = "strawhat", slot = "head" }, { name = "armorwood", slot = "body" }, { name = "backpack", slot = "body" } }, function(product)
    local Inventory = require("fieldkit/game/inventory")
    local ctx, inv, controller = context()
    controller.ismastersim = false
    ctx.G.EQUIPSLOTS = { HANDS = "hands", HEAD = "head", BODY = "body" }
    ctx.recipe.name = product.name
    inv.equipped = {}
    function inv:GetEquippedItem(slot) return self.equipped[slot] end
    assert(ctx.service:craft(ctx.recipe, 1))
    ctx:tick(0)
    ctx.emit("buildsuccess")
    ctx:tick(1)
    assert(ctx.job and ctx.job.completed == 0, "network hint waits for auto-equipment replication")
    local crafted = ctx:item(product.name)
    crafted.GUID, crafted.preview = 101, 1
    inv.equipped[product.slot] = crafted
    ctx:tick(2)
    assert(ctx.job and ctx.job.completed == 0, "predicted auto-equipment is not a stable result")
    crafted.preview = nil
    assert(Inventory.count(ctx, product.name) == 1 and #Inventory.items(ctx) == 0,
        "result counts include all equipment without widening work-tool candidates")
    inv.slots[1] = ctx:item(product.name)
    inv.slots[1].GUID = crafted.GUID
    assert(Inventory.count(ctx, product.name) == 1, "slot/equipment replication aliases count once")
    assert(ctx:tick(3) == "done", "remote crafting completes after its product auto-equips")
end)

T.each("native_panel_focus_cache", { "land", "boat" }, function(surface)
    local ctx, _, controller = context("build")
    local Placement = require("fieldkit/features/placement")
    local Selection = require("fieldkit/features/selection")
    local NativePlacement = require("fieldkit/game/placement")
    local G = ctx.G
    local vector = T.Vector3
    local function handler() return { Remove = function() end } end
    G.TheInput = {
        ControllerAttached = function() return true end,
        IsControlPressed = function() return false end,
        AddMoveHandler = handler, AddMouseButtonHandler = handler,
    }
    G.Vector3, G.FRAMES, G.Class = vector, 1 / 30, T.Class
    G.ThePlayer = ctx.player
    G.Profile = { GetAxisAlignedPlacement = function() return false end }
    G.HAS_AXISALIGNED_MOD_ENABLED = false
    local platform
    if surface == "boat" then
        platform = T.entity({ rotation = 0, entity = {} })
        platform.Transform.GetRotation = function() return platform.rotation end
        platform.entity.WorldToLocalSpace = function(_, x, _, z)
            local c, s = math.cos(platform.rotation * math.pi / 180), math.sin(platform.rotation * math.pi / 180)
            return x * c + z * s, 0, -x * s + z * c
        end
        platform.entity.LocalToWorldSpace = function(_, x, _, z)
            local c, s = math.cos(platform.rotation * math.pi / 180), math.sin(platform.rotation * math.pi / 180)
            return x * c - z * s, 0, x * s + z * c
        end
    end
    G.TheWorld = { Map = { GetPlatformAtPoint = function() return platform end } }
    G.TheSim.RegisterFindTags = function() return {} end
    setmetatable(G.ACTIONS, { __index = function(self, name)
        local action = { id = name }; rawset(self, name, action); return action
    end })
    local native_env = setmetatable({ require = function(name)
        return name:sub(1, 8) == "widgets/" and {} or require(name)
    end }, { __index = function(_, name)
        if G[name] ~= nil then return G[name] end
        return _G[name]
    end })
    local NativeController = T.native("components/playercontroller", native_env)
    native_env.require = function() return {} end
    local NativePlacer = T.native("components/placer", native_env)
    native_env.require = function(name)
        return name:sub(1, 8) == "widgets/" and {} or require(name)
    end
    local Panel = setfenv(assert(loadfile("mods/fieldkit/scripts/fieldkit/ui/panel.lua")), native_env)()
    local instances = {}
    G.Prefabs = { wall_placer = {} }
    G.SpawnPrefab = function(name)
        assert(name == "wall_placer")
        local inst = { x = 0, z = 0, rotation = 0, components = {}, entity = {}, listeners = {} }
        function inst:IsValid() return not self.removed end
        function inst:GetPosition() return vector(self.x, 0, self.z) end
        function inst:GetRotation() return self.rotation end
        function inst:StartWallUpdatingComponent() end
        function inst:Hide() end
        function inst:ListenForEvent(_, fn) self.listeners[#self.listeners + 1] = fn end
        function inst:Remove()
            self.removed = true
            for _, fn in ipairs(self.listeners) do fn() end
            self.components.placer:OnRemoveEntity()
        end
        inst.Transform = {
            GetRotation = function() return inst.rotation end,
            SetRotation = function(_, value) inst.rotation = value end,
            SetPosition = function(_, x, _, z) inst.x, inst.z = x, z end,
        }
        inst.components.placer = NativePlacer(inst)
        inst.components.placer.snap_to_meters = true
        instances[#instances + 1] = inst
        return inst
    end
    controller.inst = ctx.player
    controller.CancelPlacement = NativeController.CancelPlacement
    controller.StartBuildPlacementMode = NativeController.StartBuildPlacementMode
    controller.IsControlPressed = function() return false end
    ctx.player.PushEvent = function() end
    ctx.player.Transform = { GetWorldPosition = function() return 0, 0, 0 end }
    ctx.player.DoPeriodicTask = function() return { Cancel = function() end } end
    ctx.placement = Placement.new(ctx)
    ctx.selection = Selection.new(ctx)
    assert(ctx.selection:mark_start(vector(0, 0, 0), "right"))
    assert(ctx.selection:mark_end(vector(2, 0, 2), "right"))
    controller:StartBuildPlacementMode(ctx.recipe, "chosen_wall_skin")
    controller.placer.Transform:SetRotation(37)
    local before = assert(ctx.placement:points(ctx.selection:area(), nil, 8, nil, ctx.recipe, "build"))
    assert(#before == 4 and before[1].x == .5 and before[1].z == .5)
    for _, inst in ipairs(instances) do
        if inst ~= controller.placer then inst:Remove() end
    end
    local panel = setmetatable({ ctx = ctx, section = "construction", intent = { placement = ctx.placement:context() } }, Panel)
    ctx.screen, ctx.construction = panel, ctx.service
    function ctx:ready() return self.screen == nil end
    function ctx:report(err) error(err) end
    function ctx:notify(message) self.message = message end
    native_env.TheFrontEnd = { PopScreen = function(_, screen) assert(screen == panel) end }
    controller:CancelPlacement(true)
    controller:CancelPlacement(true)
    assert(controller.placer == nil and controller.placer_recipe == nil)
    assert(NativePlacement.selected_recipe(ctx) == ctx.recipe and ctx.placement:context() == nil)
    local after = assert(ctx.placement:points(ctx.selection:area(), nil, 8, nil, ctx.recipe, "build"))
    for i, record in ipairs(before) do
        assert(record.x == after[i].x and record.z == after[i].z, "native focus caching preserves the selected construction lattice")
    end
    if platform then platform.rotation = 90 end
    local build
    for _, row in ipairs(panel:Rows()) do
        if row.label == "Build previewed structure across area" then build = row end
    end
    assert(build)
    build.fn()
    assert(panel.closed and ctx.screen == nil and ctx.job and ctx.job.total == 4,
        "closing the real panel starts construction immediately from the native cached selection")
    assert(controller.placer_recipe == nil and controller.placer_cached[1] == ctx.recipe,
        "construction neither waits for nor forces a native controller restoration frame")
    ctx:tick(0); ctx:tick(1)
    assert(ctx.calls[1].recipe == ctx.recipe and ctx.calls[1].skin == "chosen_wall_skin")
    assert(ctx.calls[1].rotation == (platform and 127 or 37),
        "panel construction preserves the chosen orientation through focus loss and platform rotation")
    assert(platform and ctx.calls[1].x < 0 or not platform and ctx.calls[1].x == before[1].x)
    ctx.job:cancel("manual"); ctx.job = nil
    controller:CancelPlacement(false)
    assert(NativePlacement.selected_recipe(ctx) == nil)
    assert(not ctx.service:area(ctx.selection:area(), "build"), "explicit native cancellation clears the cached selection")
    local DynamicPosition = T.eval(assert(T.source("util"):match("(DynamicPosition = Class.-)\n%-+\n%-%- Class LinkedList"))
        .. "\nreturn DynamicPosition", { Class = T.Class, Vector3 = G.Vector3, TheWorld = G.TheWorld })
    panel = setmetatable({ ctx = ctx, section = "construction", intent = { position = DynamicPosition(vector(1, 0, 1)) } }, Panel)
    ctx.screen = panel
    if platform then platform.rotation = 180 end
    for _, row in ipairs(panel:Rows()) do
        if row.label == "Mark construction area start" then row.fn(); break end
    end
    assert(ctx.screen == nil and ctx.selection.first and math.abs(ctx.selection.first.x - (platform and -1 or 1)) < 1e-8,
        "real panel selection resolves its captured corner after the boat turns")
    if platform then
        local first = ctx.selection.first
        panel = setmetatable({ ctx = ctx, section = "construction", intent = { position = DynamicPosition(vector(-1, 0, 1)) } }, Panel)
        ctx.screen = panel
        platform:Remove()
        for _, row in ipairs(panel:Rows()) do
            if row.label == "Mark construction area start" then row.fn(); break end
        end
        assert(ctx.selection.first == first and ctx.message == "Selected position or platform is no longer available",
            "a removed platform cannot silently turn a saved deck point into a ground point")
    end
    ctx.scope:close()
end)

T.case("tile_watering_receipts_and_spare", function()
    local ctx, inv, controller = context()
    local can = ctx:item("wateringcan")
    can.tags.wateringcan = true
    inv.hands = can
    ctx.field_action = { id = "POUR_WATER_GROUNDTILE", code = 123 }
    ctx:remote()
    assert(ctx.service:area(ctx.area, "water"))
    ctx:tick(0)
    ctx:tick(.1)
    assert(#ctx.calls == 0, "water usage must replicate before an action can consume it")
    can.percent = .5
    ctx:tick(.2)
    assert(ctx.calls[1].action == "POUR_WATER_GROUNDTILE")
    ctx.emit("performaction", { action = ctx.pending })
    ctx.emit("percentusedchange", { percent = .1 }, ctx:item("other_tool"))
    ctx:tick(1)
    assert(ctx.job.completed == 0, "action attempt and another item's usage do not confirm watering")
    can.percent = .4
    ctx.emit("percentusedchange", { percent = can.percent }, can)
    ctx.emit("performaction")
    ctx:tick(2)
    assert(ctx.job.completed == 1 and T.listener_count(can) == 0)
    can.tags.usesdepleted = true
    local spare = ctx:item("premiumwateringcan")
    spare.tags.wateringcan, spare.percent = true, 1
    inv.slots[1] = spare
    ctx:tick(3); ctx:tick(4)
    assert(inv.hands == spare and #ctx.calls == 2, "an empty can is replaced by a usable owned can")
    ctx.job:cancel("manual")
    assert(T.listener_count(can) + T.listener_count(spare) == 0, "cancel removes the current item's receipt listener")
end)

T.case("reject_entity_watering", function()
    local ctx, inv = context()
    local can = ctx:item("wateringcan")
    can.tags.wateringcan, inv.hands = true, can
    ctx.field_action = { id = "POUR_WATER" }
    assert(ctx.service:area(ctx.area, "water"))
    ctx:tick(0); ctx:tick(1)
    assert(ctx:tick(2) == "done" and #ctx.calls == 0, "entity watering cannot substitute for a selected soil tile")
    can.tags.usesdepleted, ctx.config.queue_tools = true, false
    assert(not ctx.service:area(ctx.area, "water"), "empty equipped cans do not start a watering task")
end)

T.case("native_stack_fertilizer_decoder", function()
    local ctx, inv = context()
    inv.active = ctx:item("poop", 1)
    inv.active.tags.fertilizer, inv.active.tags.tile_deploy = true, true
    inv.slots[1] = ctx:item("poop", 1)
    inv.slots[1].tags.fertilizer, inv.slots[1].tags.tile_deploy = true, true
    local fertilizers = { inv.active, inv.slots[1] }
    for _, manure in ipairs(fertilizers) do
        manure.replica.inventoryitem.DeserializeUsage = NativeInventoryItem.DeserializeUsage
        manure.replica.inventoryitem.classified = NativeClassified()
        manure.replica.inventoryitem.classified:SerializePercentUsed(nil)
        assert(not pcall(NativeInventoryItem.DeserializeUsage, manure.replica.inventoryitem),
            "the real decoder is a client-only call even though the replica exposes it on hosts")
    end
    ctx.field_action = { id = "DEPLOY_TILEARRIVE" }
    assert(ctx.service:area(ctx.area) and ctx.job.input.cursor == "poop", "fertilizer selects the tile plan automatically")
    ctx:tick(0); ctx:complete(); ctx:tick(1); ctx:tick(2); ctx:tick(3)
    assert(ctx.refills == 1 and #ctx.calls == 2 and ctx.calls[2].action == "DEPLOY_TILEARRIVE")
    ctx:complete(); ctx:tick(4)
    assert(ctx:tick(5) == "done")
    for _, manure in ipairs(fertilizers) do
        assert(not manure.event_listeners or manure.event_listeners.percentusedchange == nil,
            "stack fertilizer never observes durability or invokes a client decoder")
    end
end)

T.each("native_finite_fertilizer_decoder", { "timeout", "last_use_removed" }, function(outcome)
    local ctx, inv, controller = context()
    local bucket = ctx:item("fertilizer")
    bucket.tags.fertilizer, bucket.tags.tile_deploy = true, true
    bucket.replica.stackable = nil
    bucket.replica.inventoryitem.DeserializeUsage = NativeInventoryItem.DeserializeUsage
    bucket.replica.inventoryitem.classified = NativeClassified()
    bucket.replica.inventoryitem.classified:SerializePercentUsed(1)
    inv.active, ctx.field_action = bucket, { id = "DEPLOY_TILEARRIVE" }
    ctx:remote()
    assert(ctx.service:area(ctx.area, "fertilize"))
    ctx:tick(0)
    bucket.replica.inventoryitem.classified:SerializePercentUsed(.9)
    ctx.emit("percentusedchange", { percent = .9 }, bucket); ctx:tick(.5)
    assert(ctx.job.completed == 0, "finite fertilizer drains its success echo before sending the next use")
    ctx.emit("performaction"); ctx:tick(1)
    assert(ctx.job.completed == 1 and bucket.count == 1, "fertilizer buckets consume uses without shrinking their stack")
    ctx:tick(2)
    if outcome == "timeout" then
        assert(ctx:tick(18) == "failed", "unknown remote results stop rather than repeat fertilizer")
    else
        bucket.replica.inventoryitem.classified:SerializePercentUsed(0)
        ctx.emit("percentusedchange", { percent = 0 }, bucket)
        bucket:Remove(); inv.active = nil
        ctx.emit("performaction"); ctx:tick(3)
        assert(ctx.job.completed == 2 and ctx:tick(4) == "done", "the final usage receipt survives item removal before the next tick")
    end
    assert(T.listener_count(bucket) == 0, "finishing or abandoning the request releases its item listener")
end)

T.each("native_tile_usage_quantization", { "water", "fertilize" }, function(kind)
    local ctx, inv, controller = context()
    local can = ctx:item("unlisted_finite_tool")
    can.replica.stackable = nil
    local classified = NativeClassified()
    can.replica.inventoryitem.classified = classified
    classified:SerializePercentUsed(1)
    if kind == "water" then can.tags.wateringcan, inv.hands = true, can
    else can.tags.fertilizer, can.tags.tile_deploy, inv.active = true, true, can end
    ctx.field_action = { id = kind == "water" and "POUR_WATER_GROUNDTILE" or "DEPLOY_TILEARRIVE", code = 123 }
    ctx:remote()
    assert(ctx.service:area(ctx.area, kind))
    ctx.emit("performaction") -- No action has been sent yet.
    ctx:tick(0)
    classified:SerializePercentUsed(159 / 160)
    ctx.emit("percentusedchange", { percent = classified.percentused:value() / 100 }, can)
    ctx:tick(1); ctx:tick(2)
    assert(ctx.job.completed == 0 and #ctx.calls == 1, "a late first success echo must be drained before the next tile")
    ctx.emit("performaction"); ctx:tick(3); ctx:tick(4)
    assert(ctx.job.completed == 1 and #ctx.calls == 2)
    ctx:tick(5)
    assert(ctx.job.completed == 1, "closing a ticket does not leak its success echo into the next one")
    ctx.emit("performaction", { action = ctx.pending }); ctx:tick(6)
    assert(ctx.job.completed == 1, "local action attempts cannot resolve a quantized usage value")
    classified:SerializePercentUsed(158 / 160)
    ctx.busy = true
    ctx.emit("performaction"); ctx:tick(6.5)
    assert(ctx.job.completed == 1, "an unchanged percentage must wait for the action to settle")
    ctx.busy = false
    ctx:tick(7)
    assert(ctx.job.completed == 2 and classified.percentused:value() == 99,
        "the native encoder can hide one use of any finite tool regardless of its prefab name")
    assert(ctx:tick(8) == "done")
    assert(ctx.service:area(ctx.area, kind))
    ctx:tick(10)
    ctx.emit("performaction", { action = ctx.pending })
    assert(ctx:tick(26) == "failed", "no successful server echo cannot silently advance a failed pour")
end)

T.case("finite_tile_refill", function()
    local ctx, inv = context()
    ctx.params.repeat_count = 5
    inv.active, inv.slots[1] = ctx:item("poop", 4), ctx:item("poop", 6)
    for _, item in ipairs({ inv.active, inv.slots[1] }) do item.tags.fertilizer, item.tags.tile_deploy = true, true end
    ctx.field_action = { id = "DEPLOY_TILEARRIVE" }
    assert(ctx.service:area(ctx.area, "fertilize"))
    local job = ctx.job
    assert(job.total == 10)
    ctx.params.repeat_count = 20
    local now = 0
    for use = 1, 10 do
        for _ = 1, 3 do
            if #ctx.calls < use then ctx:tick(now); now = now + 1 end
        end
        assert(#ctx.calls == use and ctx.calls[use].x == (use <= 5 and 1 or 2))
        ctx:complete(); ctx:tick(now); now = now + 1
        assert(job.completed == use)
    end
    assert(ctx:tick(now) == "done" and ctx.refills == 1 and job.skipped == 0)
end)

T.each("finite_tile_stop", { "rejected", "depleted", "manual" }, function(outcome)
    local ctx, inv = context()
    ctx.params.repeat_count = 5
    inv.active = ctx:item("poop", 1)
    inv.active.tags.fertilizer, inv.active.tags.tile_deploy = true, true
    ctx.field_action = { id = "DEPLOY_TILEARRIVE" }
    assert(ctx.service:area(ctx.area, "fertilize")); local job = ctx.job
    ctx:tick(0)
    if outcome == "rejected" then ctx.pending.failure(); assert(ctx:tick(1) == "failed")
    elseif outcome == "manual" then ctx.executor:cancel("manual")
    else ctx:complete(); ctx:tick(1); assert(ctx:tick(2) == "done") end
    assert(not ctx.job and #ctx.calls == 1 and job.completed == (outcome == "depleted" and 1 or 0))
end)

T.case("finite_remote_watering", function()
    local ctx, inv, controller = context()
    ctx.params.repeat_count = 5
    local can = ctx:item("premiumwateringcan")
    can.tags.wateringcan, can.percent, inv.hands = true, 1, can
    ctx.field_action = { id = "POUR_WATER_GROUNDTILE", code = 123 }
    ctx:remote()
    assert(ctx.service:area(ctx.area, "water")); local job = ctx.job
    for use = 1, 5 do
        local now = (use - 1) * 3
        ctx:tick(now)
        assert(#ctx.calls == use and ctx.pending.point.x == 1)
        ctx.emit("performaction", { action = ctx.pending }); ctx:tick(now + 1)
        assert(job.completed == use - 1, "predicted animation is not a watering receipt")
        ctx.emit("performaction"); ctx:tick(now + 2)
        assert(job.completed == use)
    end
    ctx:tick(15); assert(#ctx.calls == 6 and ctx.pending.point.x == 2)
    ctx.executor:cancel("manual")
end)

T.case("explicit_turf_intent", function()
    local ctx, inv = context()
    local shovel = ctx:item("pitchfork")
    shovel.tags.terraformer = true
    inv.slots[1], ctx.field_action = shovel, { id = "TERRAFORM" }
    assert(not ctx.service:area(ctx.area), "holding a turf tool never implies a destructive area command")
    assert(ctx.service:area(ctx.area, "terraform"))
    ctx:tick(0); ctx:tick(1)
    assert(inv.hands == shovel and ctx.calls[1].action == "TERRAFORM", "area digging equips an unlocked owned pitchfork")
    ctx:tick(2)
    assert(ctx.job.completed == 0, "idle or a dispatched action cannot complete digging")
    ctx:complete(); ctx:tick(3)
    shovel.removed, inv.hands = true, nil
    local spare = ctx:item("pitchfork")
    spare.tags.terraformer = true
    inv.slots[2] = spare
    ctx:tick(4); ctx:tick(5)
    assert(inv.hands == spare and #ctx.calls == 2, "a broken turf tool is replaced before the next tile")
    ctx:complete(); ctx:tick(6)
    assert(ctx:tick(7) == "done")
    spare.tags.plow = true
    assert(not ctx.service:area(ctx.area, "terraform"), "garden plows cannot substitute for turf digging")
end)

T.case("remote_turf_receipt", function()
    local ctx, inv, controller = context()
    local shovel = ctx:item("pitchfork")
    shovel.tags.terraformer, inv.hands = true, shovel
    ctx.field_action = { id = "TERRAFORM", code = 321 }
    ctx:remote()
    assert(ctx.service:area(ctx.area, "terraform"))
    ctx:tick(0); ctx.emit("performaction"); ctx:tick(1)
    assert(ctx.job.completed == 0, "server attempt hints do not prove the tile changed")
    ctx.tiles[1] = "dirt"
    ctx:tick(2)
    assert(ctx.job.completed == 1, "the replicated tile change is the turf receipt")
    ctx:tick(3)
    assert(ctx:tick(19) == "failed", "an unchanged tile times out without retrying destruction")
end)

T.case("native_single_drop_picker", function()
    local ctx, inv = context()
    inv.active = ctx:item("flint", 1)
    inv.active.deployable = false
    local spare = ctx:item("flint", 2)
    spare.deployable = false
    inv.slots[1] = ctx:item("flint", 10)
    inv.slots[1].locked, inv.slots[2] = true, spare
    ctx.field_action = { id = "DROP" }
    ctx.G.ACTIONS.DROP = ctx.field_action
    local native_env = setmetatable({ Class = T.Class, ACTIONS = ctx.G.ACTIONS }, { __index = _G })
    local NativePicker = T.native("components/playeractionpicker", native_env)
    local picker = ctx.player.components.playeractionpicker
    local original = picker.GetPointActions
    picker.inst = ctx.player
    ctx.player.components.playercontroller.IsControlPressed = function() return false end
    for _, item in ipairs({ inv.active, spare }) do
        item.CollectActions = function(_, kind, doer, _, actions, right)
            assert(kind == "POINT" and doer == ctx.player and not right)
            actions[1] = ctx.G.ACTIONS.DROP
        end
    end
    picker.SortActionList = function(self, actions, point, item)
        assert(actions[1] == ctx.G.ACTIONS.DROP)
        return original(self, point, item, false)
    end
    picker.GetPointActions = NativePicker.GetPointActions
    assert(ctx.service:area(ctx.area, "drop") and ctx.job.input.cursor == "flint")
    ctx:tick(0)
    assert(ctx.calls[1].action == "DROP" and ctx.calls[1].wholestack == false,
        "the picker defaults to a whole stack; each queued drop explicitly overrides it")
    ctx:complete(); ctx:tick(1); ctx:tick(2); ctx:tick(3)
    assert(inv.active == spare and inv.slots[1].count == 10, "refill skips locked stacks")
    ctx:complete(); ctx:tick(4)
    assert(ctx:tick(5) == "done" and spare.count == 1, "two points consume exactly two objects")
    spare.locked = true
    assert(not ctx.service:area(ctx.area, "drop"), "a locked cursor item cannot start a drop queue")
end)

T.case("remote_drop_receipt", function()
    local ctx, inv, controller = context()
    inv.active, ctx.field_action = ctx:item("flint", 4), { id = "DROP", code = 654 }
    ctx:remote()
    assert(ctx.service:area(ctx.area, "drop"))
    ctx:tick(0)
    inv.active.preview = 3
    ctx.emit("performaction"); ctx:tick(1)
    assert(ctx.job.completed == 0, "predicted stack changes do not complete a drop")
    inv.active.preview, inv.active.count = nil, 3
    ctx:tick(2)
    assert(ctx.job.completed == 1)
    ctx:tick(3)
    inv.active.count = 1
    assert(ctx:tick(4) == "failed", "an unexpected multi-item loss stops further drops")
end)
local function manufacturing(remote)
    local ctx, inv, controller, builder = context()
    local recipe, player = ctx.recipe, ctx.player
    recipe.name, recipe.product, recipe.manufactured, recipe.nounlock = "shellweaver_nonslipgrit", "nonslipgrit", true, true
    recipe.level, recipe.ingredients = { ORPHANAGE = 0 }, { { type = "saltrock", amount = 1 } }
    local station = T.entity({ tags = { prototyper = true } })
    local actor = remote and T.entity() or player
    local component = remote and {} or builder
    actor.components.builder, actor.components.inventory = component, inv
    actor.sg = { HasAnyStateTag = function() return false end, IsListeningForEvent = function() return false end }
    actor.components.locomotor = { Stop = function() end, PushAction = function(_, action) actor.bufferedaction = action end }
    local env = { Class = T.Class, Builder = {}, Inventory = {}, Prototyper = {}, ACTIONS = { BUILD = {} },
        TheWorld = { ismastersim = true }, GetValidRecipe = ctx.G.GetValidRecipe,
        CanPrototypeRecipe = function() return true end, ValidateRecipeSkinRequest = function(_, _, skin) return skin end,
        PREFAB_SKINS_SHOULD_NOT_SELECT = {}, DynamicPosition = function(point) return { GetPosition = function() return point end } end }
    T.native("bufferedaction", env)
    local function method(path, class, name)
        return T.eval(assert(T.source(path):match("function " .. class .. ":" .. name .. "%b().-\nend"))
            .. "\nreturn " .. class .. "." .. name, env)
    end
    T.eval(assert(T.source("actions"):match("ACTIONS.BUILD.fn = function%b().-\nend")), env)
    local stock = ctx:item("saltrock", 5)
    stock.components.stackable = stock.replica.stackable
    -- Hosts have an identified success callback; clients must consume their own stock.
    local chest = T.entity()
    local box = { slots = remote and {} or { stock }, GetNumSlots = function() return 1 end, IsOpenedBy = function() return true end }
    function box:GetItemInSlot(slot) return self.slots[slot] end
    function box:IsBusy() return self.busy end
    function box:Has(prefab, amount)
        local item = self.slots[1]
        local count = item and item.prefab == prefab and item.count or 0
        return count >= amount, count
    end
    chest.replica.container, chest.components.container = box, box
    if remote then inv.slots[2] = stock end
    local native_inventory = { itemslots = inv.slots, opencontainers = { [chest] = true },
        GetOverflowContainer = function() return inv:GetOverflowContainer() end }
    native_inventory.Has = method("components/inventory", "Inventory", "Has")
    function inv:Has(...) return native_inventory:Has(...) end
    function inv:GetOpenContainers() return native_inventory.opencontainers end
    inv.classified.IsBusy = function() return ctx.inventory_busy end
    component.inst, component.buffered_builds, component.accessible_tech_trees_no_temp = actor, {}, {}
    component.IsBuildBuffered = function() return false end
    component.KnowsRecipe = function() return true end
    component.CanLearn = function() return false end
    component.HasIngredients = function() return stock.count > 0 end
    component.GetIngredients = function() return {} end
    component.CheckIngredientsForMimic = function() return false end
    component.RemoveIngredients = function() stock.count = stock.count - 1 end
    for _, name in ipairs({ "MakeRecipeFromMenu", "MakeRecipe", "DoBuild", "ActivateCurrentResearchMachine" }) do
        component[name] = method("components/builder", "Builder", name)
    end
    local function enable_station(owner)
        owner = owner or station
        owner:AddTag("prototyper")
        owner.components.prototyper = { inst = owner, trees = {}, doers = {} }
        local proto = owner.components.prototyper
        proto.Activate = method("components/prototyper", "Prototyper", "Activate")
        proto.OnRemoveFromEntity = method("components/prototyper", "Prototyper", "OnRemoveFromEntity")
        function proto:onactivate()
            ctx.manufactures = (ctx.manufactures or 0) + 1
            proto:OnRemoveFromEntity()
            owner.components.prototyper = nil
        end
    end
    enable_station()
    component.current_prototyper = station
    if remote then
        ctx:remote()
        builder.MakeRecipeFromMenu = function(_, value, skin) component:MakeRecipeFromMenu(value, skin) end
    end
    function builder:GetCurrentPrototyper() return component.current_prototyper end
    function builder:IsFreeBuildMode() return false end
    function builder:IngredientMod() return 1 end
    ctx.G.RoundBiasedUp = math.ceil
    local knows = builder.KnowsRecipe
    function builder:KnowsRecipe(value) return station:HasTag("prototyper") and not ctx.syncing and knows(self, value) end
    ctx.service = Construction.new(ctx)
    function ctx:perform()
        local action = assert(actor.bufferedaction)
        assert(action:Do(), "native manufactured BUILD must succeed and activate the station")
        actor.bufferedaction = nil
        if remote then player:PushEvent("performaction") end
    end
    function ctx:station_ready(owner)
        owner = owner or station
        owner:RemoveTag("pickable"); enable_station(owner)
    end
    function ctx:current_station(owner) component.current_prototyper = owner end
    return ctx, builder, station, stock, box
end

T.each("native manufactured repeat", { false, true }, function(remote)
    local ctx, builder, station, stock = manufacturing(remote)
    builder:MakeRecipeFromMenu(ctx.recipe)
    ctx:perform(); ctx:station_ready()
    assert(ctx.service:repeat_recipe(2), "the panel's repeat action uses the native recorded recipe")
    ctx:tick(0); ctx:perform()
    assert(ctx:tick(1) == "running" and ctx.job.completed == 0 and not station:HasTag("prototyper"))
    assert(ctx:tick(20) == "running", "manufacturing can exceed ordinary crafting's twelve-second timeout")
    ctx:station_ready(); ctx.syncing = true
    assert(ctx:tick(21) == "running" and ctx.job.completed == 1)
    assert(ctx.manufactures == 2, "wait for recipe access to replicate after the station recovers")
    ctx.syncing = false
    ctx:tick(22); ctx:perform(); ctx:tick(23); ctx:station_ready()
    assert(ctx:tick(29) == "done" and ctx.manufactures == 3 and stock.count == 2)
end)

T.case("manufacturing requires corroborated remote acceptance", function()
    local ctx, _, station, stock, box = manufacturing(true)
    local ticket = assert(require("fieldkit/game/actions").build(ctx, ctx.recipe))
    ctx.emit("performaction")
    assert(not ticket:result_ready(), "an unrelated anonymous event cannot finish a manufacturing request")
    ctx.player.replica.inventory.slots[1] = ctx:item(ctx.recipe.product)
    assert(not ticket:product_ready(), "someone else's matching product cannot bypass manufacturing evidence")
    station:RemoveTag("prototyper"); ticket:result_ready(); station:AddTag("prototyper")
    assert(not ticket:result_ready(), "another player's manufacturing cycle has not consumed our ingredients")
    stock.count = stock.count - 1
    ctx.inventory_busy = true
    assert(not ticket:result_ready(), "personal crafting stock must finish synchronization")
    ctx.inventory_busy, stock.preview = false, stock.count
    assert(not ticket:result_ready(), "predicted ingredient stack changes cannot acknowledge manufacture")
    stock.preview = nil
    assert(ticket:result_ready(), "the same station's cycle plus stable crafting-source consumption confirms completion")
    ticket:close()
    assert(T.listener_count(ctx.player) == 0, "manufacturing listeners close with the receipt")
end)

T.each("remote manufacturing source ownership", { "shared only", "shared and personal", "busy chest", "free crafting", "unrelated chest" }, function(kind)
    local ctx, builder, _, stock, box = manufacturing(true)
    if kind == "shared only" then ctx.player.replica.inventory.slots[2], box.slots[1] = nil, stock
    elseif kind == "shared and personal" then box.slots[1] = ctx:item("saltrock", 5)
    elseif kind == "unrelated chest" then box.slots[1] = ctx:item("rocks", 5)
    elseif kind == "busy chest" then box.busy = true
    else builder.IsFreeBuildMode = function() return true end end
    assert(ctx.service:craft(ctx.recipe, 1))
    local state, message = ctx:tick(0)
    if kind == "unrelated chest" then
        assert(state == "running", "an unrelated open container does not block personal-stock manufacturing")
    else
        assert(state == "failed" and not ctx.manufactures)
        assert(message:find(kind == "free crafting" and "disable free crafting"
            or kind == "busy chest" and "synchronizing" or "Close containers", 1, true),
            "unattributable manufacturing is rejected before native submission with an actionable reason")
    end
end)

T.case("remote manufacturing accepts personal backpack ingredients", function()
    local ctx, _, _, stock, box = manufacturing(true)
    local inv = ctx.player.replica.inventory
    inv.slots[2], box.slots[1] = nil, stock
    function inv:GetOverflowContainer() return box end
    assert(ctx.service:craft(ctx.recipe, 1))
    ctx:tick(0); ctx:perform(); ctx:tick(1); ctx:station_ready()
    assert(ctx:tick(8) == "done", "the equipped backpack belongs to the player despite appearing in open containers")
end)

T.case("manufactured host follows the station used by native BUILD", function()
    local ctx, _, first = manufacturing(false)
    local second = T.entity()
    ctx:station_ready(second)
    assert(ctx.service:craft(ctx.recipe, 1))
    ctx:tick(0)
    first:RemoveTag("prototyper")
    ctx:tick(.1)
    ctx:current_station(second); ctx:perform(); ctx:tick(1)
    ctx:station_ready(first)
    assert(ctx:tick(7) == "running" and ctx.job.completed == 0,
        "another station completing cannot acknowledge our retargeted native BUILD")
    ctx:station_ready(second)
    assert(ctx:tick(8) == "done")
end)

T.case("manufactured remote stops when the selected station changes", function()
    local ctx, _, first = manufacturing(true)
    assert(ctx.service:craft(ctx.recipe, 1))
    ctx:tick(0); ctx:perform(); ctx:tick(1)
    ctx:current_station(T.entity({ tags = { prototyper = true } }))
    ctx:station_ready(first)
    ctx:tick(7)
    local state, message = ctx:tick(8)
    assert(state == "failed" and message:find("Manufacturing station changed", 1, true),
        "an anonymous remote echo cannot identify which of two stations accepted the recipe")
end)

T.each("manufactured oven requires collection", { false, true }, function(remote)
    local ctx, _, station = manufacturing(remote)
    assert(ctx.service:craft(ctx.recipe, 2))
    ctx:tick(0); ctx:perform(); ctx:tick(1)
    station:AddTag("pickable")
    local job = ctx.job
    local state, message = ctx:tick(28)
    assert(state == "done" and job.completed == 1 and message:find("collect the station's product", 1, true))
    assert(ctx.manufactures == 1 and not station:HasTag("prototyper"), "ready oven output stops further submissions until collected")
end)

T.done()
