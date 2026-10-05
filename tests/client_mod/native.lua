-- Real engine scenarios; only fixture setup reads server-only state.
return function(entries)
    local self = { result = {}, entities = {}, labels = {} }
    local previous_player = ThePlayer
    local info = {}
    local Scope = require("fieldkit/core/scope")

    local function spawn(prefab)
        local entity = assert(SpawnPrefab(prefab), "missing prefab: " .. prefab)
        self.entities[#self.entities + 1] = entity
        return entity
    end

    local function await(label, predicate, timeout, before_tick)
        return { label = label, predicate = predicate, deadline = GetTime() + (timeout or 30), before_tick = before_tick }
    end

    local function idle(label, before_tick)
        return await(label, function() return not self.ctx.executor:is_running() end, 30, before_tick)
    end

    local function delay(seconds, check)
        local until_time = GetTime() + seconds
        return await("settle " .. seconds .. "s", function()
            if check then check() end
            return GetTime() >= until_time
        end, seconds + 2)
    end

    local function rectangle(a, b, margin)
        margin = margin or .1
        return { corners = { Vector3(a.x - margin, 0, a.z - margin), Vector3(b.x + margin, 0, a.z - margin),
            Vector3(b.x + margin, 0, b.z + margin), Vector3(a.x - margin, 0, b.z + margin) } }
    end

    local function clear_segment(actor, radius)
        local start = actor:GetPosition()
        for i = 0, 15 do
            local angle = i * PI / 8
            local point = start + Vector3(math.cos(angle) * radius, 0, math.sin(angle) * radius)
            if TheWorld.Map:IsPassableAtPoint(point:Get()) and not TheWorld.Map:IsGroundTargetBlocked(point)
                and TheWorld.Pathfinder:IsClear(start.x, 0, start.z, point.x, 0, point.z,
                    actor.components.locomotor.pathcaps) then return point end
        end
        error("no clear native segment of length " .. radius)
    end

    function self:close()
        if self.ctx then
            self.ctx.executor:cancel("test finished")
            self.ctx.scope:close()
            self.ctx.player.player_classified.MapExplorer:DeactivateLocalMiniMap()
            self.ctx = nil
        end
        local labels_removed = true
        for _, label in ipairs(self.labels) do labels_removed = labels_removed and not label:IsValid() end
        for index = #self.entities, 1, -1 do
            if self.entities[index]:IsValid() then self.entities[index]:Remove() end
        end
        if self.farm_tile then
            TheWorld.Map:SetTile(self.farm_tile.x, self.farm_tile.y, self.farm_tile.original)
            self.farm_tile = nil
        end
        ThePlayer = previous_player
        self.entities, self.labels = {}, {}
        assert(labels_removed, "label survived scope")
    end

    local function context()
        local actor = spawn("wilson")
        -- There is no connected client's wake radius in this offline server.
        actor.entity:SetCanSleep(false)
        actor.userid = "KU_FIELDKIT_TEST"
        actor.Physics:Teleport(TheWorld.components.playerspawner:GetAnySpawnPoint())
        actor.sg:GoToState("idle")
        actor.components.inventory:Open()
        ThePlayer = actor
        -- Local player activation normally initializes the engine's exploration map.
        actor.player_classified.MapExplorer:ActivateLocalMiniMap()
        local ctx = { G = _G, player = actor,
            config = require("fieldkit/core/config").read(info.configuration_options, function() end, "en"),
            params = require("fieldkit/core/parameters").new() }
        ctx.params.grid_size, ctx.params.grid_smart_spacing = 1, false
        ctx.params.search_radius, ctx.params.fish_count = 30, 2
        function ctx:text(_, en) return en end
        function ctx:notify(message)
            self.message = tostring(message)
            print("fieldkit-native: " .. self.message)
        end
        function ctx:ready() return actor:IsValid() end
        ctx.scope = Scope.new(actor, error)
        ctx.executor = require("fieldkit/core/executor").new(GetTime,
            function() return ctx:ready() end, function(message) ctx:notify(message) end, error)
        ctx.store = { ready = true, data = {} }
        function ctx.store:get(key) return self.data[key] end
        function ctx.store:set(key, value) self.data[key] = value; return true end
        self.ctx = ctx
        if not actor.components.playeractionpicker then actor:AddComponent("playeractionpicker") end
        if not actor.components.playercontroller then actor:AddComponent("playercontroller") end
        actor.components.playercontroller:Enable(true)
        require("fieldkit/game/actions").attach(ctx)
        ctx.equipment = require("fieldkit/game/equipment").new(ctx)

        return ctx, actor
    end

    local function primitives()
        return {
            function()
                local Announcements, Rules, native_cooking, candidates, uncertain
                assert(TheNet:IsDedicated() and TheWorld.ismastersim)
                setfenv(entries.modinfo, info)()
                assert(info.client_only_mod and not info.all_clients_require_mod)
                setfenv(entries.modmain, {
                    GLOBAL = _G,
                    require = function() error("dedicated modmain loaded a client module") end,
                })()
                self.result.dedicated_guard = true

                Announcements = require("fieldkit/features/announcements")
                assert(Announcements.clean("25% ready\n\0世界🙂abc", 12) == "25% ready  世")
                assert(Announcements.clean("世界🙂", 2) == "世界")
                assert(Announcements.clean("世界🙂abc", 3) == "世界🙂")
                self.result.native_text = true

                Rules = require("fieldkit/rules/cooking")
                native_cooking = require("cooking")
                candidates, uncertain = Rules.preview(native_cooking, "cookpot", {
                    "monstermeat", "berries", "berries", "berries",
                })
                assert(not uncertain and #candidates == 1 and candidates[1] == "meatballs")
                candidates, uncertain = Rules.preview(native_cooking, "portablespicer", { "meatballs", "spice_chili" })
                assert(not uncertain and #candidates == 1 and candidates[1] == "meatballs_spice_chili")
                self.result.native_recipes = true
            end,
        }
    end

    local function observations()
        return {
            function()
                local ctx, actor, recipe, costs, ranges, rod, crown, descriptors, LootRules, lamp, light_radius
                local light_rules, blueprint, known_blueprint, blueprint_names, loot, loot_frame, Information
                local snapshot, seasons, year_length, calendar_progress, fullmoon, temperature, body, Placement, hook
                local placer_entity, placer, original_transform, point, x, y, z, expected_x, expected_z
                ctx, actor = context()
                recipe = assert(require("fieldkit/game/announcement_data").recipe(ctx, "campfire"))
                costs = {}
                for _, ingredient in ipairs(recipe.ingredients) do
                    assert(ingredient.category == "item" and ingredient.quality == "known"
                        and ingredient.count == 0 and ingredient.has == false)
                    costs[ingredient.type] = ingredient.required
                end
                assert(recipe.status == "known" and costs.cutgrass == 3 and costs.log == 2,
                    "native crafting announcement did not expose the campfire costs")
                self.result.native_announcement_costs = true

                ranges = require("fieldkit/rules/ranges")
                rod, crown = spawn("lightning_rod"), spawn("hivehat")
                descriptors = ranges.describe(TUNING, rod.prefab)
                assert(#descriptors == 1 and descriptors[1].kind == "lightning" and descriptors[1].radius == 40)
                LootRules = require("fieldkit/rules/loot")
                assert(LootRules.items[crown.prefab], "native hivehat was not recognized as valuable")
                rod:Remove()
                crown:Remove()
                self.result.native_range_identity = true

                for _, sample in ipairs({
                    { "firesuppressor", TUNING.FIRE_DETECTOR_RANGE, "detection" },
                    { "trap_teeth", TUNING.TRAP_TEETH_RADIUS, "trigger" },
                    { "panflute", TUNING.PANFLUTE_SLEEPRANGE, "listeners" },
                    { "boat_magnet", TUNING.BOAT.BOAT_MAGNET.PAIR_RADIUS, "connection" },
                }) do
                    local entity = spawn(sample[1])
                    local rules = ranges.describe(TUNING, entity.prefab, entity)
                    assert(#rules == 1 and rules[1].radius == sample[2] and rules[1].kind == sample[3],
                        "native range mismatch: " .. entity.prefab)
                    entity:Remove()
                end
                self.result.native_range_catalog = true

                lamp = spawn("mushroom_light")
                assert(lamp.components.container:GiveItem(spawn("lightbulb"), 1))
                assert(lamp.Light:IsEnabled(), "native mushroom light did not turn on")
                light_radius = lamp.Light:GetCalculatedRadius()
                light_rules = ranges.describe(TUNING, lamp.prefab, lamp)
                assert(light_radius > 0 and #light_rules == 1 and light_rules[1].radius == light_radius
                    and light_rules[1].kind == "light" and light_rules[1].precision == "estimate")
                lamp:Remove()
                self.result.native_range_light = true

                blueprint = spawn("dragonflyfurnace_blueprint")
                known_blueprint = spawn("spear_blueprint")
                blueprint_names = LootRules.blueprint_names(_G)
                assert(blueprint.prefab == "blueprint" and blueprint_names[blueprint.name] == "dragonflyfurnace")
                assert(known_blueprint.prefab == "blueprint" and blueprint_names[known_blueprint.name] == "spear")
                assert(not actor.replica.builder:KnowsRecipe("dragonflyfurnace", true))
                actor.components.builder:UnlockRecipe("spear", true)
                assert(actor.replica.builder:KnowsRecipe("spear", true))
                loot = require("fieldkit/features/loot").new(ctx)
                loot_frame = loot:observe({ blueprint, known_blueprint }, GetTime())
                assert(#loot_frame == 1 and loot_frame[1].entity == blueprint and loot_frame[1].quality == "observed"
                    and not loot_frame[1].glow, "existing or learned blueprints were treated as new loot")
                actor.components.builder:UnlockRecipe("dragonflyfurnace", true)
                assert(actor.replica.builder:KnowsRecipe("dragonflyfurnace", true))
                assert(#loot:observe({ blueprint, known_blueprint }, GetTime()) == 0, "learned blueprint remained highlighted")
                blueprint:Remove()
                known_blueprint:Remove()
                self.result.native_loot_blueprints = true

                Information = require("fieldkit/game/information")
                ctx.information = Information.new(ctx)
                snapshot = ctx.information:snapshot()
                assert(snapshot.own.health.quality == "known" and snapshot.own.health.value > 0)
                assert(snapshot.world.day == TheWorld.state.cycles + 1)
                assert(snapshot.nightmare.visible == false)
                self.result.native_information = true

                seasons = TheWorld.net.components.seasons:OnSave()
                year_length = 0
                for _, season in ipairs({ "autumn", "winter", "spring", "summer" }) do
                    local length = seasons.lengths[season]
                    assert(snapshot.world.season_lengths[season] == length and TheWorld.state[season .. "length"] == length,
                        "native season length mismatch: " .. season)
                    year_length = year_length + length
                end
                assert(year_length > 0 and snapshot.world.year_length == year_length)
                assert(snapshot.world.season == seasons.season and snapshot.world.season_day == seasons.elapseddaysinseason + 1)
                calendar_progress = seasons.elapseddaysinseason
                    / (seasons.elapseddaysinseason + seasons.remainingdaysinseason)
                assert(snapshot.world.season_progress == calendar_progress)
                assert(snapshot.world.next_season and snapshot.world.season_lengths[snapshot.world.next_season] > 0)
                fullmoon = snapshot.world.fullmoon
                assert(fullmoon.quality == "unknown" or fullmoon.quality == "estimated")
                if fullmoon.quality == "estimated" then
                    assert(fullmoon.min_day >= snapshot.world.day and fullmoon.max_day >= fullmoon.min_day)
                end
                self.result.native_calendar = true

                temperature = actor:GetTemperature()
                actor.components.temperature:SetTemperature(20)
                body = ctx.information:snapshot().own.temperature
                actor.components.temperature:SetTemperature(temperature)
                assert(body.quality == "known" and body.value == 20)
                assert(Information.temperature(body.value) == "20°")
                self.result.native_temperature = true

                require("fieldkit/features/presentation").new(ctx)
                for _, entity in pairs(Ents) do
                    if entity._fieldkit_text then
                        assert(entity.Label ~= nil and entity.Network == nil and entity.persists == false)
                        self.labels[#self.labels + 1] = entity
                    end
                end
                assert(#self.labels > 0, "no labels on the native visible player")
                self.result.local_labels = true

                Placement = require("fieldkit/features/placement")
                Placement.install({ AddComponentPostInit = function(name, fn)
                    assert(name == "placer")
                    hook = fn
                end }, function() return ctx end)
                placer_entity = spawn("campfire_placer")
                placer = placer_entity.components.placer
                original_transform = placer.GetAxisAlignedPlacementTransform
                hook(placer)
                placer:SetBuilder(actor, AllRecipes.campfire)
                point = actor:GetPosition()
                placer.selected_pos = Vector3(point.x + .3, 0, point.z + .3)
                assert(Placement.supported(placer, ctx), "native campfire placer not supported")
                x, y, z = placer:GetAxisAlignedPlacementTransform(point.x + .3, 0, point.z + .3)
                expected_x, expected_z = math.floor(point.x + .3 + .5), math.floor(point.z + .3 + .5)
                assert(x == expected_x and z == expected_z and y == 0)
                assert(placer_entity.Network == nil)
                ctx.scope:close()
                assert(placer.GetAxisAlignedPlacementTransform == original_transform)
                placer_entity:Remove()
                self.result.native_placer = true
            end,
        }
    end

    local function inventory()
        local ctx, actor, inv, cursor, chest, existing
        return {
            function()
                local source
                ctx, actor = context()
                inv = actor.components.inventory
                ctx.storage = require("fieldkit/features/storage").new(ctx)
                source, cursor = spawn("flint"), spawn("flint")
                source.components.stackable:SetStackSize(30)
                cursor.components.stackable:SetStackSize(30)
                assert(inv:GiveItem(source, 1))
                inv:GiveActiveItem(cursor)
                assert(ctx.storage:scroll(actor.replica.inventory, 1, 1, 5))
                return idle("storage_take")
            end,
            function()
                assert(inv:GetActiveItem().components.stackable:StackSize() == 35, "incremental cursor merge failed: " .. tostring(ctx.message))
                assert(inv:GetItemInSlot(1).components.stackable:StackSize() == 25)
                assert(inv:GetItemInSlot(2) == nil, "temporary cursor slot was not released")
                assert(ctx.storage:scroll(actor.replica.inventory, 1, -1, 5))
                return idle("storage_put")
            end,
            function()
                assert(inv:GetActiveItem().components.stackable:StackSize() == 30)
                assert(inv:GetItemInSlot(1).components.stackable:StackSize() == 30)
                -- Finish the isolated fixture's cursor state before testing container transfer.
                cursor = inv:GetActiveItem()
                inv:SetActiveItem(nil)
                cursor:Remove()
                chest = spawn("treasurechest")
                chest.Transform:SetPosition(actor.Transform:GetWorldPosition())
                chest.components.container:Open(actor)
                assert(ctx.storage:transfer(actor.replica.inventory, 1, 3))
                return idle("storage_transfer")
            end,
            function()
                assert(inv:GetItemInSlot(1).components.stackable:StackSize() == 27)
                assert(chest.components.container:GetItemInSlot(1).components.stackable:StackSize() == 3)
                assert(ctx.storage:transfer(chest.replica.container, 1))
                return idle("storage_return")
            end,
            function()
                assert(chest.components.container:IsEmpty())
                assert(inv:GetItemInSlot(1).components.stackable:StackSize() == 30)
                existing, cursor = inv:GetItemInSlot(1), spawn("flint")
                assert(existing.prefab == "flint")
                existing.components.stackable:SetStackSize(existing.components.stackable.maxsize - 2)
                cursor.components.stackable:SetStackSize(5)
                inv:GiveActiveItem(cursor)
                assert(ctx.storage:return_active())
                return idle("storage_cursor_return")
            end,
            function()
                local overflow
                assert(inv:GetActiveItem() == nil, "cursor return failed: " .. tostring(ctx.message))
                existing, overflow = inv:GetItemInSlot(1), inv:GetItemInSlot(2)
                assert(existing.components.stackable:IsFull() and overflow and overflow.components.stackable:StackSize() == 3,
                    "native partial AddAll must merge two and store the remaining three")
                inv:RemoveItemBySlot(1):Remove()
                inv:RemoveItemBySlot(2):Remove()
                chest:Remove()
                self.result.native_storage = true
                self.result.native_cursor_return = true
            end,
        }
    end

    local function drying()
        local ctx, actor, racks, unfinished, inventory
        return {
            function()
                local manager, house, score, origin, rack_point, kelp, meat
                ctx, actor = context()
                manager = assert(TheWorld.components.hermitcrab_relocation_manager)
                house = assert(manager:GetPearlsHouse(), "native Pearl home is missing")
                for _ = 1, 3 do
                    if house.components.pearldecorationscore then break end
                    assert(house.components.constructionsite):ForceCompletion(actor)
                    house = assert(manager:GetPearlsHouse(), "native home upgrade did not register its replacement")
                end
                score = assert(house.components.pearldecorationscore, "native home upgrades did not unlock decoration scoring")
                score:Enable()
                origin = house:GetPosition()
                rack_point = function()
                    for radius = 4, 12, 2 do
                        for step = 0, 15 do
                            local angle = step * math.pi / 8
                            local point = Vector3(origin.x + math.cos(angle) * radius, 0, origin.z + math.sin(angle) * radius)
                            if score:IsPointWithin(point) and TheWorld.Map:IsPassableAtPoint(point:Get())
                                and TheWorld.Map:IsDeployPointClear(point, nil, 2) then return point end
                        end
                    end
                    error("no clear ground inside the native Pearl home area")
                end
                racks = {}
                for i, prefab in ipairs({ "meatrack", "meatrack_hermit", "meatrack_hermit_multi" }) do
                    local point = rack_point()
                    local rack = spawn(prefab)
                    rack.Transform:SetPosition(point:Get())
                    assert(IsWithinHermitCrabArea(rack), "drying fixture is outside the native Pearl home area")
                    -- Position first: waking at the prefab origin can permanently abandon the nine-slot rack.
                    rack.entity:SetCanSleep(false)
                    racks[i] = rack
                end
                actor.Physics:Teleport(rack_point():Get())
                actor.sg:GoToState("idle")
                assert(racks[1].components.container:GiveItem(spawn("meat_dried"), 1))
                unfinished = spawn("meat")
                assert(racks[1].components.container:GiveItem(unfinished, 2))
                assert(racks[2].components.container:GiveItem(spawn("meat_dried"), 1))
                kelp = spawn("kelp_dried")
                kelp.components.driedsalticon:SetCollectsOnDried(true)
                assert(racks[3].components.container:GiveItem(kelp, 1))
                assert(kelp.components.driedsalticon.showicon:value(), "native salted kelp fixture is missing its public icon")
                meat = spawn("meat")
                meat.components.stackable:SetStackSize(12)
                assert(actor.components.inventory:GiveItem(meat, 1))
                ctx.drying = require("fieldkit/features/drying").new(ctx)
                assert(ctx.drying:start(racks, "meat"))

                return idle("harvest, salt and refill")
            end,
            function()
                for _, rack in ipairs(racks) do
                    assert(rack.components.container and rack.components.container:IsFull(), "drying refill failed: " .. tostring(ctx.message))
                    for slot = 1, rack.components.container:GetNumSlots() do
                        assert(rack.components.container:GetItemInSlot(slot).prefab == "meat")
                    end
                end
                assert(racks[1].components.container:GetItemInSlot(2) == unfinished,
                    "unfinished food must remain in its original slot")
                inventory = actor.components.inventory
                assert(inventory:Has("meat_dried", 2) and inventory:Has("kelp_dried", 1) and inventory:Has("saltrock", 1),
                    "native harvest or delayed salt collection failed: " .. tostring(ctx.message))
                assert(not racks[3].components.dryingracksaltcollector:HasSalt(), "salt was left behind")

                self.result.native_drying = true
            end,
        }
    end

    local function cooking()
        local ctx, actor, pot, cooking, meat, quick_cooking_job, batch_pots
        return {
            function()
                ctx, actor = context()
                pot = spawn("cookpot")
                pot.Transform:SetPosition(actor.Transform:GetWorldPosition())
                pot.components.container:Open(actor)
                for slot = 1, 4 do assert(pot.components.container:GiveItem(spawn("meat"), slot)) end
                cooking = require("fieldkit/features/cooking").new(ctx)
                assert(cooking:remember())
                pot.components.container:DestroyContents()
                meat = spawn("meat")
                meat.components.stackable:SetStackSize(4)
                assert(actor.components.inventory:GiveItem(meat, 1))
                assert(cooking:fill())
                return idle("fill")
            end,
            function()
                assert(pot.components.container:IsFull(), "native filling failed: " .. tostring(ctx.message))
                assert(actor.components.inventory:GetItemInSlot(1) == nil)
                for slot = 1, 4 do
                    local ingredient = pot.components.container:GetItemInSlot(slot)
                    assert(ingredient.prefab == "meat" and ingredient.components.stackable:StackSize() == 1)
                end
                self.result.native_fill = true
                assert(cooking:cook())
                return idle("cook")
            end,
            function()
                assert(pot.components.stewer:IsCooking(), "native cooking failed: " .. tostring(ctx.message))
                assert(not pot.replica.container:CanBeOpened())
                self.result.native_cook = true
                pot:Remove()
                pot = spawn("cookpot")
                pot.Transform:SetPosition(actor.Transform:GetWorldPosition())
                pot.components.container:Open(actor)
                for slot = 1, 4 do assert(actor.components.inventory:GiveItem(spawn("meat"), slot)) end
                assert(cooking:remember_inventory("inventory", 1), "native consecutive-slot recipe was not recorded")
                assert(pot.components.container:IsEmpty(), "recording a recipe moved its ingredients")
                for slot = 1, 4 do
                    local item = actor.components.inventory:GetItemInSlot(slot)
                    assert(item and item.prefab == "meat", "quick-cook fixture slot " .. slot .. " contains " .. tostring(item and item.prefab))
                    assert(ctx.store:get("recipes").cookpot[slot] == "meat")
                end
                assert(cooking:quick_repeat(pot), "native quick-repeat cooking rejected")
                quick_cooking_job = ctx.executor.current
                return idle("quick_cook")
            end,
            function()
                local point
                assert(quick_cooking_job.cooked == 1 and pot.components.stewer:IsCooking(),
                    "native inventory recipe did not fill and start cooking: " .. tostring(ctx.message))
                assert(not pot.replica.container:CanBeOpened())
                for slot = 1, 4 do assert(actor.components.inventory:GetItemInSlot(slot) == nil) end
                self.result.native_quick_cook = true
                pot:Remove()
                meat = spawn("meat")
                meat.components.stackable:SetStackSize(8)
                actor.components.inventory:GiveItem(meat)
                batch_pots = {}
                point = actor:GetPosition()
                for i = 1, 2 do
                    local pot = spawn("cookpot")
                    pot.Transform:SetPosition(point.x + (i == 1 and -2 or 2), 0, point.z)
                    batch_pots[i] = pot
                end
                assert(cooking:batch(batch_pots[1], 2))

                return idle("batch", function()
                    for _, pot in ipairs(batch_pots) do
                        if pot.components.stewer:IsCooking() then pot.components.stewer:LongUpdate(1000) end
                    end
                end)
            end,
            function()
                assert(actor.components.inventory:Has("bonestew", 2), "multi-pot loop failed: " .. tostring(ctx.message))
                for _, pot in ipairs(batch_pots) do pot:Remove() end
                self.result.native_batch_cook = true
            end,
        }
    end

    local function manufactured_crafting()
        local ctx, actor, station, job, finished
        return {
            function()
                ctx, actor = context()
                station = spawn("madscience_lab")
                local point = actor:GetPosition()
                station.Transform:SetPosition(point.x + 2, 0, point.z)
                station.entity:SetCanSleep(false)
                finished = 0
                ctx.scope:wrap(station.components.madsciencelab, "OnScienceWasMade", function(original)
                    return function(...)
                        finished = finished + 1
                        return original(...)
                    end
                end)
                for _, prefab in ipairs({ "froglegs", "goldnugget" }) do
                    local ingredient = spawn(prefab)
                    ingredient.components.stackable:SetStackSize(2)
                    assert(actor.components.inventory:GiveItem(ingredient))
                end
                actor.components.builder:EvaluateTechTrees()
                assert(actor.components.builder.current_prototyper == station, "native manufacturing station was not selected")
                ctx.construction = require("fieldkit/features/construction").new(ctx)
                assert(ctx.construction:craft("halloween_experiment_bravery", 2))
                job = ctx.executor.current
                return idle("manufactured crafting")
            end,
            function()
                assert(job.completed == 2 and finished == 2,
                    "native manufacturing did not finish both batches: " .. tostring(ctx.message))
                assert(not actor.components.inventory:Has("froglegs", 1)
                    and not actor.components.inventory:Has("goldnugget", 1), "native manufacturing did not consume both batches")
                local point = station:GetPosition()
                for _, entity in ipairs(TheSim:FindEntities(point.x, 0, point.z, 12)) do
                    if entity.prefab == "halloweenpotion_bravery_small" or entity.prefab == "halloweenpotion_bravery_large" then
                        self.entities[#self.entities + 1] = entity
                    end
                end
                self.result.native_manufactured_craft = true
            end,
        }
    end

    local function work_and_farm()
        local ctx, actor, target, pocket_darts, held_darts, rocks, point, a, b, area, build_points
        local plant_points, x, _, z, farm_centre, tx, ty, farm_area, can, poop, fork, terraform_before
        local fork_uses, terraform_job, cursor, drop_records, collecting, drop_points, drop_job
        return {
            function()
                local Placement, Work
                ctx, actor = context()
                Placement = require("fieldkit/features/placement")
                ctx.placement = Placement.new(ctx)
                ctx.construction = require("fieldkit/features/construction").new(ctx)
                target = spawn("flint")
                target.Transform:SetPosition(actor.Transform:GetWorldPosition())
                Work = require("fieldkit/features/work")
                ctx.work = Work.new(ctx)
                assert(ctx.work:pickup(target), "native pickup not selectable")

                return idle("pickup")
            end,
            function()
                assert(actor.components.inventory:Has("flint", 1), "native gathering produced no flint")
                self.result.native_pickup = true
                actor.components.inventory:GiveItem(spawn("pickaxe"))
                pocket_darts, held_darts = spawn("blowdart_pipe"), spawn("blowdart_pipe")
                assert(actor.components.inventory:GiveItem(pocket_darts))
                held_darts.components.stackable:SetStackSize(2)
                actor.components.inventory:Equip(held_darts)
                rocks = {}
                point = actor:GetPosition()
                for i = 1, 2 do
                    local rock = spawn("rock1")
                    rock.Transform:SetPosition(point.x + i * 2, 0, point.z)
                    rocks[i] = rock
                end
                assert(ctx.work:enqueue({ { target = rocks[1], button = "left" }, { target = rocks[2], button = "left" } }))
                return idle("work")
            end,
            function()
                local grass
                for _, rock in ipairs(rocks) do assert(not rock:IsValid(), "native mining did not finish: " .. tostring(ctx.message)) end
                assert(not held_darts:IsValid() and pocket_darts.components.stackable:StackSize() == 3,
                    "native tool switching did not preserve the merged hand stack")
                pocket_darts:Remove()
                self.result.native_equipment_stack = true
                self.result.native_tools = true
                grass = spawn("cutgrass")
                grass.components.stackable:SetStackSize(6)
                actor.components.inventory:GiveItem(grass)
                actor.components.builder:AddRecipe("rope")
                assert(ctx.construction:craft("rope", 2))
                return idle("craft")
            end,
            function()
                assert(actor.components.inventory:Has("rope", 2), "native repeated crafting failed: " .. tostring(ctx.message))
                self.result.native_craft = true
                assert(actor.components.inventory:GetEquippedItem(EQUIPSLOTS.HEAD) == nil)
                for _, ingredient in ipairs(AllRecipes.strawhat.ingredients) do
                    local item = spawn(ingredient.type)
                    item.components.stackable:SetStackSize(ingredient.amount * 2)
                    actor.components.inventory:GiveItem(item)
                end
                actor.components.builder:AddRecipe("strawhat")
                assert(ctx.construction:craft("strawhat", 2))
                return idle("craft_equipment")
            end,
            function()
                local recipe
                assert(actor.components.inventory:GetEquippedItem(EQUIPSLOTS.HEAD).prefab == "strawhat")
                assert(require("fieldkit/game/inventory").count(ctx, "strawhat") == 2,
                    "native auto-equipped craft failed: " .. tostring(ctx.message))
                self.result.native_craft_equipment = true
                for prefab, count in pairs({ cutgrass = 6, log = 4 }) do
                    local item = spawn(prefab)
                    item.components.stackable:SetStackSize(count)
                    actor.components.inventory:GiveItem(item)
                end
                recipe, point = AllRecipes.campfire, actor:GetPosition()
                for i = 0, 15 do
                    local angle = i * PI / 8
                    local first = point + Vector3(math.cos(angle) * 6, 0, math.sin(angle) * 6)
                    local second = first + Vector3(4, 0, 0)
                    if actor.replica.builder:CanBuildAtPoint(first, recipe, 0)
                        and actor.replica.builder:CanBuildAtPoint(second, recipe, 0) then a, b = first, second; break end
                end
                assert(a, "no native building area")
                actor.components.playercontroller:StartBuildPlacementMode(recipe)
                -- Opening a modal panel caches the native preview until the next controller update.
                actor.components.playercontroller:CancelPlacement(true)
                assert(actor.components.playercontroller.placer_recipe == nil)
                ctx.params.grid_size = 4
                ctx.placement:set_origin(a)
                area = rectangle(a, b)
                assert(ctx.construction:area(area, "build"))
                build_points = { a, b }
                return idle("build")
            end,
            function()
                local cones, p
                for _, point in ipairs(build_points) do
                    local found = false
                    for _, entity in ipairs(TheSim:FindEntities(point.x, 0, point.z, .2)) do
                        if entity.prefab == "campfire" then found = true; entity:Remove() end
                    end
                    assert(found, "native area building failed: " .. tostring(ctx.message))
                end
                actor.components.playercontroller:CancelPlacement()
                self.result.native_build = true
                cones = spawn("pinecone")
                cones.components.stackable:SetStackSize(2)
                actor.components.inventory:GiveActiveItem(cones)
                p = actor:GetPosition()
                for i = 0, 15 do
                    local angle = i * PI / 8
                    local first = p + Vector3(math.cos(angle) * 4, 0, math.sin(angle) * 4)
                    local second = first + Vector3(3, 0, 0)
                    if cones.replica.inventoryitem:CanDeploy(first, nil, actor, 0)
                        and cones.replica.inventoryitem:CanDeploy(second, nil, actor, 0) then a, b = first, second; break end
                end
                assert(a, "no native planting area")
                ctx.params.grid_size = 3
                ctx.placement:set_origin(a)
                area = rectangle(a, b)
                assert(ctx.construction:area(area, "deploy"))
                plant_points = { a, b }
                return idle("deploy")
            end,
            function()
                local hoe
                for _, point in ipairs(plant_points) do
                    local found = false
                    for _, entity in ipairs(TheSim:FindEntities(point.x, 0, point.z, .2)) do
                        if entity.prefab == "pinecone_sapling" then found = true; self.entities[#self.entities + 1] = entity end
                    end
                    assert(found, "native deployment failed: " .. tostring(ctx.message))
                end
                self.result.native_deploy = true
                assert(actor.components.inventory:GetActiveItem() == nil)
                x, _, z = TheWorld.Map:GetTileCenterPoint(actor.Transform:GetWorldPosition())
                farm_centre = Vector3(x, 0, z)
                for _, entity in ipairs(TheSim:FindEntities(x, 0, z, 4, nil, { "player", "CLASSIFIED", "FX", "INLIMBO" })) do
                    if entity ~= actor and not entity:HasTag("irreplaceable") then entity:Remove() end
                end
                tx, ty = TheWorld.Map:GetTileCoordsAtPoint(x, 0, z)
                self.farm_tile = { x = tx, y = ty, original = TheWorld.Map:GetTile(tx, ty) }
                TheWorld.Map:SetTile(tx, ty, WORLD_TILES.FARMING_SOIL)
                hoe = spawn("farm_hoe")
                actor.components.inventory:GiveItem(hoe)
                actor.components.inventory:Equip(hoe)
                ctx.params.grid_smart_spacing = true
                ctx.placement:reset_origin()
                area = rectangle(farm_centre, farm_centre, 1.8)
                assert(ctx.construction:area(area, "till"))
                farm_area = area
                return idle("till")
            end,
            function()
                local soils
                soils = TheSim:FindEntities(farm_centre.x, 0, farm_centre.z, 2.5, { "soil" }, { "NOCLICK", "NOBLOCK" })
                assert(#soils == 9, "native 3x3 till produced " .. #soils .. " usable soils: " .. tostring(ctx.message))
                for _, soil in ipairs(soils) do self.entities[#self.entities + 1] = soil end
                self.result.native_till = true
                can = spawn("wateringcan")
                can.components.finiteuses:SetUses(2)
                assert(actor.components.inventory:GiveItem(can))
                actor.components.inventory:Equip(can)
                assert(ctx.construction:area(farm_area, "water"), "native watering area rejected")
                return idle("water")
            end,
            function()
                assert(can.components.finiteuses:GetUses() == 1,
                    "native watering must consume exactly one use per tile: " .. tostring(ctx.message))
                self.result.native_water = true
                can:Remove()
                point = farm_centre
                tx, ty = TheWorld.Map:GetTileCoordsAtPoint(point:Get())
                -- Only the fixture reads server nutrient state; production uses item/action receipts.
                TheWorld.components.farming_manager:SetTileNutrients(tx, ty, 0, 0, 0)
                poop = spawn("poop")
                poop.components.stackable:SetStackSize(2)
                actor.components.inventory:GiveActiveItem(poop)
                assert(ctx.construction:area(farm_area, "fertilize"), "native fertilizing area rejected")
                return idle("fertilize")
            end,
            function()
                local nutrients, changed
                assert(poop:IsValid() and poop.components.stackable:StackSize() == 1,
                    "native fertilizing must consume exactly one item per tile: " .. tostring(ctx.message))
                tx, ty = TheWorld.Map:GetTileCoordsAtPoint(farm_centre:Get())
                nutrients = { TheWorld.components.farming_manager:GetTileNutrients(tx, ty) }
                changed = false
                for index, amount in ipairs(poop.components.fertilizer.nutrients) do
                    assert(nutrients[index] >= amount, "native fertilizer did not increase soil nutrients")
                    changed = changed or nutrients[index] > 0
                end
                assert(changed, "native fertilizer changed no nutrients")
                self.result.native_fertilize = true
                actor.components.inventory:SetActiveItem(nil)
                poop:Remove()
                fork = spawn("pitchfork")
                assert(actor.components.inventory:GiveItem(fork))
                actor.components.inventory:Equip(fork)
                terraform_before = TheWorld.Map:GetTileAtPoint(farm_centre:Get())
                fork_uses = fork.components.finiteuses:GetUses()
                assert(ctx.construction:area(farm_area, "terraform"), "native turf-digging area rejected")
                terraform_job = ctx.executor.current
                return idle("terraform")
            end,
            function()
                local points
                assert(terraform_job.completed == 1
                    and TheWorld.Map:GetTileAtPoint(farm_centre:Get()) ~= terraform_before,
                    "native turf-digging did not change its tile: " .. tostring(ctx.message))
                assert(fork.components.finiteuses:GetUses() < fork_uses,
                    "native turf-digging did not consume tool durability")
                fork:Remove()
                self.result.native_terraform = true
                cursor = spawn("twigs")
                cursor.components.stackable:SetStackSize(3)
                actor.components.inventory:GiveActiveItem(cursor)
                a, b = farm_centre + Vector3(-1, 0, 0), farm_centre + Vector3(1, 0, 0)
                ctx.params.grid_size = 2
                ctx.placement:set_origin(a)
                area = rectangle(a, b)
                points = assert(ctx.placement:points(area, nil, 32, cursor, nil, "drop"))
                assert(#points == 2, "native drop fixture did not project to two grid points")
                for index, expected in ipairs({ a, b }) do
                    local actual = ctx.placement:resolve(points[index])
                    assert(actual and actual:DistSq(expected) < .001 ^ 2,
                        "native drop fixture grid differs from requested point: " .. tostring(actual) .. " vs " .. tostring(expected))
                end
                drop_records = {}
                collecting = true
                ctx.scope:listen("dropitem", function(_, data)
                    if collecting and data.item.prefab == "twigs" then
                        data.item.entity:SetCanSleep(false)
                        drop_records[#drop_records + 1] = { entity = data.item, point = data.item:GetPosition() }
                        self.entities[#self.entities + 1] = data.item
                    end
                end)
                assert(ctx.construction:area(area, "drop"), "native one-per-grid drop rejected")
                drop_points, drop_job = { a, b }, ctx.executor.current
                return idle("drop")
            end,
            function()
                collecting = false
                assert(drop_job.completed == 2, "native dropping did not finish two points: " .. tostring(ctx.message))
                assert(actor.components.inventory:GetActiveItem() == cursor
                    and cursor.components.stackable:StackSize() == 1, "native dropping did not preserve one cursor item")
                assert(#drop_records == 2 and drop_records[1].entity ~= drop_records[2].entity,
                    "native dropping did not emit exactly two distinct items")
                return await("drop landing", function()
                    for _, record in ipairs(drop_records) do
                        assert(record.entity:IsValid(), "native dropped item disappeared")
                        if not record.entity.components.inventoryitem.is_landed then return false end
                    end
                    return true
                end, 5)
            end,
            function()
                local received, total
                received, total = {}, 0
                for _, record in ipairs(drop_records) do
                    local position, matched = record.entity:GetPosition()
                    for index, point in ipairs(drop_points) do
                        if (position.x - point.x) ^ 2 + (position.z - point.z) ^ 2 <= .5 ^ 2 then
                            assert(not matched and not received[index], "native drop points overlap or share a pile")
                            assert((record.point.x - point.x) ^ 2 + (record.point.z - point.z) ^ 2 < .01 ^ 2,
                                "native action did not initially drop at its requested point")
                            matched, received[index] = index, true
                        end
                    end
                    local count = record.entity.components.stackable:StackSize()
                    assert(matched and count == 1, "native grid point did not receive exactly one item")
                    total = total + count
                end
                assert(received[1] and received[2] and total == 2, "native drop total or grid coverage is incorrect")
                actor.components.inventory:SetActiveItem(nil)
                cursor:Remove()
                self.result.native_drop = true
            end,
        }
    end

    local function fishing()
        local ctx, actor, pond, rod, fish_collections, rod_uses, pond_fish, fishing_job, cancel_rod_uses
        local fishingrod
        return {
            function()
                local destination
                ctx, actor = context()
                destination = clear_segment(actor, 5)
                ctx.work = require("fieldkit/features/work").new(ctx)
                pond = spawn("pond")
                pond.Transform:SetPosition(destination:Get())
                pond.entity:SetCanSleep(false)
                pond.components.childspawner:SetMaxChildren(0)
                pond.components.childspawner:StopRegen()
                rod = spawn("fishingrod")
                rod.entity:SetCanSleep(false)
                rod.components.fishingrod:SetWaitTimes(.5, .5)
                rod.components.fishingrod:SetStrainTimes(0, 5)
                assert(actor.components.inventory:GiveItem(rod))
                actor.components.inventory:Equip(rod)
                fish_collections = 0
                ctx.scope:listen("fishingcatch", function()
                    local fish = rod.components.fishingrod.caughtfish
                    assert(fish and fish:IsValid(), "native catch event lost its fish")
                    -- Spawned catches have no connected client's wake radius in this fixture.
                    fish.entity:SetCanSleep(false)
                    self.entities[#self.entities + 1] = fish
                end)
                ctx.scope:listen("fishingcollect", function(_, data)
                    assert(data and data.fish and data.fish:IsValid(), "native collection event lost its fish")
                    fish_collections = fish_collections + 1
                end)
                rod_uses = rod.components.finiteuses:GetUses()
                pond_fish = pond.components.fishable.fishleft
                assert(ctx.work:start(pond, "FISH"), "native repeated fishing rejected")
                fishing_job = ctx.executor.current
                return idle("two fishing rounds")
            end,
            function()
                assert(fishing_job.caught == 2 and fish_collections == 2,
                    "native repeated fishing did not collect twice: " .. tostring(ctx.message))
                assert(rod.components.finiteuses:GetUses() == rod_uses - 2)
                assert(pond.components.fishable.fishleft == pond_fish - 2)
                assert(actor.components.inventory:Has("pondfish", 2), "native catch pickup did not retain both fish")
                assert(not rod.components.fishingrod:IsFishing())

                return delay(1)
            end,
            function()
                local cancelled
                assert(fish_collections == 2 and not ctx.executor:is_running()
                    and not rod.components.fishingrod:IsFishing(), "finite fishing restarted after its catch limit")
                self.result.native_fishing = true
                rod.components.fishingrod:SetStrainTimes(4, 4)
                cancel_rod_uses = rod.components.finiteuses:GetUses()
                assert(ctx.work:start(pond, "FISH"), "native hook-cancellation fishing rejected")

                cancelled = false
                return await("hook before cancellation", function()
                    assert(cancelled or ctx.executor:is_running(), "fishing stopped before hook")
                    return cancelled
                end, 15, function()
                    local fishingrod = rod.components.fishingrod
                    if fishingrod:HasHookedFish() and not fishingrod:HasCaughtFish() then
                        local controller = actor.components.playercontroller
                        controller.remote_controls[CONTROL_FORCE_TRADE] = true
                        assert(controller:IsControlPressed(CONTROL_FORCE_TRADE), "native fixture must hold the drop modifier")
                        ctx.executor:cancel("cancelled")
                        controller.remote_controls[CONTROL_FORCE_TRADE] = nil
                        cancelled = true
                    end
                end)
            end,
            function()
                return delay(4.5, function() assert(rod:IsValid(), "cancellation destroyed the rod") end)
            end,
            function()
                assert(rod:IsValid(), "native fishing cancellation destroyed the rod")
                fishingrod = rod.components.fishingrod
                assert(not ctx.executor:is_running() and not fishingrod:IsFishing() and fishingrod.target == nil
                    and not fishingrod:HasHookedFish() and not fishingrod:HasCaughtFish() and fishingrod.fishtask == nil,
                    "native fishing cancellation left an active target or delayed task")
                assert(not actor.sg:HasStateTag("fishing") and not actor.sg:HasStateTag("catchfish"),
                    "native fishing cancellation did not leave the fishing state")
                assert(rod.components.inventoryitem.owner == actor
                    and actor.components.inventory:GetEquippedItem(EQUIPSLOTS.HANDS) ~= rod,
                    "native fishing cancellation did not retain the unequipped rod in inventory")
                assert(fish_collections == 2 and rod.components.finiteuses:GetUses() == cancel_rod_uses,
                    "native cancellation collected or consumed another fish")
                pond:Remove()
                rod:Remove()
                self.result.native_fishing_cancel = true
            end,
        }
    end

    local function movement()
        local ctx, actor, start, destination, inventory, cane, tool, creature, travel, cane_observed
        local observe_cane, reveal, route_job, appended, empty_hand_job
        return {
            function()
                local backpack
                ctx, actor = context()
                start, destination = actor:GetPosition(), clear_segment(actor, 6)
                inventory = actor.components.inventory
                backpack, cane, tool = spawn("backpack"), spawn("cane"), spawn("axe")
                inventory:Equip(backpack)
                backpack.components.container:Open(actor)
                assert(backpack.components.container:GiveItem(cane, 1))
                assert(inventory:GiveItem(tool))
                inventory:Equip(tool)
                creature = spawn("pigman")
                creature.entity:SetCanSleep(false)
                creature.Physics:Teleport(destination:Get())
                creature:StopBrain()
                creature.components.locomotor:Stop()
                creature.sg:GoToState("idle")
                ctx.config.travel, ctx.params.follow_mode, ctx.params.follow_distance = true, "trail", 1.5
                ctx.params.travel_cane = true
                travel = require("fieldkit/features/travel").new(ctx)
                assert(travel:can_follow(creature), "native creature is not a follow candidate")
                assert(travel:can_push(creature), "native creature collision pair is not pushable")
                assert(travel:follow(creature), "native trajectory follow rejected")
                cane_observed = false
                observe_cane = function()
                    cane_observed = cane_observed or inventory:GetEquippedItem(EQUIPSLOTS.HANDS) == cane
                end
                return await("follow and restore tool", function()
                    observe_cane()
                    assert(ctx.executor:is_running(), "native follow stopped: " .. tostring(ctx.message))
                    local gap = math.sqrt(actor:GetDistanceSqToInst(creature)) - actor.Physics:GetRadius() - creature.Physics:GetRadius()
                    -- Restoring the tool happens after waiting, whose hysteresis allows .5m.
                    if gap > ctx.params.follow_distance + .5 then return false end
                    assert(actor:GetPosition():DistSq(start) > 1 and gap > .8, "follow movement or collision gap is incorrect")
                    assert(cane_observed, "native follow did not borrow the backpack cane")
                    return inventory:GetEquippedItem(EQUIPSLOTS.HANDS) == tool
                end, 15)
            end,
            function()
                local push_start
                ctx.executor:cancel("cancelled")
                self.result.native_follow, self.result.native_cane_follow = true, true
                push_start = creature:GetPosition()
                assert(travel:push(creature), "native push rejected")
                return await("push displacement", function()
                    assert(ctx.executor:is_running(), "native push stopped: " .. tostring(ctx.message))
                    return creature:GetPosition():DistSq(push_start) >= .5 ^ 2
                end, 15)
            end,
            function()
                local point
                ctx.executor:cancel("cancelled")
                assert(actor.components.locomotor.bufferedaction == nil, "movement action survived cancellation")
                self.result.native_push = true
                reveal = function(a, b)
                    for i = 0, 10 do
                        local point = a + (b - a) * (i / 10)
                        actor.player_classified.MapExplorer:RevealArea(point:Get())
                    end
                    assert(actor:CanSeePointOnMiniMap(b:Get()), "native map initialization failed")
                end
                point = actor:GetPosition()
                reveal(point, start)
                cane_observed = false
                assert(travel:walk(start), "native explored route rejected: " .. tostring(ctx.message))
                route_job = ctx.executor.current
                appended = start + (point - start) * .5
                assert(actor:CanSeePointOnMiniMap(appended:Get()), "appended destination was not revealed")
                return await("route underway", function() return true end)
            end,
            function()
                observe_cane()
                assert(ctx.executor.current == route_job, "route ended before append")
                assert(travel:add_waypoint(appended), "native active route append rejected")
                assert(ctx.executor.current == route_job and #route_job.waypoints == 2, "append replaced the task or lost a waypoint")
                return idle("two waypoints", observe_cane)
            end,
            function()
                local previous
                observe_cane()
                assert(route_job.completed == 2 and actor:GetPosition():DistSq(appended) < .6 ^ 2,
                    "native route did not visit both destinations: " .. tostring(ctx.message))
                assert(cane_observed and inventory:GetEquippedItem(EQUIPSLOTS.HANDS) == tool,
                    "native route did not borrow a cane and restore the original tool")
                self.result.native_route, self.result.native_route_append, self.result.native_cane_restore = true, true, true
                creature:Remove()
                previous = inventory:Unequip(EQUIPSLOTS.HANDS)
                assert(previous == tool and inventory:GiveItem(previous))
                actor.Physics:Teleport(start:Get())
                reveal(start, destination)
                cane_observed = false
                assert(travel:walk(destination))
                empty_hand_job = ctx.executor.current
                return idle("empty hands", observe_cane)
            end,
            function()
                local heavy
                observe_cane()
                assert(empty_hand_job.completed == 1 and cane_observed and inventory:GetEquippedItem(EQUIPSLOTS.HANDS) == nil,
                    "native route did not restore empty hands: " .. tostring(ctx.message))
                assert(cane.components.inventoryitem:GetGrandOwner() == actor, "native cane was dropped on return")
                self.result.native_cane_empty_hand = true
                actor.Physics:Teleport(start:Get())
                heavy = spawn("sculpture_rooknose")
                inventory:Equip(heavy)
                assert(inventory:IsHeavyLifting() and inventory:GetEquippedItem(EQUIPSLOTS.BODY) == heavy)
                cane_observed = false
                assert(travel:walk(destination))
                return delay(1, function()
                    observe_cane()
                    assert(not cane_observed and inventory:GetEquippedItem(EQUIPSLOTS.BODY) == heavy,
                        "automatic cane switching dropped a native heavy object")
                end)
            end,
            function()
                assert(ctx.executor:is_running(), "heavy-object route stopped before observation")
                ctx.executor:cancel("cancelled")
                self.result.native_cane_heavy = true
            end,
        }
    end

    function self:run()
        self.scenes = {
            { "primitives", primitives }, { "observations", observations }, { "inventory", inventory },
            { "drying", drying }, { "cooking", cooking }, { "manufactured crafting", manufactured_crafting },
            { "work and farm", work_and_farm },
            { "fishing", fishing }, { "movement", movement },
        }
        self.scene_index, self.step_index = 0, 1
        return true
    end

    function self:poll()
        if self.waiting and self.waiting.before_tick then self.waiting.before_tick() end
        if self.ctx then self.ctx.executor:tick() end
        if self.waiting then
            assert(GetTime() < self.waiting.deadline, "timed out: " .. self.waiting.label)
            if not self.waiting.predicate() then return end
            self.waiting = nil
        end
        while true do
            if not self.steps or not self.steps[self.step_index] then
                self:close()
                self.scene_index = self.scene_index + 1
                local scene = self.scenes[self.scene_index]
                if not scene then self.result.cleanup = true; return self.result end
                self.scene, self.steps, self.step_index = scene[1], scene[2](), 1
                print("fieldkit-native scene: " .. self.scene)
            end
            self.waiting = self.steps[self.step_index]()
            self.step_index = self.step_index + 1
            if self.waiting then return end
        end
    end

    return self
end
