local M = {}

local colours = {
    heat = { 1, .3, .2, .7 }, cold = { .3, .55, 1, .7 }, light = { .3, 1, 1, .7 },
    attack = { 1, .35, .75, .7 }, minimum = { 1, .35, .75, .7 }, trigger = { 1, .35, .75, .7 },
    tend = { .3, 1, .5, .7 }, shade = { .3, 1, .5, .7 }, heal = { .3, 1, .5, .7 },
    protection = { .3, 1, .5, .7 }, default = { 1, .85, .35, .7 },
}
local aliases = {
    boat_magnet_kit = "boat_magnet", carnivalgame_wheelspin_kit = "carnivalgame_wheelspin_station",
    dug_trap_starfish = "trap_starfish", eyeturret_item = "eyeturret", gelblob_storage_kit = "gelblob_storage",
    lureplantbulb = "lureplant", waterpump_item = "waterpump", deerclopseyeball_sentryward_kit = "deerclopseyeball_sentryward",
    vault_pillar_guard_constr_plans = "vault_pillar_guard_constr", oceantreenut = "oceantree",
}

function M.alias(prefab)
    if type(prefab) ~= "string" then return nil end
    prefab = prefab:gsub("_placer$", "")
    return aliases[prefab] or prefab
end

local function finite(value)
    return type(value) == "number" and value > 0 and value < math.huge
end

local function value(tuning, key)
    if type(key) == "number" then return key end
    for part in key:gmatch("[^.]+") do
        tuning = type(tuning) == "table" and tuning[part] or nil
    end
    return tuning
end

-- Each entry names the native query being drawn; conditions stay visible in its label.
local catalog = {}
local function define(prefabs, radius, kind, zh, en, source, precision, owner)
    for prefab in prefabs:gmatch("%S+") do
        local list = catalog[prefab] or {}
        catalog[prefab] = list
        list[#list + 1] = { radius, kind, zh, en, source, precision or "rule", owner == true }
    end
end

-- Placement, detection, interaction and combat.
define("boat_magnet boat_magnet_beacon", "BOAT.BOAT_MAGNET.PAIR_RADIUS", "connection", "航行信标配对", "Beacon pairing", "components/boatmagnet.lua")
define("firesuppressor", "FIRE_DETECTOR_RANGE", "detection", "火情检测", "Fire detection", "components/firedetector.lua")
define("lightning_rod", 40, "lightning", "天气落雷吸引", "Weather lightning attraction", "components/weather.lua")
define("winona_catapult", "WINONA_CATAPULT_TARGETING_MIN_RANGE", "minimum", "最小索敌", "Minimum targeting", "prefabs/winona_catapult.lua", "target_radius")
define("winona_catapult", "WINONA_CATAPULT_MAX_RANGE", "attack", "最大索敌", "Maximum targeting", "prefabs/winona_catapult.lua", "target_radius")
define("trap_teeth trap_teeth_maxwell", "TRAP_TEETH_RADIUS", "trigger", "陷阱触发", "Trap trigger", "prefabs/trap_teeth.lua")
define("beemine beemine_maxwell", "BEEMINE_RADIUS", "trigger", "陷阱触发", "Trap trigger", "prefabs/beemine.lua")
define("waterpump", "WATERPUMP.MAXRANGE", "detection", "水上火情检测", "Fire detection while afloat", "prefabs/waterpump.lua")
define("crabking_cannontower", 12, "attack", "炮塔目标搜索", "Cannon target search", "prefabs/crabking_cannontower.lua")
define("crabking_cannontower", 18, "detection", "炮塔船只搜索", "Cannon boat search", "prefabs/crabking_cannontower.lua")
define("deer_blue deer_red", "DEER_GEMMED_CAST_MAX_RANGE", "attack", "法阵施放上限", "Spell placement limit", "prefabs/deer.lua", "upper_bound")
define("fence_electric", "ELECTRIC_FENCE_MAX_DIST", "connection", "电网节点连接", "Electric fence connection", "components/electricconnector.lua")
define("gunpowder slurtleslime", 3, "attack", "爆炸目标查询", "Explosion target query", "components/explosive.lua")
define("leif_idol", "LEIF_IDOL_SPAWN_RADIUS", "protection", "树木召唤检查", "Tree summon check", "prefabs/leif_idol.lua", nil, true)
define("leif_idol", "LEIF_REAWAKEN_RADIUS", "protection", "树人唤醒", "Treeguard awakening", "prefabs/leif_idol.lua", nil, true)
define("lunarthrall_plant grass sapling sapling_moon", "LUNARTHRALL_PLANT_RANGE", "attack", "寄生后索敌", "Targeting when infested", "prefabs/lunarthrall_plant.lua", "conditional")
define("lunarthrall_plant grass sapling sapling_moon", 30, "protection", "寄生植物排斥新感染", "Infested plant excludes new infestation", "components/lunarthrall_plantspawner.lua", "conditional")
define("moon_altar moon_altar_astral moon_altar_cosmic moon_fissure", "MOON_ALTAR_ESTABLISH_LINK_RADIUS", "connection", "祭坛连接上限", "Altar connection limit", "components/moonaltarlinktarget.lua")
define("moon_altar_link moon_device_construction1 moon_device_construction2 moon_device_construction3", 19, "attack", "仪式陨石落点上限", "Ritual meteor landing limit", "prefabs/moon_device.lua", "upper_bound")
define("moonbase", 30, "detection", "仪式生物吸引", "Ritual creature attraction", "components/moonbeastspawner.lua", "conditional")
define("offering_pot offering_pot_upgraded", "WURT_OFFERING_POT_RANGE", "protection", "邻近鱼人住宅供奉", "Offerings to nearby merm homes", "prefabs/offering_pot.lua", "conditional")
define("pigking", "PIG_MINIGAME_ARENA_RADIUS", "detection", "摔跤场清空检查", "Wrestling arena clearance", "prefabs/pigking.lua")
define("storage_robot", "STORAGE_ROBOT_WORK_RADIUS", "detection", "收纳工作区", "Storage work area", "prefabs/storage_robot_common.lua")
define("winona_storage_robot", "STORAGE_ROBOT_WORK_RADIUS", "detection", "收纳工作区", "Storage work area", "prefabs/storage_robot_common.lua")
define("support_pillar support_pillar_dreadstone", "QUAKE_BLOCKER_RANGE", "protection", "地震落石保护", "Quake debris protection", "components/quaker.lua", "conditional")
define("tentacle", "TENTACLE_ATTACK_DIST", "attack", "触手攻击起始", "Tentacle attack start", "prefabs/tentacle.lua", "target_radius")
define("tentacle", "TENTACLE_STOPATTACK_DIST", "detection", "触手保留目标", "Tentacle target retention", "prefabs/tentacle.lua")
define("trap_starfish", "STARFISH_TRAP_RADIUS", "trigger", "海星触发", "Anenemy trigger", "prefabs/trap_starfish.lua")
define("voidcloth_umbrella", "VOIDCLOTH_UMBRELLA_DOME_RADIUS", "protection", "落地展开后避雨", "Rain dome when deployed", "prefabs/voidcloth_umbrella.lua", "conditional")
define("wagdrone_rolling", "WAGDRONE_ROLLING_WORK_RADIUS", "detection", "工作区", "Work area", "brains/wagdrone_rollingbrain.lua")
define("warg mutatedwarg", 30, "detection", "嚎叫生成猎犬", "Howl hound spawn distance", "components/hounded.lua", "conditional")
define("wasphive", 10, "attack", "杀人蜂巢警戒", "Killer bee hive alert", "prefabs/wasphive.lua")
define("winch", 2.5, "detection", "吊爪打捞", "Claw salvage", "prefabs/winch.lua")
define("vault_pillar_guard_constr", "VAULT_PILLAR_GUARD_COMBAT_RANGE", "protection", "以建造点为中心的警戒区", "Guard area around construction site", "brains/vault_pillar_guardbrain.lua")

-- Ecological rules are conditional searches, not promises of a spawn.
define("cactus oasis_cactus catcoonden cave_banana_tree flower_cave flower_cave_double flower_cave_triple lightflier_flower rabbithole red_mushroom green_mushroom blue_mushroom reeds", 20, "regrowth", "再生点周围建筑排斥检查", "Structures exclude regrowth around this point", "components/regrowthmanager.lua", "conditional")
define("flower flower_rose flower_evil", 6, "minimum", "蝴蝶生成最小玩家距离", "Minimum player distance for butterflies", "components/butterflyspawner.lua", "conditional")
define("flower flower_rose flower_evil", 25, "regrowth", "蝴蝶生成最大玩家距离", "Maximum player distance for butterflies", "components/butterflyspawner.lua", "conditional")
define("flower flower_rose flower_evil", 42, "regrowth", "蜂箱采花路径估计上限", "Estimated hive foraging reach", "brains/beecommon.lua", "estimate")
define("carnivalgame_wheelspin_station trap_starfish", 4, "protection", "阻止鸟落地", "Blocks bird landing", "components/birdspawner.lua")
define("scarecrow", "BIRD_CANARY_LURE_DISTANCE", "regrowth", "金丝雀替代乌鸦", "Canary replaces crow", "components/birdspawner.lua")
define("lureplant", 25, "detection", "吸引此范围内玩家野火候选", "Wildfire candidate for players within this radius", "components/wildfires.lua", "conditional")
define("homesign arrowsign_post", 24, "protection", "裂隙场地排斥检查", "Rift arena exclusion check", "components/map.lua", "conditional")
define("spiderden", "SPIDER_SUMMON_WARRIORS_RADIUS", "detection", "蜘蛛受击向巢求援", "Attacked spiders call this den", "prefabs/spider.lua", "conditional")
define("spiderden", 30, "regrowth", "三级巢生成女王所需玩家距离", "Player proximity required for tier-three queen", "prefabs/spiderden.lua", "conditional")
define("watertree_pillar", "SHADE_CANOPY_RANGE", "shade", "树冠遮蔽", "Canopy shelter", "components/sheltered.lua")
define("oceantree_pillar oceantree winch", "SHADE_CANOPY_RANGE_SMALL", "shade", "成熟树干树冠预估", "Future mature trunk canopy", "components/sheltered.lua", "conditional")

-- Heat falls off with distance and compares against each recipient's temperature.
define("dragonflyfurnace stafflight lava_pond", 10, "heat", "热影响查询上限（强度衰减）", "Heat query limit (strength falls off)", "components/temperature.lua", "upper_bound")
define("gelblob_storage", 10, "heat", "存放太阳鱼时热影响上限", "Heat query limit when holding a sunfish", "components/temperature.lua", "conditional")
define("staffcoldlight", 10, "cold", "极光冷却查询上限（强度衰减）", "Polar-light cooling limit (strength falls off)", "components/temperature.lua", "upper_bound")
define("moonbase", 10, "cold", "插入唤月者后冷却查询上限", "Cooling query limit with a Moon Caller staff", "components/temperature.lua", "conditional")
define("lava_pond", 5, "attack", "火焰传播伤害查询", "Fire propagation damage query", "prefabs/lava_pond.lua", "target_radius")
define("deerclopseyeball_sentryward", "DEERCLOPSEYEBALL_SENTRYWARD_GROUND_ICE_RADIUS", "cold", "启用后的结冰地面", "Frozen ground while enabled", "prefabs/deerclopseyeball_sentryward.lua", "conditional")
define("deerclopseyeball_sentryward", "DEERCLOPSEYEBALL_SENTRYWARD_RADIUS", "cold", "启用后的气温覆盖", "Temperature override while enabled", "prefabs/deerclopseyeball_sentryward.lua", "conditional")
define("deerclopseyeball_sentryward", 5, "minimum", "迷你冰川落点内界", "Minimum glacier spawn distance", "prefabs/deerclopseyeball_sentryward.lua")
define("deerclopseyeball_sentryward", "DEERCLOPSEYEBALL_SENTRYWARD_ROCK_ICE_MAX_DENSITY_RAD", "regrowth", "迷你冰川密度检查", "Glacier density check", "prefabs/deerclopseyeball_sentryward.lua")

-- Carried instruments and tools project their action around the user.
define("panflute", "PANFLUTE_SLEEPRANGE", "listeners", "催眠听众", "Sleep listeners", "prefabs/panflute.lua", nil, true)
define("horn", "HORN_RANGE", "tend", "牛群呼唤与作物照料", "Beefalo call and crop tending", "prefabs/horn.lua", nil, true)
define("houndwhistle", "HOUNDWHISTLE_RANGE", "listeners", "哨声听众", "Whistle listeners", "prefabs/houndwhistle.lua", nil, true)
define("featherfan", "FEATHERFAN_RADIUS", "cold", "降温与灭火", "Cooling and extinguishing", "prefabs/fans.lua", nil, true)
define("onemanband", "ONEMANBAND_RANGE", "tend", "招募与作物照料", "Recruitment and crop tending", "components/leaderrollcall.lua", nil, true)
define("orangeamulet", "ORANGEAMULET_RANGE", "detection", "自动拾取", "Automatic pickup", "prefabs/amulet.lua", nil, true)
define("phonograph w_radio", "PHONOGRAPH_TEND_RANGE", "tend", "音乐照料作物", "Musical crop tending", "prefabs/phonograph.lua")
define("singingshell_octave3 singingshell_octave4 singingshell_octave5", "SINGINGSHELL_FARM_PLANT_INTERACT_RANGE", "tend", "贝壳照料作物", "Shell crop tending", "prefabs/singingshell.lua")
define("trident", "TRIDENT_FARM_PLANT_INTERACT_RANGE", "tend", "作物照料", "Crop tending", "prefabs/trident.lua", nil, true)
define("gnarwail_horn", "GNARWAIL_HORN_FARM_PLANT_INTERACT_RANGE", "tend", "作物照料", "Crop tending", "prefabs/gnarwail_horn.lua", nil, true)
define("polly_rogershat", "POLLY_ROGERS_RANGE", "detection", "鹦鹉拾取搜索", "Parrot pickup search", "brains/pollyrogerbrain.lua", nil, true)
define("spider_whistle", "SPIDER_WHISTLE_RANGE", "listeners", "蜘蛛呼唤", "Spider call", "prefabs/spider_whistle.lua", nil, true)
define("thurible", "THURIBLE_AOE_RANGE", "protection", "避免骨牢", "Bone cage prevention", "prefabs/stalker.lua")
define("voidcloth_boomerang", "VOIDCLOTH_BOOMERANG_ATTACK_DIST", "attack", "回旋镖攻击距离", "Boomerang attack distance", "prefabs/voidcloth_boomerang.lua", "target_radius", true)

-- Books with global effects have no local boundary to draw.
define("book_birds", 6, "minimum", "落鸟最小距离", "Minimum bird landing distance", "components/birdspawner.lua", nil, true)
define("book_birds", 12, "regrowth", "落鸟最大距离", "Maximum bird landing distance", "components/birdspawner.lua", nil, true)
define("book_fire", "BOOK_FIRE_RADIUS", "protection", "灭火对象查询", "Fire suppression query", "prefabs/books.lua", nil, true)
define("book_fish", 10 + math.sqrt(18), "regrowth", "鱼群中心外边界（个体可更远）", "School centre limit (individual fish may spread farther)", "prefabs/books.lua", "upper_bound", true)
define("book_gardening book_silviculture book_sleep book_horticulture book_horticulture_upgraded", 30, "tend", "阅读效果对象查询", "Reading effect target query", "prefabs/books.lua", nil, true)
define("book_light book_light_upgraded", 3, "light", "光源标称半径（非安全边界）", "Nominal light radius (not a safety boundary)", "prefabs/booklight.lua", "estimate", true)
define("book_rain", math.sqrt(32), "tend", "局部浇地正方形外接圆", "Envelope of square soil-watering area", "prefabs/books.lua", "upper_bound", true)
define("book_research_station", "BOOK_RESEARCH_STATION_RADIUS", "tend", "临时科技共享", "Temporary technology sharing", "prefabs/books.lua", nil, true)
define("book_temperature", "BOOK_TEMPERATURE_RADIUS", "tend", "温湿调节", "Temperature and moisture adjustment", "prefabs/books.lua", nil, true)
define("book_tentacles", 3, "minimum", "触手生成最小距离", "Minimum tentacle spawn distance", "prefabs/books.lua", nil, true)
define("book_tentacles", 8, "attack", "触手生成最大距离", "Maximum tentacle spawn distance", "prefabs/books.lua", nil, true)
define("book_web", "BOOK_WEB_GROUND_RADIUS", "protection", "蛛网地面", "Webbed ground", "prefabs/book_web_ground.lua", nil, true)
local songs = "battlesong_durability battlesong_healthgain battlesong_sanitygain battlesong_sanityaura battlesong_fireresistance battlesong_shadowaligned battlesong_lunaraligned"
define(songs, "BATTLESONG_ATTACH_RADIUS", "tend", "战歌加入范围", "Battle song attachment", "components/singinginspiration.lua", nil, true)
define(songs, "BATTLESONG_DETACH_RADIUS", "tend", "战歌失效距离", "Battle song detachment", "components/singinginspiration.lua", nil, true)
define("battlesong_instant_taunt battlesong_instant_panic battlesong_instant_revive", "BATTLESONG_ATTACH_RADIUS", "tend", "即兴战歌对象查询", "Instant battle song target query", "components/singinginspiration.lua", nil, true)

local fixed_centres = { storage_robot = true, winona_storage_robot = true, wagdrone_rolling = true, vault_pillar_guard = true }

function M.describe(tuning, prefab, target, player)
    prefab = M.alias(prefab)
    local result = {}
    local point
    if (prefab == "storage_robot" or prefab == "winona_storage_robot") and target
        and target.GetCurrentPlatform and target:GetCurrentPlatform() == nil
        and target._originx and target._originz then
        local x, z = target._originx:value(), target._originz:value()
        if type(x) == "number" and type(z) == "number" and math.abs(x) < math.huge and math.abs(z) < math.huge then
            point = { x = x, z = z }
        end
    end
    local function add(radius, kind, zh, en, source, precision, owner)
        if finite(radius) then
            if fixed_centres[prefab] and not point then
                zh, en = zh .. "（当前点固定参考，真实中心未知）", en .. " (fixed current-point reference; true centre unknown)"
                precision = "estimate"
            end
            result[#result + 1] = { radius = radius, kind = kind, zh = zh, en = en,
                source = source, precision = precision or "rule", owner = owner == true,
                color = colours[kind] or colours.default, anchor = not point and fixed_centres[prefab] and "pinned" or nil, point = point }
        end
    end
    for _, rule in ipairs(catalog[prefab] or {}) do
        add(value(tuning, rule[1]), rule[2], rule[3], rule[4], rule[5], rule[6], rule[7])
    end
    if prefab == "wortox_soul" then
        local radius = tuning.WORTOX_SOULHEAL_RANGE
        if finite(radius) then
            -- This is the local player's release preview; an existing soul's owner modifiers are server-private.
            local skills = player and player.components and player.components.skilltreeupdater
            for _, tier in ipairs({ 1, 2 }) do
                local bonus = value(tuning, "SKILLS.WORTOX.WORTOX_SOULPROTECTOR_" .. tier .. "_RANGE")
                if skills and skills:IsActivated("wortox_soulprotector_" .. tier) and finite(bonus) then radius = radius + bonus end
            end
            add(radius, "heal", "自己释放灵魂时的治疗区", "Healing area when you release a soul", "prefabs/wortox_soul.lua", "conditional", true)
        end
    elseif prefab == "winona_battery_low" or prefab == "winona_battery_high" then
        local radius, footprint = tuning.WINONA_BATTERY_RANGE, tuning.WINONA_ENGINEERING_FOOTPRINT
        if finite(radius) and finite(footprint) then
            add(radius - footprint, "connection", "同平台标准工程连接", "Standard engineering connection on same platform", "components/circuitnode.lua")
        end
    elseif prefab == "eyeturret" then
        if finite(tuning.EYETURRET_RANGE) then
            add(tuning.EYETURRET_RANGE + 3, "detection", "索敌", "Target acquisition", "prefabs/eyeturret.lua")
        end
    elseif prefab == "vault_pillar_guard" or prefab == "vault_pillar_guard_dormant" then
        local tag = prefab == "vault_pillar_guard" and "player_aligned" or "structure"
        if target and target.HasTag and target:HasTag(tag) then
            add(tuning.VAULT_PILLAR_GUARD_COMBAT_RANGE, "protection", "守卫建造点警戒半径", "Guard construction-site alert radius", "brains/vault_pillar_guardbrain.lua")
        end
    elseif prefab == "spiderden" then
        local radius, multiplier = tuning.SPIDER_SUMMON_WARRIORS_RADIUS, tuning.SPRING_COMBAT_MOD
        if finite(radius) and finite(multiplier) then
            add(radius * multiplier, "detection", "春季蜘蛛受击向巢求援", "Spring attacked spiders call this den", "prefabs/spider.lua", "conditional")
        end
    elseif prefab == "monkeyqueen" then
        local bands = tuning.PIRATESPAWNER or {}
        for _, band in ipairs({ { "INNER", "近区", "Inner" }, { "MID", "中区", "Middle" }, { "OUTTER", "远区", "Outer" } }) do
            add(bands[band[1]] and bands[band[1]].MAX, "detection", "海盗袭击权重" .. band[2], band[3] .. " pirate raid weight zone", "components/piratespawner.lua", "conditional")
        end
    elseif prefab == "mushroom_light" or prefab == "mushroom_light2" then
        local light = target and target.Light
        local radius = light and (not light.IsEnabled or light:IsEnabled()) and light.GetCalculatedRadius and light:GetCalculatedRadius()
        if finite(radius) then
            add(radius, "light", "当前光照渲染边界（非安全边界）", "Current rendered light boundary (not a safety boundary)", "prefabs/mushroom_light.lua", "estimate")
        else
            add(5.5, "light", "满载光源标称半径（非安全边界）", "Full nominal light radius (not a safety boundary)", "prefabs/mushroom_light.lua", "estimate")
        end
    elseif prefab == "winona_spotlight" then
        add(tuning.WINONA_SPOTLIGHT_MAX_RANGE, "light", "普通照明跟踪范围", "Normal light tracking range", "prefabs/winona_spotlight.lua", "conditional")
        add(value(tuning, "SKILLS.WINONA.SPOTLIGHT_MAX_RANGE2"), "light", "宽敞技能照明跟踪范围", "Spacious skill light tracking range", "prefabs/winona_spotlight.lua", "conditional")
    end
    return result
end

return M
