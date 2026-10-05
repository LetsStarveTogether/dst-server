local T = require("tests.client_mod.support")
local Placement = require("fieldkit/features/placement")
local Scope = require("fieldkit/core/scope")
local point = T.Vector3
local function entity()
    local e = T.entity({ rotation = 0, shown = true })
    function e:GetRotation() return self.rotation end
    function e:StartWallUpdatingComponent() end
    function e:Show() self.shown = true end
    function e:Hide() self.shown = false end
    local remove = e.Remove
    function e:Remove()
        if self.removed then return end
        remove(self)
        for _, component in pairs(self.components) do
            if component.OnRemoveEntity then component:OnRemoveEntity() end
        end
    end
    e.entity = { IsVisible = function() return e.shown end, SetParent = function() end }
    e.Transform.GetRotation = function() return e.rotation end
    e.Transform.SetRotation = function(_, value) e.rotation = value end
    e.GetDistanceSqToPoint = e.GetDistanceSq
    e.AnimState = {
        SetScale = function() end, SetMultColour = function() end, SetAddColour = function() end,
        PlayAnimation = function(_, name) e.animation = name end,
    }
    return e
end
local function boat()
    local inst = entity()
    inst.x, inst.z, inst.rotation = 100.2, 50.4, 90
    inst.entity.WorldToLocalSpace = function(_, x, _, z)
        local c, s = math.cos(inst.rotation * math.pi / 180), math.sin(inst.rotation * math.pi / 180)
        return (x - inst.x) * c + (z - inst.z) * s, 0, -(x - inst.x) * s + (z - inst.z) * c
    end
    inst.entity.LocalToWorldSpace = function(_, x, _, z)
        local c, s = math.cos(inst.rotation * math.pi / 180), math.sin(inst.rotation * math.pi / 180)
        return inst.x + x * c - z * s, 0, inst.z + x * s + z * c
    end
    return inst
end
local function context(create_module)
    local f = { cursor = entity(), guides = {}, probes = {}, prefab_setup = {}, cursor_events = 0, deploy_name = "sapling_placer" }
    local cursor = f.cursor
    cursor.x, cursor.z = 1.3, -.6
    local player = T.entity({ entity = { LocalToWorldSpace = function() return cursor.x, cursor.y, cursor.z end } })
    function player:GetCurrentPlatform() return f.platform end
    function player:PushEvent(event)
        if event == "onplacerhidden" or event == "onplacershown" then f.cursor_events = f.cursor_events + 1 end
    end
    local ctx = { player = player, config = { grid = true }, params = require("fieldkit/core/parameters").new(), G = {
        Vector3 = point, Class = T.Class, TheInput = {
            ControllerAttached = function() return f.attached end,
            GetWorldPosition = function() return cursor:GetPosition() end,
            GetHUDEntityUnderMouse = function() return f.hud_target end,
            IsControlPressed = function() return f.ctrl end,
        },
        TheSim = { FindEntities = function() return {} end },
        TheWorld = { Map = {
            GetPlatformAtPoint = function() return f.platform end,
            GetSize = function() return 200, 180 end,
            GetTileCenterPoint = function(_, x, _, z) return math.floor(x / 4) * 4 + 2, 0, math.floor(z / 4) * 4 + 2 end,
            CanTillSoilAtPoint = function(_, x) return x > 0 end,
        } },
    } }
    ctx.params.grid_size, ctx.params.grid_smart_spacing = 1, false
    function ctx:ready() return not self.scope.closed end
    function ctx:text(_, en) return en end
    function ctx:report(err) error(err) end
    ctx.scope = Scope.new(player, error)
    Placement.install({ AddComponentPostInit = function(_, fn) f.callback = fn end }, function() return ctx end)
    local G = ctx.G
    G.TUNING = { AXISALIGNEDPLACEMENT_INTERVALS = 1, AXISALIGNEDPLACEMENT_CIRCLESIZE = 2 }
    G.ThePlayer, G.HAS_AXISALIGNED_MOD_ENABLED, G.TILE_SCALE = player, false, 4
    G.TriggerDeployHelpers = function() end
    G.CONTROL_AXISALIGNEDPLACEMENT_TOGGLEMOD = 1
    G.Profile = { GetAxisAlignedPlacement = function() return false end }
    G.DEPLOYMODE, G.ACTIONS = { NONE = 0, TURF = 1, WALL = 2 }, { DEPLOY = 1 }
    G.GetFarmTillSpacing = function() return 1.25 end
    G.BufferedAction = function(_, _, _, _, pt) return { point = pt, onsuccess = {} } end
    local previous_trap = package.loaded["prefabs/trap_fumarole_util"]
    f.trap_util = T.native("prefabs/trap_fumarole_util", { TheWorld = G.TheWorld })
    package.loaded["prefabs/trap_fumarole_util"] = f.trap_util
    ctx.scope:defer(function() package.loaded["prefabs/trap_fumarole_util"] = previous_trap end)
    local env = setmetatable({ require = function(name)
        if name == "prefabs/trap_fumarole_util" then return f.trap_util end
        return {}
    end }, { __index = function(_, key) if G[key] ~= nil then return G[key] end return _G[key] end })
    local NativePlacer = T.native("components/placer", env)
    local recipe = { name = "campfire", placer = "campfire_placer", min_spacing = 0 }
    G.Prefabs = { campfire_placer = {}, sapling_placer = {}, axisalignedplacement_outline = {} }
    G.SpawnPrefab = function(name)
        assert(G.Prefabs[name], "must not spawn an unregistered prefab: " .. name)
        local inst = entity()
        if name == "axisalignedplacement_outline" then f.guides[#f.guides + 1] = inst
        else
            inst.components.placer = NativePlacer(inst)
            f.probes[#f.probes + 1] = inst
            f.callback(inst.components.placer)
            if f.prefab_setup[name] then f.prefab_setup[name](inst.components.placer) end
        end
        return inst
    end
    player.replica.builder = { CanBuildAtPoint = function(_, pt) return pt.x ~= 2 end }
    player.components.playercontroller = {}
    function f:make(item)
        local inst = entity()
        local placer = NativePlacer(inst)
        inst.components.placer = placer
        self.callback(placer)
        placer:SetBuilder(player, not item and recipe or nil, item)
        placer.testfn = function(pt) return pt.x ~= 2, false end
        return placer
    end
    function f:deployment()
        local item = { replica = { inventoryitem = {
            GetDeployMode = function() return 3 end, IsDeployable = function() return true end,
            DeploySpacingRadius = function() return 1 end, GetDeployPlacerName = function() return f.deploy_name end,
            CanDeploy = function(_, pt) return pt.x ~= 2 end,
        } }, HasAnyTag = function() return false end, HasTag = function() return false end }
        self.deploy = self:make(item)
        player.components.playercontroller = { deployplacer = self.deploy.inst }
        return item, self.deploy
    end
    function f:deploy_prefab(name, setup)
        G.Prefabs[name] = {}
        self.deploy_name, self.prefab_setup[name] = name, setup
        setup(self.deploy)
    end
    f.ctx, f.G, f.player, f.recipe, f.native = ctx, G, player, recipe, NativePlacer
    if create_module ~= false then f.module = Placement.new(ctx) end
    return f
end
local function scenario(name, fn, create_module)
    T.case(name, function()
        local f = context(create_module)
        fn(f)
        f.ctx.scope:close()
    end)
end

scenario("build_preview_and_grid", function(f)
    local ctx, player, cursor, module, recipe = f.ctx, f.player, f.cursor, f.module, f.recipe
    local probes, guides = f.probes, f.guides
    local build = f:make()
    player.components.playercontroller = { placer = build.inst }
    build:OnUpdate(0)
    assert(build.inst.x == 1 and build.inst.z == -1 and build.can_build)
    assert(module:context().point.x == 1 and module:status():match("square 1"))
    do
        local test, allocations = build.testfn, #probes + #guides
        build.testfn = function() error("reading placement context must not validate") end
        local current = assert(module:context())
        assert(current.kind == "build" and current.recipe == recipe and current.valid and current.rotation == 0)
        current.point.x = 999
        assert(module:context().point.x == 1, "context coordinates do not alias the live placer")
        module:toggle()
        assert(module:context().point.x == 1, "native context remains available when grid snapping is disabled")
        module:toggle()
        assert(#probes + #guides == allocations, "reading context never creates a probe or display entity")
        build.testfn = test
    end
    local before = #guides
    assert(module:swap_shape() == "diamond" and module:swap_shape() == "square")
    assert(module:cycle() == "diamond" and module:cycle() == "hex")
    assert(module:swap_shape() == "diamond" and module:swap_shape() == "hex")
    ctx.params.grid_shape = "square"
    build:OnUpdate(0)
    assert(#guides - before == 12, "grid creation respects per-frame budget")
    cursor.x = 1.7
    build:OnUpdate(0)
    assert(build.inst.x == 2 and not build.can_build, "native validation sees snapped position")
    assert(build.inst.z == -1, "multi-point tests restore selected transform")
    f.ctrl, cursor.x = true, 3.4
    build:OnUpdate(0)
    assert(build.inst.x == 3.4 and build.axisalignedhelpers.parent.shown, "Ctrl frees placement")
    f.ctrl = false
    module:rotate(90)
    module:set_origin(point(0.25, 0, 0.25))
    build:OnUpdate(0)
    assert(math.abs(build.inst.x - 3.25) < 1e-8 and math.abs(build.inst.z + .75) < 1e-8)
    module:reset_origin()
    module:rotate(-90)
end)

T.each("guide refresh", {
    { name = "square", count = 81 }, { name = "hex", count = 61 },
}, function(row)
    scenario("render", function(f)
        f.ctx.params.grid_shape = row.name
        f.player.replica.builder.CanBuildAtPoint = function() return true end
        local build = f:make()
        f.player.components.playercontroller = { placer = build.inst }
        local blocked = false
        build.testfn = function() return not blocked, false end
        for _ = 1, 16 do build:OnUpdate(0) end
        local guides = {}
        for _, guide in ipairs(f.guides) do
            if not guide.floordecalanim then guides[#guides + 1] = guide end
        end
        assert(#guides == row.count)
        local moves, scales, paints = 0, 0, 0
        for _, guide in ipairs(guides) do
            local move, scale, paint = guide.Transform.SetPosition, guide.AnimState.SetScale, guide.AnimState.PlayAnimation
            guide.Transform.SetPosition = function(...) moves = moves + 1; return move(...) end
            guide.AnimState.SetScale = function(...) scales = scales + 1; return scale(...) end
            guide.AnimState.PlayAnimation = function(...) paints = paints + 1; return paint(...) end
            assert(guide.animation == "unit")
        end
        build:OnUpdate(0)
        assert(moves == 0 and scales == 0 and paints == 0,
            "stationary, fully validated guides must not repeat unchanged rendering writes")
        blocked = true
        for _ = 1, 8 do build:OnUpdate(0) end
        for _, guide in ipairs(guides) do assert(guide.animation == "unit_x") end
        assert(moves == 0 and scales == 0 and paints == row.count,
            "stationary placement still revalidates world changes within the refresh cycle")
        local positions = {}
        for i, guide in ipairs(guides) do positions[i] = guide:GetPosition() end
        f.cursor.x = f.cursor.x + 3
        build:OnUpdate(0)
        for i, guide in ipairs(guides) do
            assert(math.abs(guide.x - positions[i].x - 3) < 1e-8 and math.abs(guide.z - positions[i].z) < 1e-8,
                "all guides follow the newly selected grid centre in the same frame")
        end
        f.ctrl = true
        build:OnUpdate(0)
        for _, guide in ipairs(guides) do assert(not guide.shown) end
        f.ctrl = false
        build:OnUpdate(0)
        for _, guide in ipairs(guides) do assert(guide.shown) end
    end)
end)

scenario("deploy_controls_and_platform", function(f)
    local G, cursor, module = f.G, f.cursor, f.module
    local item, deploy = f:deployment()
    cursor.x = 3.2
    deploy:OnUpdate(0)
    assert(deploy:GetDeployAction().point.x == 3)
    deploy.selected_pos, f.attached, cursor.x = nil, true, -2.3
    deploy:OnUpdate(0)
    assert(deploy:GetDeployAction().point.x == -2, "controller preview and native action share point")
    deploy.selected_pos, f.attached = nil, false
    local boat = boat()
    f.platform, cursor.x, cursor.z = boat, 100.6, 50.8
    deploy:OnUpdate(0)
    assert(math.abs(deploy.inst.x - 100.2) < 1e-8 and math.abs(deploy.inst.z - 50.4) < 1e-8)
    local world, record = module:snap(cursor:GetPosition(), item, nil, "deploy")
    assert(math.abs(world.x - deploy.inst.x) < 1e-8, "platform plan and preview use same local lattice")
    boat.x = boat.x + 5
    assert(math.abs(module:resolve(record).x - world.x - 5) < 1e-8, "planned point follows moving platform")
    local _, rotated_record = module:snap(point(boat.x + 2, 0, boat.z + 1), item, nil, "deploy")
    boat.rotation = 180
    local rotated_point = module:resolve(rotated_record)
    assert(math.abs(rotated_point.x - boat.x + 1) < 1e-8 and math.abs(rotated_point.z - boat.z - 2) < 1e-8,
        "a plan retains local lattice coordinates when its platform rotates")
    boat.rotation = 90
    local edge_calls = 0
    G.TheSim = { FindEntities = function() return { boat } end }
    G.GetClosest = function(_, boats) return boats[1] end
    G.SnapToBoatEdge = function(inst, target, pt)
        assert(target == boat and pt)
        edge_calls = edge_calls + 1
        inst.Transform:SetPosition(boat.x + 3, 0, boat.z)
        inst.Transform:SetRotation(90)
    end
    f:deploy_prefab("boat_bumper_placer", function(placer) placer.snap_to_boat_edge = true end)
    deploy:OnUpdate(0)
    assert(deploy.inst.x == boat.x + 3 and edge_calls > 1, "boat-edge preview and guide validation delegate to native helper")
    local edge_valid, edge_point, edge_rotation = module:validate(cursor:GetPosition(), item, nil, "deploy")
    assert(edge_valid and edge_point.x == boat.x + 3 and edge_rotation == 90)
    f.platform = nil
    assert(module:validate(cursor:GetPosition(), item, nil, "deploy"), "boat-edge validation finds a nearby boat outside its platform")
end)

scenario("controller_native_ground_projection", function(f)
    local ctx, G, player = f.ctx, f.G, f.player
    f.attached, ctx.params.grid_smart_spacing = true, true
    player.x, player.z = -.1, 0
    player.Transform.GetRotation = function() return 0 end
    player.entity.LocalToWorldSpace = function(_, x, y, z) return player.x + x, y, player.z + z end
    local env = { Class = T.Class, DEGREES = math.pi / 180, GetTime = function() return 0 end,
        TUNING = { CONTROLLER_BOATPLACEMENT_ANGLE = 135 },
        string = setmetatable({}, { __index = string }),
        table = setmetatable({}, { __index = table }), math = setmetatable({}, { __index = math }) }
    T.native("util", env)
    local map = G.TheWorld.Map
    map.IsOceanTileAtPoint = function(_, x) return x > 0 end
    T.eval(assert(T.source("components/map"):match(
        "(function Map:IsSurroundedByWater.-)\nfunction Map:IsSurroundedByLand")), { Map = map })
    local item, placer = f:deployment()
    item.replica.inventoryitem.DeploySpacingRadius = function() return 4 end
    item.HasAnyTag = function(_, ...)
        for _, tag in ipairs({ ... }) do if tag == "boatbuilder" then return true end end
        return false
    end
    placer:SetBuilder(player, nil, item)
    placer.onground, placer.offset = true, 6
    env.ControllerPlacer_Boat_SpotFinder(placer.inst, 4)
    placer.testfn = function(pt) return map:IsSurroundedByWater(pt.x, pt.y, pt.z, 4.2), false end
    assert(not placer.axisalignedplacementallowedbyitem, "native boat placers exclude axis alignment")
    placer:OnUpdate(0)
    assert(placer.can_build and math.abs(placer.inst.x - 5.9) < 1e-8,
        "controller ground projection retains a valid boat position beside the shoreline")
    assert(#f.guides == 0 and f.module:status():match("free placement"),
        "native controller projection must not show an unrelated snapped grid")
    f.attached = false
    f.cursor.x, f.cursor.z = 5.9, 0
    placer:InitializeAxisAlignedHelpers()
    placer:OnUpdate(0)
    assert(math.abs(placer.inst.x - 4.001) < 1e-8, "mouse placement still uses the configured grid")
    local guides = {}
    for _, guide in ipairs(f.guides) do
        if not guide.floordecalanim then guides[#guides + 1] = guide; assert(guide.shown) end
    end
    assert(#guides == 12 and not placer.axisalignedhelpers.parent.shown,
        "switching to mouse restores Fieldkit guides and hides native helpers")
    f.attached = true
    placer:OnUpdate(0)
    for _, guide in ipairs(guides) do assert(not guide.shown, "controller projection hides existing Fieldkit guides") end
    assert(placer.axisalignedhelpers.parent.shown, "controller projection restores native helper visibility")
    local ordinary = f:make()
    player.components.playercontroller.placer = ordinary.inst
    local before = #f.guides
    ordinary:OnUpdate(0)
    assert(ordinary.inst.x == 1 and #f.guides == before + 12 and f.guides[#f.guides].shown,
        "ordinary controller placers still snap and show their grid")
end)

scenario("native_snap_and_projection", function(f)
    local G, cursor, module = f.G, f.cursor, f.module
    local item, deploy = f:deployment()
    cursor.x, cursor.z = 3.2, -1.3
    f:deploy_prefab("wall_placer", function(placer) placer.snap_to_meters = true end)
    deploy:OnUpdate(0)
    assert(deploy.inst.x == 3.5 and deploy.inst.z == -1.5, "walls retain native half-metre origin")
    assert(module:snap(cursor:GetPosition(), item, nil, "deploy").x == 3.5)
    local visible_test = deploy.testfn
    local blocked_test = function(pt) return item.replica.inventoryitem:CanDeploy(pt), true end
    deploy.testfn = blocked_test
    local visible_can, visible_blocked = deploy:TestCanBuild()
    assert(visible_can and visible_blocked, "native visible placer remains blocked by the mouse or HUD")
    deploy.inst.Transform:SetRotation(37)
    local before_validation_events = f.cursor_events
    local allowed, wall_point, wall_rotation = module:validate(cursor:GetPosition(), item, nil, "deploy")
    assert(allowed and wall_point.x == 3.5 and wall_rotation == 37,
        "independent wall validation ignores cursor blocking and retains the chosen rotation")
    assert(f.cursor_events == before_validation_events, "replacing a validation probe must not change the native cursor icon")
    G.TheCamera = { GetHeading = function() return 10 end }
    deploy.fixedcameraoffset, deploy.rotationoffset, deploy.fixedcameraoffset_nearestangle = 90, 45, 30
    local wall_sites = module:points(point(3, 0, -2), point(4, 0, -1), 10, item, nil, "deploy")
    assert(#wall_sites == 1 and wall_sites[1].rotation == 120,
        "batch points preserve camera-relative rotation offsets and angle rounding")
    assert(deploy.inst.Transform:GetRotation() == 37 and deploy.testfn == blocked_test,
        "batch validation does not mutate the visible placer's transform or test")
    deploy.testfn = visible_test
    deploy.fixedcameraoffset, deploy.rotationoffset, deploy.fixedcameraoffset_nearestangle = nil, nil, nil
    G.TheCamera = nil
    deploy.snap_to_meters = nil
    f:deploy_prefab("turf_placer", function(placer) placer.snap_to_tile = true end)
    deploy:OnUpdate(0)
    assert(deploy.inst.x == 2 and deploy.inst.z == -2)
    assert(module:snap(cursor:GetPosition(), item, nil, "deploy").x == 2, "tile plan matches native preview")
    deploy.snap_to_tile = nil
    f:deploy_prefab("exact_point_placer", function(placer)
        placer.onupdatetransform = function(inst) inst.Transform:SetPosition(7, 0, 9); inst.accept_placement = true end
        placer.override_testfn = function(inst) return inst.accept_placement, false end
        placer.override_build_point_fn = function(inst) return inst:GetPosition() end
    end)
    deploy:OnUpdate(0)
    assert(module:context().point.x == 7 and deploy.can_build, "custom exact-point transform remains authoritative")
    local can, projected = module:validate(point(1, 0, 2), item, nil, "deploy")
    assert(can and projected.x == 7 and projected.z == 9 and deploy.inst.x == 7)
    local sites = module:points(point(0, 0, 0), point(2, 0, 2), 20, item, nil, "deploy")
    assert(#sites == 1 and sites[1].x == 7, "native projected sites are deduplicated")
end)

scenario("hidden_probes_and_spacing", function(f)
    local ctx, player, module, recipe, probes = f.ctx, f.player, f.module, f.recipe, f.probes
    local item = f:deployment()
    player.components.playercontroller = {}
    ctx.params.grid_smart_spacing = true
    assert(module:snapshot(point(0, 0, 0), nil, recipe, "build").grid.spacing == 1, "zero recipe spacing is legal and finite")
    recipe.min_spacing = 3
    assert(module:snapshot(point(0, 0, 0), nil, recipe, "build").grid.spacing == 3.001)
    assert(module:validate(point(3, 0, 0), nil, recipe, "build"))
    assert(not module:validate(point(2, 0, 0), nil, recipe, "build"))
    assert(#probes > 0 and not probes[1].shown, "cached validation placer remains hidden")
    local probe_count = #probes
    local missing_recipe = { placer = "missing_placer", min_spacing = 1 }
    module:snapshot(point(0, 0, 0), nil, missing_recipe, "build")
    assert(not module:validate(point(3, 0, 0), nil, missing_recipe, "build"), "missing recipe placer cannot validate")
    f.deploy_name = "missing_placer"
    module:snapshot(point(0, 0, 0), item, nil, "deploy")
    assert(module:validate(point(3, 0, 0), item, nil, "deploy"), "placerless deployment uses native CanDeploy")
    assert(#probes == probe_count, "missing placers never reach SpawnPrefab")
end)

scenario("native seed placement and probe lifetime", function(f)
    local item, placer = f:deployment()
    f.ctx.params.grid_smart_spacing = true
    function item:HasActionComponent(name) return name == "farmplantable" or name == "deployable" end
    local outlines = {}
    local env = setmetatable({ SpawnPrefab = function(name)
        assert(name == "tile_outline")
        local outline = entity()
        outlines[#outlines + 1] = outline
        return outline
    end }, { __index = function(_, key) return f.G[key] or _G[key] end })
    local source = T.source("prefabs/seeds")
    local postinit = T.eval(assert(source:match("local function update_seed_placer_outline%b().-\nend")) .. "\n"
        .. assert(source:match("local function seed_placer_postinit%b().-\nend")) .. "\nreturn seed_placer_postinit", env)
    local native_test = T.eval(assert(source:match("local function can_plant_seed%b().-\nend"))
        .. "\nreturn can_plant_seed", env)
    item.replica.inventoryitem.CanDeploy = function(_, pt, mouse, doer) return native_test(item, pt, mouse, doer) end
    f:deploy_prefab("seeds_placer", function(p) postinit(p.inst) end)
    f.cursor.x, f.cursor.z = 1.5, 1.5
    placer:OnUpdate(0)
    assert(placer.inst.x == 2 and placer.inst.z == 2)
    local cursor_events = f.cursor_events
    local frame = f.module:snapshot(f.cursor:GetPosition(), item, nil, "deploy")
    assert(frame.grid.spacing == 4 / 3, "native direct seed planting uses the farm lattice")
    local first_probe = #f.probes
    local sites = f.module:points(point(0, 0, 0), point(4, 0, 4), 16, item, nil, "deploy")
    assert(#sites == 9 and #f.probes == first_probe + 1, "an area validates all sites with one temporary native placer")
    assert(outlines[1]:IsValid(), "the real preview retains its own outline")
    for i = 2, #outlines do
        assert(not outlines[i]:IsValid(), "native removal must release unparented, unlinked outline entities before rendering")
    end
    item.replica.inventoryitem.CanDeploy = function() error("native deployment test failure") end
    assert(not pcall(f.module.validate, f.module, point(2, 0, 2), item, nil, "deploy"))
    assert(not outlines[#outlines]:IsValid() and not f.probes[#f.probes]:IsValid(), "failed probes release all native resources")
    assert(placer.inst.x == 2 and placer.inst.z == 2 and f.cursor_events == cursor_events)
end)

scenario("planned orientation survives preview replacement", function(f)
    local item, placer = f:deployment()
    local heading = 10
    f.G.TheCamera = { GetHeading = function() return heading end }
    f:deploy_prefab("rotated_placer", function(p)
        p.fixedcameraoffset, p.fixedcameraoffset_nearestangle = 90, 30
    end)
    placer.rotationoffset = 45
    local records = f.module:points(point(3, 0, 3), point(4, 0, 4), 8, item, nil, "deploy")
    assert(records[1].rotation == 120)
    f.player.components.playercontroller.deployplacer = nil
    placer.inst:Remove()
    heading = 180
    local position, rotation = f.module:resolve(records[1])
    local allowed, _, final_rotation = f.module:validate(position, item, nil, "deploy", rotation)
    assert(allowed and final_rotation == 120, "captured orientation survives camera movement and cursor replenishment")
    local platform = boat()
    f.platform = platform
    records = f.module:points(point(platform.x, 0, platform.z), point(platform.x + 1, 0, platform.z + 1), 8, item, nil, "deploy")
    local _, before = f.module:resolve(records[1])
    platform.rotation = platform.rotation + 90
    position, rotation = f.module:resolve(records[1])
    allowed, _, final_rotation = f.module:validate(position, item, nil, "deploy", rotation)
    assert(allowed and rotation == before + 90 and final_rotation == rotation,
        "a platform plan rotates both its positions and its building orientations")
end)

T.each("native anchored placement", {
    { name = "moon_device", transform = "placer_onupdatetransform", build = "placer_override_build_point", test = "placer_override_testfn" },
    { name = "wendy_recipe_gravestone", transform = "wendy_placer_onupdatetransform", build = "wendy_placer_override_build_point", test = "wendy_placer_override_testfn" },
    { name = "wagboss_robot_constructionsite", transform = "OnUpdateTransform_Placer", build = "OverrideBuildPoint_Placer" },
    { name = "gestalt_cage", transform = "OnUpdateTransform_Placer", build = "OverrideBuildPoint_Placer" },
}, function(row)
    scenario("anchor", function(f)
        local item, placer = f:deployment()
        local anchor = entity(); anchor.x, anchor.z = 3.5, 3.5
        anchor.tags.can_build_moon_device = true
        local env = setmetatable({ PLACER_SNAP_DISTANCE = 1, WENDY_PLACER_SNAP_DISTANCE = 1 },
            { __index = function(_, key) return f.G[key] or _G[key] end })
        f.G.TUNING.WAGBOSS_ROBOT_CONSTRUCTIONSITE_KIT_PLACEMENT_RADIUS = 1
        f.G.TUNING.GESTALT_CAGE_FILLED_PLACEMENT_RADIUS = 1
        f.G.TheSim.FindEntities = function(_, x, _, z, radius)
            return (anchor.x - x)^2 + (anchor.z - z)^2 <= radius^2 and { anchor } or {}
        end
        local source = T.source("prefabs/" .. row.name)
        local function fn(name)
            return name and T.eval(assert(source:match("local function " .. name .. "%b().-\nend")) .. "\nreturn " .. name, env)
        end
        f:deploy_prefab(row.name .. "_placer", function(p)
            p.onupdatetransform, p.override_build_point_fn, p.override_testfn = fn(row.transform), fn(row.build), fn(row.test)
        end)
        f.ctx.params.grid_size = 8
        f.cursor.x, f.cursor.z = 3.6, 3.6
        placer:OnUpdate(0)
        assert(placer.can_build and placer.inst.x == anchor.x and placer.inst.z == anchor.z,
            "native anchor search must receive the pointer instead of a distant geometric lattice point")
        assert(not placer:IsAxisAlignedPlacement() and f.module:status():match("native placement"))
        local allowed, projected = f.module:validate(f.cursor:GetPosition(), item, nil, "deploy")
        assert(allowed and projected.x == anchor.x and projected.z == anchor.z)
    end)
end)

scenario("native bridge direction", function(f)
    local _, placer = f:deployment()
    local checked
    local env = setmetatable({ TILE_SIZE = 4, Bridge_DeployCheck_Helper = function(_, position)
        checked = point(position.x, 0, position.z)
        return false
    end }, { __index = function(_, key) return f.G[key] or _G[key] end })
    local source = T.source("prefabs/rope_bridge")
    local function fn(name)
        return T.eval(assert(source:match("local function " .. name .. "%b().-\nend")) .. "\nreturn " .. name, env)
    end
    placer.inst.pos, placer.inst.pieces = point(), {}
    placer.onupdatetransform = fn("placer_onupdatetransform")
    placer.oncanbuild, placer.oncannotbuild = fn("placer_oncanbuild"), fn("placer_oncannotbuild")
    f.ctx.params.grid_size = 8
    f.cursor.x, f.cursor.z = 3.6, 3.6
    placer:OnUpdate(0)
    assert(checked.x == 2.4 and checked.z == 2.4,
        "native bridge search retains the mouse quadrant even with a coarse grid selected")
    assert(not placer:IsAxisAlignedPlacement() and f.module:status():match("native placement"))
end)

scenario("native fumarole half-tile lattice", function(f)
    local item, placer = f:deployment()
    f:deploy_prefab("trap_fumarole_placer", function(p) p.snap_to_half_tile = true end)
    for _, value in ipairs({ -9.01, -8, -2.01, -2, -1.99, -.01, 0, .01, 1.99, 2, 2.01, 9.01 }) do
        f.cursor.x, f.cursor.z = value, -value
        placer:OnUpdate(0)
        local x, _, z = f.trap_util.GetTrapCenterPoint(value, 0, -value)
        local snapped = f.module:snap(f.cursor:GetPosition(), item, nil, "deploy")
        local _, projected = f.module:validate(f.cursor:GetPosition(), item, nil, "deploy")
        assert(placer.inst.x == x and placer.inst.z == z and snapped.x == x and snapped.z == z
            and projected.x == x and projected.z == z, "preview, sampling and validation use native half-tile boundaries")
    end
end)

scenario("farm_layouts_and_tile_actions", function(f)
    local ctx, G, module, probes = f.ctx, f.G, f.module, f.probes
    local item = f:deployment()
    f.player.components.playercontroller = {}
    ctx.params.grid_smart_spacing = true
    local probe_count, boat = #probes, boat()
    local deploy_queries = 0
    G.TheSim.FindEntities = function() return {} end
    local hoe = { replica = { inventoryitem = {
        GetDeployMode = function() return G.DEPLOYMODE.NONE end,
        IsDeployable = function() deploy_queries = deploy_queries + 1; return false end,
        GetDeployPlacerName = function() return "farm_hoe_placer" end,
    } } }
    assert(module:snapshot(point(0, 0, 0), hoe, nil, "till").grid.spacing == 4 / 3)
    assert(module:snap(point(1.5, 0, 1.5), hoe, nil, "till").x == 2)
    assert(module:validate(point(1, 0, 0), hoe, nil, "till"))
    local till_sites = module:points(point(0, 0, 0), point(4, 0, 4), 100, hoe, nil, "till")
    assert(#till_sites > 0 and #probes == probe_count and deploy_queries == 0,
        "all till entry points bypass deploy placer lookup for the equipped hoe")
    assert(module:snapshot(point(0, 0, 0), nil, nil, "till").grid.spacing == 4 / 3)
    G.GetFarmTillSpacing = function() return 2 end
    assert(module:snapshot(point(0, 0, 0), nil, nil, "till").grid.spacing == 2.001, "larger native till radii preserve old soil")
    G.GetFarmTillSpacing = function() return 1.25 end
    assert(module:validate(point(1, 0, 0), nil, nil, "till"))
    assert(not module:validate(point(-1, 0, 0), nil, nil, "till"))
    G.TheSim.FindEntities = function() return { { IsValid = function() return true end } } end
    assert(not module:validate(point(1, 0, 0), nil, nil, "till"), "native CanTill alone cannot protect existing soil")
    G.TheSim.FindEntities = function() return {} end
    T.each("layout", { 2, 3, 4 }, function(layout)
        ctx.params.farm_layout = layout
        local sites = assert(module:points(point(0, 0, 0), point(4, 0, 4), 100, hoe, nil, "till"))
        assert(#sites == layout * layout, "one unobstructed tile retains the selected farm layout")
        for i, a in ipairs(sites) do
            assert(a.x > 0 and a.x < 4 and a.z > 0 and a.z < 4)
            for j = i + 1, #sites do
                local b = sites[j]
                assert((a.x - b.x)^2 + (a.z - b.z)^2 > 1.25^2, "neighbours never collapse each other's soil")
            end
        end
        local expanded = assert(module:points(point(0, 0, 0), point(8, 0, 4), 100, hoe, nil, "till"))
        for i, a in ipairs(expanded) do
            for j = i + 1, #expanded do
                local b = expanded[j]
                assert((a.x - b.x)^2 + (a.z - b.z)^2 > 1.25^2, "dense rows stay continuous across tile borders")
            end
        end
        local shifted = assert(module:points(point(-8, 0, -4), point(-4, 0, 0), 100, hoe, nil, "till"))
        if layout ~= 4 then assert(#shifted == layout * layout) end
        for _, record in ipairs(shifted) do
            local snapped = module:snap(point(record.x, 0, record.z), nil, nil, "till")
            assert(math.abs(snapped.x - record.x) < 1e-7 and math.abs(snapped.z - record.z) < 1e-7,
                "area plans and manual tilling share a fixed lattice at negative coordinates")
        end
        for _, record in ipairs(expanded) do
            local snapped = module:snap(point(record.x, 0, record.z), nil, nil, "till")
            assert(math.abs(snapped.x - record.x) < 1e-7 and math.abs(snapped.z - record.z) < 1e-7,
                "crossing a tile boundary cannot restart dense rows at a colliding offset")
        end
    end)
    ctx.params.farm_layout = 3
    local platform_query = G.TheWorld.Map.GetPlatformAtPoint
    G.TheWorld.Map.GetPlatformAtPoint = function(_, x, z)
        return math.abs(x - 2) < 0.1 and math.abs(z - 2) < 0.1 and boat or nil
    end
    local mixed, reason = module:points(point(0, 0, 0), point(4, 0, 4), 100, nil, nil, "till")
    assert(not mixed and reason == "mixed_platforms", "four land corners cannot hide a platform through the interior")
    G.TheWorld.Map.GetPlatformAtPoint = platform_query
    G.WORLD_TILES = { FARMING_SOIL = 42 }
    G.TheWorld.Map.GetTileAtPoint = function(_, x) return x < 8 and 42 or 1 end
    G.TheWorld.Map.CanTerraformAtPoint = function(_, x) return x < 8 end
    G.TheWorld.Map.IsPassableAtPoint = function(_, x) return x >= 0 end
    G.TheWorld.Map.IsGroundTargetBlocked = function(_, pt) return pt.x == 3 end
    module:rotate(45)
    module:set_origin(point(.3, 0, .8))
    module:cycle()
    T.each("tile_action", { "water", "fertilize", "terraform" }, function(kind)
        local field, frame = module:points(point(0, 0, 0), point(8, 0, 8), 4, item, nil, kind)
        assert(#field == 4 and frame.fixed and frame.grid.shape == "square" and frame.grid.spacing == 4)
        for _, record in ipairs(field) do
            assert(record.x % 4 == 2 and record.z % 4 == 2 and record.platform == nil,
                "tile jobs use one native tile centre regardless of active lattice, spacing, origin or rotation")
        end
        assert(not module:points(point(0, 0, 0), point(8, 0, 8), 3, item, nil, kind), "tile limits do not truncate a plan")
    end)
    assert(module:validate(point(2, 0, 2), item, nil, "water"))
    assert(not module:validate(point(10, 0, 2), item, nil, "water"), "changed farmland is revalidated before watering")
    assert(module:validate(point(2, 0, 2), nil, nil, "terraform"))
    assert(not module:validate(point(10, 0, 2), nil, nil, "terraform"), "native turf protections are checked at execution")
    assert(module:validate(point(2, 0, 2), nil, nil, "drop"))
    assert(not module:validate(point(-1, 0, 2), nil, nil, "drop"), "drop plans do not throw items into water or void")
    assert(not module:validate(point(3, 0, 2), nil, nil, "drop"), "native ground-target blockers also block dropping")
    local old_mode, old_spacing = item.replica.inventoryitem.GetDeployMode, ctx.params.grid_smart_spacing
    item.replica.inventoryitem.GetDeployMode = function() return G.DEPLOYMODE.TURF end
    ctx.params.grid_smart_spacing = true
    local drop_frame = module:snapshot(point(2, 0, 2), item, nil, "drop")
    assert(not drop_frame.fixed and drop_frame.grid.spacing == ctx.params.grid_size and drop_frame.grid.shape ~= "square",
        "dropping turf or deployables uses the selected geometric grid, not their deployment lattice")
    assert(drop_frame.grid.x == .3 and drop_frame.grid.z == .8,
        "drop plans retain the shared configured origin")
    item.replica.inventoryitem.GetDeployMode, ctx.params.grid_smart_spacing = old_mode, old_spacing
    f.platform = boat
    assert(not module:points(point(0, 0, 0), point(8, 0, 8), 4, item, nil, "water"), "soil jobs never follow a boat-local tile")
    assert(not module:validate(point(2, 0, 2), item, nil, "fertilize"))
    assert(not module:points(point(0, 0, 0), point(8, 0, 8), 4, nil, nil, "terraform"), "boats have no diggable turf grid")
    assert(not module:validate(point(2, 0, 2), nil, nil, "terraform"))
    local drop_point, drop_record = module:snap(point(boat.x + 1, 0, boat.z + 1), item, nil, "drop")
    boat.x = boat.x + 2
    assert(math.abs(module:resolve(drop_record).x - drop_point.x - 2) < 1e-8,
        "dropping on a platform retains platform-relative positions")
    f.platform = nil
    module:rotate(-45)
    module:reset_origin()
    assert(#probes == probe_count and deploy_queries == 0, "tile jobs bypass mouse placers and allocate no validation prefabs")
end)

scenario("placer_lifecycle", function(f)
    local ctx, player, module, recipe, probes, guides = f.ctx, f.player, f.module, f.recipe, f.probes, f.guides
    local NativePlacer, build = f.native, f:make()
    local item, deploy = f:deployment()
    assert(module:validate(point(3, 0, 0), item, nil, "deploy") and #probes > 0)
    local cleanup_count = #ctx.scope.cleanups
    T.each("native_removal", { "SetBuilder", "OnUpdate" }, function(method)
        local placer = NativePlacer(entity())
        placer[method] = function(self) self.inst:Remove(); return 1, nil, 3 end
        f.callback(placer)
        if method == "OnUpdate" then placer:SetBuilder(player, recipe) end
        local wrapped, before_guides = placer[method], #guides
        local later = function(...) return wrapped(...) end
        placer[method] = later
        local a, b, c = placer[method](placer, player, recipe)
        assert(a == 1 and b == nil and c == 3, "closing inside native code preserves all return values")
        assert(placer[method] == later and rawget(placer, "IsAxisAlignedPlacement") == nil, "later hooks survive independent placer cleanup")
        placer[method](placer, player, recipe)
        assert(#guides == before_guides and T.listener_count(placer.inst) == 0, "closed hooks cannot attach or render after native removal")
        assert(#ctx.scope.cleanups == cleanup_count, "transient placers do not accumulate player cleanup callbacks")
    end)
    local native_update = deploy.OnUpdate
    deploy.OnUpdate = function(self, ...) return native_update(self, ...) end
    local later = deploy.OnUpdate
    deploy.inst:Remove()
    assert(deploy.OnUpdate == later and rawget(deploy, "IsAxisAlignedPlacement") == nil)
    local cleanup_cursor_events = f.cursor_events
    ctx.scope:close()
    for _, inst in ipairs(probes) do assert(inst.removed, "probe lifecycle") end
    assert(f.cursor_events == cleanup_cursor_events, "scope teardown cannot change native placement UI through its validation probe")
    assert(module:context() == nil, "closed player scope exposes no placement context")
    assert(rawget(build, "OnUpdate") == nil)
end)

scenario("manual_till_input", function(f)
    local ctx, G, player, cursor = f.ctx, f.G, f.player, f.cursor
    ctx.params.grid_shape = "square"
    local tool = { HasActionComponent = function(_, name) return name == "farmtiller" end }
    player.replica.inventory = { GetEquippedItem = function() return tool end, GetActiveItem = function() end }
    G.EQUIPSLOTS = { HANDS = "hands" }
    G.ACTIONS.TILL = { id = "TILL" }
    local native_calls, requested, native_action, sent = 0, nil, nil, nil
    local pc = {
        UsingMouse = function() return not f.attached end,
        IsAOETargeting = function() return false end,
        OnRightClick = function() native_calls = native_calls + 1 end,
        GetRightMouseAction = function() return native_action end,
        DoControllerAltActionButton = function() native_calls = native_calls + 1 end,
        GetGroundUseAction = function() return nil, native_action end,
        IsAxisAlignedPlacement = function() return true end,
    }
    player.components.playercontroller = pc
    player.components.playeractionpicker = { GetPointActions = function(_, pt, item_, right)
        requested = pt
        assert(item_ == tool and right)
        return { { action = G.ACTIONS.TILL, point = pt } }
    end }
    local Actions = require("fieldkit/game/actions")
    local original_dispatch = Actions.dispatch
    local closed = 0
    Actions.dispatch = function(_, action, button)
        assert(button == "right")
        sent = action.point
        return { close = function() closed = closed + 1 end }
    end
    local till_tick
    function player:DoPeriodicTask(_, fn)
        till_tick = fn
        return { Cancel = function() till_tick = nil end }
    end
    local tilling = Placement.new(ctx)
    cursor.x, cursor.z = 1.5, 1.4
    till_tick()
    local till_context = assert(tilling:context())
    assert(till_context.kind == "till" and till_context.valid and till_context.point.x == 2)
    pc:OnRightClick(true)
    assert(sent.x == 2 and sent.z == 2 and native_calls == 0, "mouse till dispatch agrees with geometric preview")
    pc:OnRightClick(false)
    assert(native_calls == 1, "native release always runs")
    native_action = { action = { id = "PICK" } }
    sent = nil
    pc:OnRightClick(true)
    assert(sent == nil and native_calls == 2, "other native right-click action keeps priority")
    f.attached, native_action = true, { action = G.ACTIONS.TILL }
    pc:DoControllerAltActionButton()
    assert(sent.x == 2 and sent.z == 2 and closed == 1, "controller till uses same point and closes previous ticket")
    G.TheSim.FindEntities = function() return { { IsValid = function() return true end } } end
    sent = nil
    f.attached = false
    pc:OnRightClick(true)
    f.attached = true
    pc:DoControllerAltActionButton()
    assert(sent == nil and native_calls == 2,
        "existing soil blocks both till inputs without falling back to destructive native placement")
    ctx.scope:close()
    assert(closed == 2)
    assert(till_tick == nil and tilling:context() == nil)
    Actions.dispatch = original_dispatch
end, false)

scenario("manual_till_native_input_gates", function(f)
    local ctx, G, player = f.ctx, f.G, f.player
    local pc, actions, cancelled, released = player.components.playercontroller, 0, 0, 0
    G.EQUIPSLOTS, G.ACTIONS.TILL = { HANDS = "hands" }, { id = "TILL", code = 99 }
    G.GetTime, G.TheNet = function() return 0 end, { IsServerPaused = function() return false end }
    ctx.hud = { HasInputFocus = function() return false end }
    player.HUD = ctx.hud
    ctx.ready = function(self) return require("fieldkit/game/input").ready(self) end
    local tool = { HasActionComponent = function(_, name) return name == "farmtiller" end }
    player.replica.inventory = { GetEquippedItem = function() return tool end, GetActiveItem = function() end }
    pc.ismastersim = true
    pc.UsingMouse = function() return not f.attached end
    pc.IsEnabled, pc.IsBusy = function() return not f.disabled end, function() return false end
    pc.IsAOETargeting, pc.ClearActionHold = function() return f.aoe end, function() end
    pc.GetRightMouseAction = function() return nil end
    pc.GetGroundUseAction = function() return nil, { action = G.ACTIONS.TILL } end
    pc.DoAction = function() actions = actions + 1 end
    pc.RemoteStopControl = function() released = released + 1 end
    pc.CancelPlacement = function(self) self.placer_recipe = nil; cancelled = cancelled + 1 end
    pc.CancelDeployPlacement = function(self) self.deployplacer = nil; cancelled = cancelled + 1 end
    pc.CancelAOETargeting = function() f.aoe = nil; cancelled = cancelled + 1 end
    local source = T.source("components/playercontroller")
    for _, name in ipairs({ "OnRightClick", "DoControllerAltActionButton" }) do
        T.eval(assert(source:match("(function PlayerController:" .. name .. "%b().-)\nfunction PlayerController:")),
            { PlayerController = pc, TheInput = G.TheInput })
    end
    player.components.playeractionpicker = { GetPointActions = function(_, pt)
        return { { action = G.ACTIONS.TILL, doer = player, point = pt, rotation = 0,
            IsValid = function() return true end,
            AddSuccessAction = function() end, AddFailAction = function() end } }
    end }
    Placement.new(ctx)
    for _, flag in ipairs({ "hud_target", "attached", "disabled" }) do
        f[flag] = true
        pc:OnRightClick(true)
        assert(actions == 0, flag .. " must retain the native mouse input gate")
        f[flag] = nil
    end
    for _, method in ipairs({ "OnRightClick", "DoControllerAltActionButton" }) do
        local before = cancelled
        pc.placer_recipe = f.recipe
        pc[method](pc, true)
        f.aoe = true
        pc[method](pc, true)
        assert(actions == 0 and cancelled == before + 2, "placement and AOE cancellation stay native")
    end
    pc.deployplacer = entity()
    pc:DoControllerAltActionButton()
    assert(actions == 0 and cancelled == 5, "controller deployment cancellation stays native")
    pc:OnRightClick(false)
    assert(released == 1, "native release clears the held control")
    pc:OnRightClick(true)
    pc:DoControllerAltActionButton()
    assert(actions == 2, "unblocked mouse and controller till actions still dispatch")
end, false)

T.done()
