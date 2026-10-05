local T = require("tests.client_mod.support")
local native = os.getenv("FIELDKIT_GAME_SCRIPTS") or "dst-scripts/scripts"
package.path = native .. "/?.lua;" .. package.path
local Ranges = require("fieldkit/rules/ranges")
FRAMES, PI, RADIANS, DEGREES, ENTITY_POPOUT_RADIUS = 1 / 30, math.pi, 180 / math.pi, math.pi / 180, 76.8
require("tuning")
local read = T.source

-- Exercise the complete supported object set with real native tuning, including nested values.
local prefabs = [[
boat_magnet boat_magnet_beacon cactus oasis_cactus catcoonden cave_banana_tree carnivalgame_wheelspin_station
crabking_cannontower deer_blue deer_red deerclopseyeball_sentryward dragonflyfurnace eyeturret fence_electric
firesuppressor flower flower_rose flower_evil flower_cave flower_cave_double flower_cave_triple lightflier_flower
gelblob_storage gunpowder slurtleslime homesign arrowsign_post lava_pond leif_idol lightning_rod
lunarthrall_plant grass sapling sapling_moon lureplant monkeyqueen moon_altar moon_altar_astral moon_altar_cosmic
moon_fissure moon_altar_link moon_device_construction1 moon_device_construction2 moon_device_construction3
moonbase mushroom_light mushroom_light2 offering_pot offering_pot_upgraded phonograph w_radio
singingshell_octave3 singingshell_octave4 singingshell_octave5 pigking rabbithole red_mushroom green_mushroom
blue_mushroom reeds scarecrow spiderden staffcoldlight stafflight storage_robot winona_storage_robot
support_pillar support_pillar_dreadstone tentacle thurible trap_starfish vault_pillar_guard vault_pillar_guard_dormant
vault_pillar_guard_constr voidcloth_umbrella wagdrone_rolling warg mutatedwarg wasphive watertree_pillar
oceantree_pillar oceantree oceantreenut winch winona_catapult winona_spotlight winona_battery_low
winona_battery_high trap_teeth trap_teeth_maxwell beemine beemine_maxwell waterpump
book_birds book_fire book_fish book_gardening book_silviculture book_sleep book_horticulture
book_horticulture_upgraded book_light book_light_upgraded book_rain book_research_station book_temperature
book_tentacles book_web battlesong_durability battlesong_healthgain battlesong_sanitygain battlesong_sanityaura
battlesong_fireresistance battlesong_shadowaligned battlesong_lunaraligned battlesong_instant_taunt
battlesong_instant_panic battlesong_instant_revive featherfan horn onemanband orangeamulet panflute houndwhistle
trident gnarwail_horn polly_rogershat spider_whistle voidcloth_boomerang wortox_soul
]]
local cases = {}
for prefab in prefabs:gmatch("%S+") do cases[#cases + 1] = prefab end
T.each("native_prefab", cases, function(prefab)
    local rules = Ranges.describe(TUNING, prefab, { HasTag = function(_, tag) return tag == "player_aligned" or tag == "structure" end })
    assert(#rules > 0, "missing native rules: " .. prefab)
    for _, rule in ipairs(rules) do
        assert(rule.radius > 0 and rule.radius < math.huge, prefab)
        assert(rule.zh and rule.en and rule.precision and rule.kind and #rule.color == 4, prefab)
        assert(read(rule.source):len() > 0, "missing native source: " .. rule.source)
    end
end)
T.case("aliases_public_state_and_native_constants", function()
    local kit = Ranges.describe(TUNING, "vault_pillar_guard_constr_plans_placer")
    assert(kit[1].radius == TUNING.VAULT_PILLAR_GUARD_COMBAT_RANGE)
    assert(Ranges.alias("boat_magnet_kit_placer") == "boat_magnet")
    assert(Ranges.alias("dug_trap_starfish_placer") == "trap_starfish")
    assert(Ranges.alias(nil) == nil and #Ranges.describe(TUNING, nil) == 0)
    assert(#Ranges.describe(TUNING, "unrecognized_placer") == 0)
    assert(#Ranges.describe({}, "panflute") == 0)
    T.each("invalid_radius", { 0, -1, math.huge, 0 / 0, "15", -math.huge }, function(invalid)
        assert(#Ranges.describe({ PANFLUTE_SLEEPRANGE = invalid }, "panflute") == 0)
    end)
    local song = Ranges.describe(TUNING, "battlesong_durability")
    assert(song[1].radius == 12 and song[2].radius == 16 and song[1].owner)
    assert(Ranges.describe(TUNING, "boat_magnet")[1].radius == 24)
    local robot = Ranges.describe(TUNING, "winona_storage_robot")[1]
    assert(robot.radius == 15 and robot.anchor == "pinned" and robot.precision == "estimate")
    local platform
    local localrobot = { GetCurrentPlatform = function() return platform end,
        _originx = { value = function() return -12 end }, _originz = { value = function() return 0 end } }
    local exactrobot = Ranges.describe(TUNING, "storage_robot", localrobot)[1]
    assert(exactrobot.precision == "rule" and exactrobot.point.x == -12 and exactrobot.point.z == 0 and exactrobot.anchor == nil)
    platform = {}
    local afloat = Ranges.describe(TUNING, "storage_robot", localrobot)[1]
    assert(afloat.precision == "estimate" and afloat.point == nil and afloat.anchor == "pinned", "boat origin is private local space")
    assert(kit[1].anchor == nil and kit[1].precision == "rule")
    assert(#Ranges.describe(TUNING, "vault_pillar_guard", { HasTag = function() return false end }) == 0)
    local guard = Ranges.describe(TUNING, "vault_pillar_guard", { HasTag = function(_, tag) return tag == "player_aligned" end })[1]
    assert(guard.radius == 20 and guard.anchor == "pinned" and guard.precision == "estimate")
    assert(read("prefabs/vault_pillar_guard.lua"):find('inst:AddTag("player_aligned")', 1, true))
    assert(read("prefabs/vault_pillar_guard.lua"):find('ReplacePrefab(inst, "vault_pillar_guard")', 1, true))
    assert(Ranges.describe(TUNING, "oceantreenut_placer")[1].radius == 22)
    local queen = Ranges.describe(TUNING, "monkeyqueen")
    assert(#queen == 3 and queen[1].radius == 300 and queen[2].radius == 600 and queen[3].radius == 800)

    -- Client public light state is enough; server component access must never be required.
    local target = { Light = { GetCalculatedRadius = function() return 10.25 end },
        components = setmetatable({}, { __index = function() error("server state is not available") end }) }
    local light = Ranges.describe(TUNING, "mushroom_light", target)[1]
    assert(light.radius == 10.25 and light.precision == "estimate")
    assert(Ranges.describe(TUNING, "dragonflyfurnace", target)[1].precision == "upper_bound")
    assert(Ranges.describe(TUNING, "gelblob_storage", target)[1].precision == "conditional")
    assert(Ranges.describe(TUNING, "spiderden")[2].radius == 30)
    local spider = Ranges.describe(TUNING, "spiderden")
    assert(math.abs(spider[3].radius - 15.96) < .001)

    -- These native constants explain the intentional corrections to historical mod radii.
    assert(read("components/temperature.lua"):find("local ZERO_DISTANCE = 10", 1, true))
    assert(read("components/temperature.lua"):find("1 - dsqtoinst / ZERO_DISTSQ", 1, true))
    assert(read("prefabs/crabking_cannontower.lua"):find("local FIND_TARGET_DIST = 12", 1, true))
    assert(read("prefabs/crabking_cannontower.lua"):find("local FIND_BOAT_DIST = 18", 1, true))
    assert(read("components/regrowthmanager.lua"):find("local BASE_RADIUS = 20", 1, true))
    assert(read("components/birdspawner.lua"):find("local radius = 6 + math.random() * 6", 1, true))
    assert(read("prefabs/books.lua"):find("local size = TILE_SCALE", 1, true))
    assert(read("prefabs/books.lua"):find("Vector3(math.random(1,3), 0, math.random(1,3))", 1, true))
    assert(read("brains/vault_pillar_guardbrain.lua"):find("local range = TUNING.VAULT_PILLAR_GUARD_COMBAT_RANGE", 1, true))
    assert(read("prefabs/spider.lua"):find("SpringCombatMod(TUNING.SPIDER_SUMMON_WARRIORS_RADIUS)", 1, true))
    local winch = Ranges.describe(TUNING, "winch")[1]
    assert(winch.radius == tonumber(read(winch.source):match("local CLAW_CATCHING_RADIUS%s*=%s*([%d.]+)")),
        "claw salvage hint must match the native catching radius")
    assert(Ranges.describe(TUNING, "crabking_cannontower")[1].radius == 12)
    assert(Ranges.describe(TUNING, "crabking_cannontower")[2].radius == 18)
    assert(Ranges.describe(TUNING, "book_birds")[1].radius == 6 and Ranges.describe(TUNING, "book_birds")[2].radius == 12)
    assert(Ranges.describe(TUNING, "book_rain")[1].radius == math.sqrt(32))
end)

-- The local player's skill tree exists on clients; dropped souls' modifiers do not replicate.
local private_soul = { components = setmetatable({}, { __index = function() error("private soul state") end }) }
local heals, distance = 0, 0
AllPlayers = { { entity = { IsVisible = function() return true end }, HasTag = function() return false end,
    GetDistanceSqToPoint = function() return distance^2 end,
    components = { health = { IsDead = function() return false end, IsHurt = function() return true end,
        DoDelta = function() heals = heals + 1 end } } } }
local native_soul = { Transform = { GetWorldPosition = function() return 0, 0, 0 end } }
local heal = require("prefabs/wortox_soul_common").DoHeal
T.each("soul_skill_tier", { 0, 1, 2 }, function(tier)
    local selected = {}
    for level = 1, tier do selected["wortox_soulprotector_" .. level] = true end
    local player = { components = { skilltreeupdater = { IsActivated = function(_, skill) return selected[skill] end } } }
    local rule = Ranges.describe(TUNING, "wortox_soul", private_soul, player)[1]
    assert(rule.radius == TUNING.WORTOX_SOULHEAL_RANGE + tier * 3 and rule.owner)
    native_soul.soul_heal_range_modifier = rule.radius - TUNING.WORTOX_SOULHEAL_RANGE
    distance, heals = rule.radius - .01, 0
    heal(native_soul); assert(heals == 1, "native healing reaches just inside the displayed boundary")
    distance, heals = rule.radius + .01, 0
    heal(native_soul); assert(heals == 0, "native healing excludes targets beyond the displayed boundary")
end)
assert(Ranges.describe(TUNING, "wortox_soul")[1].radius == TUNING.WORTOX_SOULHEAL_RANGE)
assert(read("prefabs/wortox_soul.lua"):find("+ TUNING.SKILLS.WORTOX.WORTOX_SOULPROTECTOR_1_RANGE", 1, true))
assert(read("prefabs/wortox_soul.lua"):find("+ TUNING.SKILLS.WORTOX.WORTOX_SOULPROTECTOR_2_RANGE", 1, true))
T.done()
