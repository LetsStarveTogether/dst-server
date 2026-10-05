local T = require("tests.client_mod.support")
T.case("metrics contracts", function()
    local Metrics = require("fieldkit/game/metrics")
    local function component(path)
        return T.native("components/" .. path, { Class = T.Class, require = function() return {} end })
    end
    local function net(value) return { value = function() return value end } end
    local tags = {}
    local player = { components = {}, replica = {}, HasTag = function(_, tag) return tags[tag] end,
        GetTemperature = function() return 21 end, GetMoisture = function() return 0 end,
        GetMaxMoisture = function() return 100 end }
    local ctx = { player = player, G = { TUNING = { MIGHTINESS_MAX = 100, INSPIRATION_MAX = 100,
        WANDA_MIN_YEARS_OLD = 20, WANDA_MAX_YEARS_OLD = 80,
        SKILLS = { WOLFGANG_OVERBUFF_5 = 50 } } }, hud = { controls = { status = {}, secondary_status = {} } } }
    local own = Metrics.snapshot(ctx)
    assert(own.health.quality == "unknown" and own.temperature.value == nil and own.moisture.value == nil)
    assert(own.boat_health == nil and own.pet_health == nil and own.energy == nil and own.age == nil)

    local Health = component("health_replica")
    local health = setmetatable({ inst = player, classified = { currenthealth = net(75), maxhealth = net(150) } }, { __index = Health })
    player.replica.health = health
    own = Metrics.snapshot(ctx)
    assert(own.health.value == 75 and own.health.max == 150 and own.health.quality == "known")
    player.prefab = "wanda"
    assert(Metrics.get(ctx, "age").value == 50 and Metrics.get(ctx, "age").max == 80)
    ctx.hud.controls.status.heart = {}
    assert(Metrics.widget(ctx, ctx.hud.controls.status.heart) == "age")
    health.classified = nil
    assert(Metrics.get(ctx, "age").value == nil, "unattached native health defaults do not imply an age")
    player.prefab = "wolfgang"
    assert(Metrics.widget(ctx, ctx.hud.controls.status.heart) == "health")

    -- Character getters return zero before classified attaches; that is not an observation.
    player.GetMightiness, player.GetInspiration, player.GetWereness = function() return 1.5 end,
        function() return .72 end, function() return .28 end
    tags.wereplayer, tags.wolfgang_overbuff_5 = true, true
    own = Metrics.snapshot(ctx)
    assert(own.mightiness.value == nil and own.inspiration.value == nil and own.wereness.value == nil)
    player.player_classified = { currentmightiness = {}, currentinspiration = {}, currentwereness = {} }
    own = Metrics.snapshot(ctx)
    assert(own.mightiness.value == 150 and own.mightiness.max == 150, "overbuff is scaled against the base, never clamped")
    assert(own.inspiration.value == 72 and own.inspiration.max == 100 and math.abs(own.wereness.value - 28) < .001)
    assert(own.temperature.value == 21 and own.moisture.value == 0)
    tags.wolfgang_overbuff_5, tags.wereplayer = nil, nil
    assert(Metrics.get(ctx, "mightiness").max == 100 and Metrics.get(ctx, "wereness") == nil)
    player.player_classified = nil
    player.components.mightiness, player.components.singinginspiration, player.components.wereness = {}, {}, {}
    tags.wereplayer = true
    assert(Metrics.get(ctx, "mightiness").value == 150 and Metrics.get(ctx, "inspiration").value == 72
        and math.abs(Metrics.get(ctx, "wereness").value - 28) < .001, "host components are valid sources without classified")

    local HealthSyncer = component("healthsyncer")
    local boat = { components = { healthsyncer = setmetatable({ _healthpct = net(.25), max_health = 1000 }, { __index = HealthSyncer }) } }
    player.GetCurrentPlatform = function() return boat end
    assert(Metrics.get(ctx, "boat_health").value == 250 and Metrics.get(ctx, "boat_health").max == 1000)
    boat = nil
    assert(Metrics.get(ctx, "boat_health") == nil, "leaving a platform discards its observation immediately")

    local PetHealth = component("pethealthbar")
    local pet = setmetatable({ _healthpct = net(.5), _maxhealth = net(700), _maxbonus = net(200), _bonuspct = net(.5) }, { __index = PetHealth })
    player.components.pethealthbar = pet
    local companion = Metrics.get(ctx, "pet_health")
    assert(companion.value == 350 and companion.max == 700 and companion.bonus.value == 100 and companion.bonus.max == 200,
        "bonus denotes an added health maximum; it must not be added again to current health")
    pet._maxhealth, pet._maxbonus = net(0), net(0)
    assert(Metrics.get(ctx, "pet_health").value == nil and Metrics.get(ctx, "pet_health").bonus == nil,
        "a pethealthbar constructor's initial full percentage is unavailable until its maximum arrives")
    player.pet_hunger_classified = { Max = function() return 50 end, GetPercent = function() return .4 end }
    assert(Metrics.get(ctx, "pet_hunger").value == 20)
    player.pet_hunger_classified = nil
    assert(Metrics.get(ctx, "pet_hunger") == nil)

    local Owner = component("upgrademoduleowner")
    local owner = setmetatable({ max_charge = 6, charge_level = 2, overridefullcharge = true }, { __index = Owner })
    player.components.upgrademoduleowner = owner
    T.each("native effective charge", {
        { name = "shadow power", override = true, expected = 6 }, { name = "stored", override = false, expected = 2 },
    }, function(case)
        owner.overridefullcharge = case.override
        local energy = Metrics.get(ctx, "energy")
        assert(energy.value == case.expected and energy.max == 6)
    end)
    player.components.upgrademoduleowner = nil
    player.wx78_classified = { GetEnergyLevel = function() return 4 end, GetMaxEnergy = function() return 8 end }
    assert(Metrics.get(ctx, "energy").value == 4 and Metrics.get(ctx, "energy").max == 8)
    ctx.hud.controls.secondary_status.upgrademodulesdisplay = {}
    assert(Metrics.widget(ctx, ctx.hud.controls.secondary_status.upgrademodulesdisplay) == "energy")
    assert(Metrics.widget(ctx, {}) == nil)
    player.wx78_classified = nil
    assert(Metrics.get(ctx, "energy") == nil, "a detached classified cannot leave cached power information")
end)
T.done()
