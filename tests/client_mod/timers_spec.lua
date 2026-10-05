local T = require("tests.client_mod.support")
local Rules = require("fieldkit/rules/timers")
local Scope = require("fieldkit/core/scope")
local tuning = { TOTAL_DAY_TIME = 480, WORMLIGHT_DURATION = 240, BUFF_ELECTRICATTACK_DURATION = 480,
    BUFF_ATTACK_DURATION = 240, FOOD_TEMP_LONG = 480, LUNARHAIL_EVENT_TIME = 90,
    BEEQUEEN_RESPAWN_TIME = 9600, MALBATROSS_SPAWNDELAY_BASE = 500, MALBATROSS_SPAWNDELAY_RANDOM = 100 }

T.case("native weather curves and uncertain modes", function()
    local source = T.source("components/weather")
    local moisture, ceiling, floor, peak = 900, 1000, 400, .65
    local function net(fn) return { value = fn } end
    local calculate = T.eval(assert(source:match("local function CalculatePrecipitationRate%b().-\n    end"))
        .. "\nreturn CalculatePrecipitationRate", {
        _precipmode = net(function() return 0 end), _preciptype = net(function() return 1 end),
        PRECIP_MODES = { always = 2, never = 3 }, PRECIP_TYPES = { none = 0 },
        _moisture = net(function() return moisture end), _moisturefloor = net(function() return floor end),
        _moistureceil = net(function() return ceiling end), _peakprecipitationrate = net(function() return peak end),
        MIN_PRECIP_RATE = .1, PI = math.pi,
    })
    local weather, state, result = Rules.weather(), { precipitation = "rain", season = "spring" }
    for i = 0, 8 do
        state.moisture, state.moistureceil, state.precipitationrate = moisture, ceiling, calculate()
        result = weather:update(state, i * .5, tuning)
        if i < 8 then moisture = moisture - calculate() * 5 end
    end
    local seconds = 0
    while moisture > floor do moisture = moisture - calculate() * .01; seconds = seconds + .001 end
    assert(result.low and result.high and result.low <= seconds and result.high >= seconds,
        "observed native precipitation must fall inside the estimated interval")
    state = { precipitation = "none", season = "spring", moistureceil = 1000, precipitationrate = 0 }
    weather:reset()
    for i = 0, 2 do state.moisture = 100 + i; result = weather:update(state, i, tuning) end
    assert(result.low == 898 and result.high == 898)
    state.moisture = 120
    assert(weather:update(state, 3, tuning).low == nil, "changed slope invalidates the prediction")
    state.season = "summer"
    assert(weather:update(state, 4, tuning).low == nil)
    weather:reset()
    state.precipitation, state.moisture, state.precipitationrate = "rain", 100, .6
    for i = 1, 5 do assert(weather:update(state, i, tuning).low == nil, "constant precipitation is not a natural rain clock") end
    state.precipitation, state.lunarhaillevel = "lunarhail", 50
    assert(weather:update(state, 6, tuning).low == 45)
end)

T.case("food rules and native respawn variance", function()
    local effects = Rules.food(tuning, "voltgoatjelly_spice_chili", { temperature = 10, temperatureduration = 120 })
    assert(#effects == 3 and effects[1].duration == 480 and effects[2].duration == 240 and effects[3].key == "temperature")
    assert(#Rules.food({}, "voltgoatjelly") == 0 and Rules.food(tuning, "glowberrymousse")[1].duration == 960)
    local cold = Rules.food({ COLD_FOOD_BONUS_TEMP = -10, FOOD_TEMP_BRIEF = 30 }, "ice")
    assert(cold[1].id == "cool" and cold[1].key == "temperature" and cold[1].duration == 45)
    local low, high = Rules.respawn(tuning, "malbatross")
    assert(low == 400 and high == 600 and Rules.respawn({}, "malbatross") == nil)
    assert(T.source("components/malbatrossspawner"):find("GetRandomWithVariance(TUNING.MALBATROSS_SPAWNDELAY_BASE, TUNING.MALBATROSS_SPAWNDELAY_RANDOM)", 1, true))
end)

T.case("consumption receipts, death and observed boss lifecycle", function()
    package.loaded.cooking = { recipes = { cookpot = { voltgoatjelly_spice_chili = {} } } }
    local now, writes, entities, animation, rpc_args = 10, 0, {}, "idle_loop", nil
    local player = T.entity({ prefab = "wilson", userid = "KU_TEST" })
    T.tasks(player)
    player.AnimState = { GetCurrentAnimationName = function() return animation end }
    local controller = { DoAction = function() end, OnControl = function() end }
    player.components.playercontroller = controller
    local world = T.entity({ meta = { session_identifier = "world" }, state = {
        cycles = 2, time = .5, precipitation = "none", season = "autumn", moisture = 5, moistureceil = 100, precipitationrate = 0,
    } })
    local G = { TheWorld = world, TUNING = tuning, GetTime = function() return now end,
        ACTIONS = { EAT = { code = 1 }, GIVE = { code = 2 } },
        RPC = { UseItemFromInvTile = 1, ControllerUseItemOnSelfFromInvTile = 2 },
        SendRPCToServer = function(...) rpc_args = { count = select("#", ...), ... }; return 71 end,
        STRINGS = { NAMES = {} }, CanEntitySeeTarget = function(_, entity) return not entity.dark end,
        TheSim = { FindEntities = function() return entities end, GetScreenPos = function(_, x, _, z) return x, z end,
            GetScreenSize = function() return 100, 100 end } }
    local store = { ready = true, data = {} }
    function store:get(key) return self.data[key] end
    function store:set(key, value) self.data[key], writes = value, writes + 1; return true end
    function store:on_ready(fn) fn(); return function() end end
    local ctx = { G = G, player = player, scope = Scope.new(player, error), store = store }
    function ctx:text(_, en) return en end
    function ctx:report(value) error(value) end
    ctx.journal = require("fieldkit/game/journal").new(ctx)
    local original = G.SendRPCToServer
    local timers = require("fieldkit/features/timers").new(ctx)
    local food = T.entity({ prefab = "voltgoatjelly_spice_chili", count = 2 })
    food.replica.stackable = { StackSize = function() return food.count end }
    assert(G.SendRPCToServer(1, 1, food, nil, true, nil) == 71)
    assert(rpc_args.count == 6 and rpc_args[3] == food and rpc_args[5] == true, "observing RPCs preserves argument count and returns")
    player:PushEvent("performaction")
    timers:update(); assert(#timers:snapshot().buffs == 0)
    food.count = 1
    timers:update(); assert(#timers:snapshot().buffs == 0, "stack loss plus a callback needs an observed eating animation")
    animation = "quick_eat"
    timers:update(); assert(#timers:snapshot().buffs == 2)
    local applied = ctx.journal:get("buff:electricattack").due
    now, food.count, animation = 20, 2, "idle_loop"
    G.SendRPCToServer(1, 1, food)
    controller:OnControl(99, true)
    food.count, animation = 1, "eat"
    player:PushEvent("performaction"); timers:update()
    assert(ctx.journal:get("buff:electricattack").due == applied, "manual takeover cannot confirm a discarded eat request")
    world.ismastersim = true
    player:PushEvent("oneat", { food = food })
    assert(ctx.journal:get("buff:electricattack").due == now + 480, "native host consumption refreshes the effect")
    player:PushEvent("death"); player.tags.playerghost = true
    local cleared = writes
    for _ = 1, 20 do timers:update() end
    assert(#timers:snapshot().buffs == 0 and writes == cleared, "ghost polling cannot repeatedly write the profile")
    player.tags.playerghost = nil; timers:update()
    local boss = T.entity({ prefab = "beequeen", x = 50, z = 50 })
    boss.replica.health = { IsDead = function() return boss.dead end }
    entities, now = { boss }, 21; timers:update()
    entities, now = {}, 24; boss:Remove(); timers:update()
    assert(#timers:snapshot().bosses == 0, "streaming removal is not a confirmed death")
    boss = T.entity({ prefab = "beequeen", x = 50, z = 50 })
    boss.replica.health = { IsDead = function() return boss.dead end }
    entities, now = { boss }, 25; timers:update()
    boss.dead = true; boss:PushEvent("death")
    assert(#timers:snapshot().bosses == 1 and timers:snapshot().bosses[1].low == 9600)
    local alive = T.entity({ prefab = "beequeen", x = 50, z = 50 })
    entities, now = { alive }, 26; timers:update()
    assert(timers:snapshot().bosses[1].state == "seen", "a living observation supersedes an estimated cooldown")
    ctx.scope:close()
    assert(G.SendRPCToServer == original and T.listener_count(player) == 0 and T.listener_count(alive) == 0)
end)
T.done()
