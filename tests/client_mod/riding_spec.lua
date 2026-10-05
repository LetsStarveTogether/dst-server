local T = require("tests.client_mod.support")
local Information = require("fieldkit/game/information")
local Scope = require("fieldkit/core/scope")
local tuning = { BEEFALO_MIN_BUCK_TIME = 50, BEEFALO_MAX_BUCK_TIME = 800,
    BEEFALO_BUCK_TIME_MOOD_MULT = .2, BEEFALO_BUCK_TIME_NUDE_MULT = .2,
    BEEFALO_BUCK_TIME_UNDOMESTICATED_MULT = .3,
    SKILLS = { WATHGRITHR = { WATHGRITHR_BEEFALO_BUCK_TIME_MOD = 1.3 } } }

T.case("domestication inverse matches native buck scheduling", function()
    local env = { TUNING = tuning, fns = { GetIsInMood = function(inst) return inst.mood end } }
    T.native("mathutil", env)
    local delay = T.eval(assert(T.source("prefabs/beefalo"):match("local function CalculateBuckDelay%b().-\nend"))
        .. "\nreturn CalculateBuckDelay", env)
    for _, value in ipairs({ 0, .35, 1 }) do
        for _, domestic in ipairs({ false, true }) do
            for _, beard in ipairs({ false, true }) do
                for _, mood in ipairs({ false, true }) do
                    for _, skilled in ipairs({ false, true }) do
                        local rider = { components = { skilltreeupdater = { HasSkillTag = function() return skilled end } } }
                        local mount = { mood = mood, components = {
                            domesticatable = { GetDomestication = function() return value end, IsDomesticated = function() return domestic end },
                            beard = { bits = beard and 1 or 0 }, rideable = { GetRider = function() return rider end },
                        } }
                        local lower, upper = Information.ride_window(tuning, domestic, beard, mood, skilled)
                        local low, high = Information.domestication(delay(mount), lower, upper)
                        assert(low <= value and high >= value, "all native multipliers must be inverted together")
                    end
                end
            end
        end
    end
    assert(Information.domestication(0 / 0, 15, 240) == nil and Information.domestication(300, 15, 240) == nil)
end)

T.case("ride history uses buck onset and rejects incomplete evidence", function()
    local now, mounted, animation, records = 0, false, "idle_loop", {}
    local player = T.entity({ prefab = "wilson" })
    local tasks = T.tasks(player)
    local mount = T.entity({ prefab = "beefalo", name = "Beefalo", tags = { beefalo = true, has_beard = true } })
    player.replica.rider = { IsRiding = function() return mounted end, GetMount = function() return mount end }
    player.AnimState = { GetCurrentAnimationName = function() return animation end }
    player.components.skilltreeupdater = { HasSkillTag = function() return false end }
    local ctx = { player = player, scope = Scope.new(player, error), config = { ride_timer = true },
        journal = { add = function(_, kind, record) assert(kind == "ride"); records[#records + 1] = record end, flush = function() end },
        G = { TheWorld = T.entity({ state = {} }), TUNING = tuning, GetTime = function() return now end,
            ACTIONS = { WALKTO = {}, GIVE = {} } } }
    Information.new(ctx)
    local function tick() tasks[#tasks].fn() end
    local function start()
        now, animation, mounted = now + 10, "mount", true
        player:PushEvent("isridingdirty")
        player:PushEvent("performaction") -- Native remote mount acknowledgement while the mount animation is active.
        animation = "idle_loop"
    end
    local function finish(duration)
        now, animation = now + duration, "buck"; tick()
        now, mounted, animation = now + 1.2, false, "bucked"
        player:PushEvent("isridingdirty")
    end
    start(); finish(127.5)
    assert(#records == 1 and records[1].duration == 127.5 and records[1].low < .5 and records[1].high > .5,
        "the post-buck dismount animation cannot inflate the inferred domestication")
    for _, event in ipairs({ "attacked", "feedmount", "performaction" }) do
        start(); player:PushEvent(event); finish(127.5)
        assert(records[#records].low == nil, "actions which may reset or force a buck invalidate the estimate")
    end
    start(); mount.tags.has_beard = nil; tick(); finish(30)
    assert(records[#records].low == nil, "changing public buck multipliers invalidates the estimate")
    mount.tags.has_beard = true
    start(); now, mounted = now + 20, false; player:PushEvent("isridingdirty")
    assert(records[#records].state == "dismounted" and records[#records].low == nil)
    local count = #records
    ctx.scope:close(); assert(#records == count)
    mounted, animation = true, "idle_loop"
    ctx.scope = Scope.new(player, error)
    Information.new(ctx); finish(127.5)
    assert(records[#records].low == nil, "joining while mounted cannot infer the missing start")
    start(); ctx.scope:close()
    assert(records[#records].state == "interrupted" and T.listener_count(player) == 0)
end)
T.done()
