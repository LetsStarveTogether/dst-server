local T = require("tests.client_mod.support")
T.case("awareness contracts", function()
    local Ranges = require("fieldkit/rules/ranges")
    local rendered, closed
    package.loaded["fieldkit/ui/indicators"] = { new = function()
        return { render = function(_, frame) rendered = frame end, close = function() closed = true end }
    end }
    local Awareness = require("fieldkit/features/awareness")
    local Scope = require("fieldkit/core/scope")

    local tuning = { FIRE_DETECTOR_RANGE = 15, WINONA_BATTERY_RANGE = 6.6, WINONA_ENGINEERING_FOOTPRINT = 1.6,
        WINONA_CATAPULT_TARGETING_MIN_RANGE = 5.5, WINONA_CATAPULT_MIN_RANGE = 6, WINONA_CATAPULT_MAX_RANGE = 15,
        EYETURRET_RANGE = 15, PANFLUTE_SLEEPRANGE = 15, HORN_RANGE = 25, HOUNDWHISTLE_RANGE = 25,
        TRAP_TEETH_RADIUS = 1.5, BEEMINE_RADIUS = 3, WATERPUMP = { MAXRANGE = 20 } }
    T.each("public range radii", {
        { name = "firesuppressor", radius = 15 }, { name = "lightning_rod", radius = 40 },
        { name = "winona_battery_low", radius = 5 }, { name = "winona_catapult", radius = 5.5 },
        { name = "eyeturret", radius = 18 },
    }, function(case) assert(Ranges.describe(tuning, case.name)[1].radius == case.radius) end)
    assert(Ranges.describe(tuning, "panflute")[1].owner)
    T.each("unsupported ranges", { "icebox", "cookpot" }, function(prefab) assert(#Ranges.describe(tuning, prefab) == 0) end)
    T.each("invalid native radius", { {}, { value = 0 / 0 }, { value = math.huge }, { value = 0 }, { value = -1 } }, function(case)
        assert(#Ranges.describe({ FIRE_DETECTOR_RANGE = case.value }, "firesuppressor") == 0)
    end)
    T.each("native range contracts", {
        { "prefabs/firesuppressor", "local PLACER_SCALE = 1.55" }, { "tuning", "FIRE_DETECTOR_RANGE = 15" },
        { "components/weather", "LIGHTNINGSTRIKE_SEARCH_RANGE = 40" },
        { "prefabs/lightningrod", 'Prefab("lightning_rod",' }, { "prefabs/beequeen", "'hivehat'" },
    }, function(case) assert(T.source(case[1]):find(case[2], 1, true), "recheck native range rules after game changes") end)

    local now, entities, hover, slot, placement, sounds, ready = 0, {}, nil, nil, nil, 0, true
    local next_guid = 0
    local function entity(prefab, x, z, tags)
        next_guid = next_guid + 1
        return T.entity({ prefab = prefab, GUID = next_guid, x = x, z = z, tags = tags })
    end
    local player = entity("wilson", 50, 50)
    local tasks = T.tasks(player)
    local function tick() tasks[1].fn() end
    local ctx = { player = player, config = { range_hints = true, boss_indicators = true, loot_hints = true, loot_sound = true }, hud = {},
        placement = { context = function() return placement end }, G = {
            TUNING = tuning, GetTime = function() return now end,
            CanEntitySeeTarget = function(_, e) return not e.dark end,
            TheInput = { GetWorldEntityUnderMouse = function() return hover end,
                GetHUDEntityUnderMouse = function() return slot end },
            TheSim = { GetScreenPos = function(_, x, y, z) return x, z end,
                GetScreenSize = function() return 100, 100 end,
                FindEntities = function() return entities end },
            TheFocalPoint = { SoundEmitter = { PlaySound = function() sounds = sounds + 1 end } },
        } }
    function ctx:text(_, en) return en end
    function ctx:ready() return ready end
    ctx.scope = Scope.new(player, error)
    local boss = entity("deerclops", 110, 50, { epic = true })
    local gem = entity("greengem", 45, 50)
    gem.replica.inventoryitem = { IsHeld = function() return gem.held end }
    entities = { boss, gem }
    local feature = Awareness.new(ctx)
    assert(#rendered.bosses == 0, "host simulation cannot discover a never-seen off-screen boss")
    assert(#rendered.loot == 1 and not rendered.loot[1].label:find("newly") and sounds == 0, "baseline loot is not a new drop")
    boss.x, now = 70, 1; tick()
    assert(#rendered.bosses == 0, "on-screen bosses need no edge indicator")
    boss.x, now = 150, 2; tick()
    assert(#rendered.bosses == 1 and rendered.bosses[1].point.x == 70, "off-screen movement cannot update observation")
    boss.x, now = 400, 10; tick()
    assert(rendered.bosses[1].point.x == 70 and rendered.bosses[1].label:find("9s"))
    gem.held = true; tick(); assert(#rendered.loot == 0, "picked-up items lose local hints immediately")
    local newgem = entity("yellowgem", 60, 60)
    newgem.replica.inventoryitem = { IsHeld = function() return false end }
    entities[#entities + 1] = newgem
    now = 11; tick()
    assert(#rendered.loot == 1 and rendered.loot[1].label:find("newly observed") and sounds == 0)
    now = 12; tick(); assert(sounds == 0, "a newly observed item without drop evidence cannot alert")
    now = 47; tick(); assert(#rendered.bosses == 0, "last-seen directions expire")
    do
        local sack, cap = entity("klaus_sack", 60, 50), entity("toadstool_cap", 65, 50)
        local cap_state = 1
        cap._state = { value = function() return cap_state end }
        entities, ctx.config.loot_hints = { sack, cap }, false
        tick()
        sack.x, cap.x = 150, 160
        tick()
        assert(#rendered.bosses == 2, "observed boss structures work without an epic tag or loot hints")
        sack.x, cap.x, cap_state = 60, 65, 0
        tick()
        sack.removed, cap.x = true, 160
        tick()
        assert(#rendered.bosses == 0, "observing an empty cap clears its previous boss marker")
        ctx.config.loot_hints = true
    end
    local fracture = entity("rock_break_fx", 50, 50, { FX = true, NOCLICK = true })
    local fossil = entity("fossil_piece", 50, 50)
    fossil.replica.inventoryitem = { IsHeld = function() return false end }
    entities = { fracture, fossil }
    assert(not Awareness.observed(ctx, fracture) and Awareness.observed(ctx, fracture, true))
    tick(); assert(#rendered.loot == 1 and rendered.loot[1].glow and sounds == 1,
        "visible NOCLICK break effects reach loot evidence without exposing off-screen simulation")
    ready = false; tick(); ready = true; tick()
    assert(#rendered.loot == 0, "returning from an inactive HUD does not restart an old loot effect")

    local detector = entity("firesuppressor", 50, 50)
    hover = detector; tick()
    assert(#rendered.ranges == 1 and rendered.ranges[1].rules[1].kind == "detection")
    assert(feature:toggle_pin(detector)); hover = nil; tick()
    assert(#rendered.ranges == 1 and feature:status():find("1/64"))
    detector.x, tuning.FIRE_DETECTOR_RANGE = 110, 20
    tick()
    assert(#rendered.ranges == 1 and rendered.ranges[1].point.x == 50 and rendered.ranges[1].rules[1].radius == 15
        and rendered.ranges[1].label:find("last observed"), "off-screen circles retain only their last observed geometry")
    detector.x = 60; tick()
    assert(rendered.ranges[1].point.x == 60 and rendered.ranges[1].rules[1].radius == 20
        and not rendered.ranges[1].label:find("last observed"), "returning targets refresh their actual center and rules")
    tuning.FIRE_DETECTOR_RANGE = 15
    detector.hidden = true; tick(); assert(#rendered.ranges == 0 and feature:status() == "")
    detector.hidden = false
    for i = 1, 6 do assert(feature:toggle_pin(entity("lightning_rod", i, 40))) end
    assert(feature:status():find("6/64")); feature:clear_pins(); tick(); assert(#rendered.ranges == 0)

    -- Same-kind pins are bounded independently of other nearby objects, and never discover off-screen entities.
    local rods = {}
    for i = 1, 70 do rods[i] = entity("lightning_rod", 45 + i / 10, 50) end
    local offscreen = entity("lightning_rod", 200, 50)
    entities = { offscreen }
    for _, rod in ipairs(rods) do entities[#entities + 1] = rod end
    assert(feature:toggle_group(rods[1], true)); tick()
    assert(#rendered.ranges == 64 and feature:status():find("64/64"))
    assert(feature:toggle_group(rods[1], false)); tick(); assert(#rendered.ranges == 0)
    hover = rods[1]
    assert(feature:toggle_pin(hover)); tick(); assert(#rendered.ranges == 1)
    assert(feature:toggle_pin(hover)); tick(); assert(#rendered.ranges == 0, "unpinning must not immediately reveal a hover circle")
    hover = nil; tick(); hover = rods[1]; tick(); assert(#rendered.ranges == 1)
    assert(feature:toggle_pin(hover)); now = now + 120; tick()
    assert(#rendered.ranges == 1 and feature:status():find("1/64"), "pins persist until explicitly cleared")
    feature:clear_pins(); tick(); assert(#rendered.ranges == 0)
    hover = nil
    assert(not feature:custom(0 / 0) and not feature:custom(math.huge) and not feature:custom(0))
    assert(feature:custom("9", player)); tick()
    assert(rendered.ranges[1].rules[1].radius == 9 and rendered.ranges[1].label:find("Measured"))
    assert(feature:custom_group(5, rods[1])); tick(); assert(#rendered.ranges == 64)
    feature:clear_pins(); tick(); assert(#rendered.ranges == 0)

    tuning.STORAGE_ROBOT_WORK_RADIUS, tuning.WAGDRONE_ROLLING_WORK_RADIUS = 20, 20
    local robot = entity("storage_robot", 70, 50)
    robot._originx, robot._originz = { value = function() return 40 end }, { value = function() return 30 end }
    function robot:GetCurrentPlatform() end
    assert(feature:toggle_pin(robot)); tick()
    assert(rendered.ranges[1].point.x == 40 and rendered.ranges[1].point.z == 30,
        "replicated robot origin determines its work area while the robot is moving")
    robot.x, robot._originx = 150, { value = function() error("unseen work origin must not be read") end }
    tick(); assert(rendered.ranges[1].point.x == 40, "off-screen range rules use the captured origin")
    feature:clear_pins()
    local drone = entity("wagdrone_rolling", 50, 50)
    assert(feature:toggle_pin(drone)); drone.x = 60; tick()
    assert(rendered.ranges[1].point.x == 50 and rendered.ranges[1].rules[1].precision == "estimate",
        "an unreplicated work origin remains a fixed, explicitly estimated reference")
    feature:clear_pins()
    local boat = entity("boat", 10, 0)
    boat.entity.WorldToLocalSpace = function(_, x, y, z) return x - boat.x, y, z end
    boat.entity.LocalToWorldSpace = function(_, x, y, z) return x + boat.x, y, z end
    function drone:GetCurrentPlatform() return boat end
    assert(feature:toggle_pin(drone)); drone.x, boat.x = 80, 20; tick()
    assert(rendered.ranges[1].point.x == 70, "fixed references on a boat follow its platform, not the walking robot")
    drone.x, boat.x = 150, 90; tick()
    assert(rendered.ranges[1].point.x == 70, "an unseen platform cannot move the observed reference")
    feature:clear_pins()
    drone.x = 60
    assert(feature:toggle_pin(drone)); drone.x = 150; tick()
    assert(rendered.ranges[1].point.x == 60, "pin creation captures a reference before its first periodic update")
    feature:clear_pins()

    placement = { point = { x = 8, z = 9 }, recipe = { product = "firesuppressor" }, kind = "build" }
    tick(); assert(rendered.ranges[1].point.x == 8, "placement owns the actual snapped center")
    placement = nil
    player.components = { playercontroller = { deployplacer = entity("waterpump_item_placer", 20, 30) } }
    ctx.placement = nil
    tick(); assert(rendered.ranges[1].point.x == 20 and rendered.ranges[1].rules[1].radius == 20,
        "native deployment previews keep working with geometry assistance disabled")
    player.components.playercontroller.deployplacer = nil
    local kit = entity("waterpump_item", 999, 999)
    slot = { widget = { parent = { name = "ItemSlot", tile = { item = kit } } } }
    tick(); assert(rendered.ranges[1].point.x == player.x, "inventory previews also support deployable objects, not only owner tools")
    ctx.config.range_inventory = false; tick(); assert(#rendered.ranges == 0)
    ctx.config.range_inventory = true
    local flute = entity("panflute", 999, 999)
    slot = { widget = { parent = { name = "ItemSlot", tile = { item = flute } } } }
    tick(); assert(rendered.ranges[1].point.x == player.x and rendered.ranges[1].point.z == player.z)
    ctx.config.range_hover_modifier = "alt"
    ctx.G.KEY_ALT = 308
    ctx.G.TheInput.IsKeyDown = function() return false end
    tick(); assert(#rendered.ranges == 1, "world hover modifiers do not suppress inventory previews")
    slot = nil; hover = detector
    tick(); assert(#rendered.ranges == 0, "world hover has its own modifier gate")
    ctx.G.TheInput.IsKeyDown = function() return true end
    tick(); assert(#rendered.ranges == 1)
    ctx.config.range_hover_modifier = "off"
    tick(); assert(#rendered.ranges == 0, "world hover can be disabled independently")
    ctx.config.range_hover_modifier = "none"
    hover = nil
    ctx.G.EQUIPSLOTS = { HANDS = "hands" }
    player.replica.inventory = { GetActiveItem = function() return flute end, GetEquippedItem = function() end }
    tick(); assert(rendered.ranges[1].point.x == player.x, "cursor instruments retain their user-centered range")
    player.replica.inventory = nil
    tuning.BATTLESONG_ATTACH_RADIUS, tuning.BATTLESONG_DETACH_RADIUS = 16, 20
    for _, prefab in ipairs({ "book_birds", "battlesong_durability", "waterpump_item" }) do
        local item = entity(prefab, 50, 50)
        slot = { widget = { name = "ItemSlot", tile = { item = item } } }
        tick(); assert(#rendered.ranges == 1, "built-in books, songs and objects share inventory previews")
        slot = nil; hover = item
        tick(); assert(#rendered.ranges == 1, "built-in rules are available on world hover")
        assert(feature:toggle_pin(item)); tick(); assert(#rendered.ranges == 1, "pins replace the matching hover circle")
        feature:clear_pins(); hover = nil
        player.replica.inventory = { GetActiveItem = function() return item end, GetEquippedItem = function() end }
        tick(); assert(#rendered.ranges == 1, "active cursor hints share the built-in rules")
        player.replica.inventory = nil
    end
    ctx.config.range_hover_modifier = "off"
    player.components.playercontroller.deployplacer = entity("waterpump_item_placer", 20, 30)
    tick(); assert(#rendered.ranges == 1, "native placement previews are independent of world hover")
    player.components.playercontroller.deployplacer = nil
    ctx.config.range_hover_modifier = "none"
    entities = {}
    for i = 1, 30 do
        local item = entity("yellowgem", 30 + i, 40)
        item.replica.inventoryitem = { IsHeld = function() return false end }
        entities[i] = item
    end
    tick(); assert(#rendered.loot == 24, "loot markers have a fixed budget")
    entities = {}
    for i = 1, 300 do
        local item = entity("cutgrass", 50, 50, { _inventoryitem = true })
        item.replica.inventoryitem = { IsHeld = function() return false end }
        entities[i] = item
    end
    for _, loot_enabled in ipairs({ false, true }) do
        ctx.config.loot_hints = loot_enabled
        local crowded_boss = entity("bearger", 80, 50, { epic = true })
        entities[301] = crowded_boss
        now = now + 1; tick()
        assert(#rendered.bosses == 0, "nearby clutter cannot misclassify an on-screen boss as off-screen")
        crowded_boss.x = 150
        now = now + 1; tick()
        assert(#rendered.bosses == 1 and rendered.bosses[1].point.x == 80,
            "first sight of a boss survives a full independent loot budget")
        crowded_boss.x = 85
        now = now + 1; tick()
        assert(#rendered.bosses == 0, "an already tracked boss returning on-screen is always suppressed")
        crowded_boss.removed = true
        tick()
    end
    ready = false; tick(); assert(#rendered.ranges + #rendered.loot + #rendered.bosses == 0)
    ctx.scope:close(); assert(closed and tasks[1].cancelled and #ctx.scope.cleanups == 0)

    -- The owned native art is calibrated against Klei's own 15-unit helper, never the target's AnimState.
    local created = {}
    local widget = require("tests.client_mod.support.widgets")
    widget.install()
    package.loaded["fieldkit/ui/indicators"] = nil
    ctx.hud = widget()
    ctx.G.ANIM_ORIENTATION = { OnGround = 1 }
    ctx.G.CreateEntity = function()
        local e = entity("owned_marker")
        e.tags = {}
        function e:AddTag(tag) self.tags[tag] = true end
        function e:Remove() self.removed = true end
        e.entity.AddTransform, e.entity.SetCanSleep = function() end, function() end
        e.Transform.SetPosition = function() end
        e.Transform.SetScale = function(_, scale) e.scale = scale end
        e.entity.AddAnimState = function()
            e.AnimState = setmetatable({}, { __index = function(_, key)
                if key == "SetBank" then return function(_, value) e.bank = value end end
                if key == "SetMultColour" then return function(_, ...) e.color = { ... } end end
                return function() end
            end })
        end
        e.entity.AddLabel = function() e.Label = setmetatable({}, { __index = function() return function() end end }) end
        created[#created + 1] = e
        return e
    end
    local render = require("fieldkit/ui/indicators").new(ctx)
    render:render({ ranges = { { key = "test", point = { x = 0, z = 0 }, label = "test",
        rules = { { radius = 15, color = { 1, .3, .2, .7 } } } } } })
    assert(#created == 2 and created[2].scale == 1.55 and created[2].bank == "firefighter_placement")
    assert(created[2].color[1] == 1 and created[2].color[2] == .3, "each range keeps its rule color")
    assert(created[2].persists == false and created[2].tags.NOCLICK and created[2].Network == nil)
    render:render({}); assert(created[1].removed and created[2].removed)
    local guarded_loot = entity("walrushat", 50, 50)
    guarded_loot.AnimState = setmetatable({}, { __index = function() error("real item visuals must remain untouched") end })
    render:render({ loot = { { key = "loot:test", point = { x = 50, z = 50 }, entity = guarded_loot,
        label = "likely drop", glow = true } } })
    assert(#created == 4 and created[4].bank == "lavaarena_item_pickup_fx" and created[4].Network == nil,
        "loot glow is its own local animation")
    render:render({ loot = { { key = "loot:test", point = { x = 50, z = 50 }, entity = guarded_loot,
        label = "observed", glow = false } } })
    assert(created[4].removed and not created[3].removed, "observed items retain labels without a drop glow")
    render:render({}); assert(created[3].removed)
    ctx.G.GetIndicatorLocationAndAngle = function(_, x, z) assert(x == 70 and z == 50); return 10, 20, 45 end
    render:render({ bosses = { { key = "boss", point = { x = 70, z = 50 }, label = "last seen" } } })
    assert(#ctx.hud.children[1].children == 1)
    render:locate({ point = { x = 50, z = 50 }, label = "nearby" })
    local circle, marker = created[5], created[6]
    render:render({})
    assert(not circle.removed and not marker.removed, "general render pruning cannot remove the locator")
    ctx.G.GetIndicatorLocationAndAngle = function(_, x, z) assert(x == 150 and z == 50); return 10, 20, 45 end
    render:locate({ point = { x = 150, z = 50 }, label = "outside view" })
    local arrow = ctx.hud.children[1].children[2]
    assert(arrow and not arrow.killed, "off-screen nearby items need a direction marker")
    render:locate({ point = { x = 50, z = 50 }, label = "nearby" })
    assert(arrow.killed and not circle.removed, "returning to view releases only the direction marker")
    render:locate(nil)
    assert(circle.removed and marker.removed, "losing selection clears every locator entity")
    render:close()
    assert(ctx.hud.children[1].killed)
end)
T.done()
