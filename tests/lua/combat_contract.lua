return function(entity, advance, records)
    require("mathutil")
    local Combat, Health = require("components/combat"), require("components/health")
    IsRangedWeapon = function() return false end
    local zero, one = {Get = function() return 0 end}, {Get = function() return 1 end}
    local function fighter(prefab, guid, userid)
        local inst = entity(prefab, guid, userid)
        inst.components.combat = setmetatable({inst = inst, externaldamagetakenmultipliers = one}, {__index = Combat})
        inst.components.health = setmetatable({
            inst = inst, currenthealth = 100, maxhealth = 100, penalty = 0, nonlethal_pct = 0,
            playerabsorb = 0, absorb = 0, externalabsorbmodifiers = zero, externalreductionmodifiers = zero,
        }, {__index = Health})
        if userid then TheWorld:PushEvent("ms_playerjoined", inst) end
        return inst
    end
    local a, b = fighter("wilson", 30, "KU_A"), fighter("wendy", 31, "KU_B")
    local spider, swarm = fighter("spider", 32), fighter("spider", 33)
    local bear, twin = fighter("bearger", 34), fighter("bearger", 35)
    bear.tags.epic, twin.tags.epic = true, true
    local spear = entity("spear", 36)
    local function hit(source, target, weapon) target.components.combat:GetAttacked(source, 2, weapon) end
    local function contacts() return records("dst.player.combat_contact") end
    local function step(seconds, paused)
        for _ = 1, seconds * 30 do advance(paused) end
    end

    b.components.health.invincible = true
    hit(a, b)
    b.components.health.invincible, b.components.health.absorb = false, 1
    hit(a, b)
    b.components.health.absorb = 0
    assert(b.components.health.currenthealth == 100)
    -- A native PvP attack notifies both players. Only the recipient can report it.
    a:PushEvent("onhitother", {target = b, damageresolved = 2})
    a:PushEvent("attacked", {attacker = a, damageresolved = 2})
    a:PushEvent("attacked", {attacker = spider, damageresolved = 2, redirected = spear})
    for _, damage in ipairs({-1, math.huge, 0 / 0}) do
        a:PushEvent("onhitother", {target = spider, damageresolved = damage})
    end
    assert(#contacts() == 0)

    while GetTick() % 30 ~= 0 do advance() end
    a.userid = nil
    hit(a, b, spear)
    a.userid = "KU_A"
    assert(b.components.health.currenthealth == 98 and #contacts() == 1)
    -- A new attacker entity with the same account shares the defender's existing window.
    hit(fighter("wolfgang", 37, a.userid), b)
    hit(b, a)
    hit(a, spider)
    hit(a, swarm)
    hit(b, swarm)
    hit(swarm, a)
    hit(a, bear)
    hit(a, twin)
    step(11, true)
    hit(a, swarm)
    step(9.5)
    hit(a, swarm)
    assert(#contacts() == 7, "a pause and a repeat at 9.5 game seconds must stay inside the original window")
    step(0.5)
    hit(a, swarm)

    local expected = {
        {a, b, "pvp", "spear"}, {b, a, "pvp"}, {a, spider, "ordinary"},
        {b, swarm, "ordinary"}, {swarm, a, "ordinary"},
        {a, bear, "epic"}, {a, twin, "epic"}, {a, swarm, "ordinary"},
    }
    local events = contacts()
    assert(#events == #expected)
    for i, pair in ipairs(expected) do
        local event = events[i]
        assert(event.source.guid == pair[1].GUID and event.source.userid == pair[1].userid)
        assert(event.target.guid == pair[2].GUID and event.target.userid == pair[2].userid)
        assert(event.kind == pair[3] and event.weapon == pair[4])
    end

    NewUpdatingEnts, num_updating_ents = {}, 0
    ProfileStatsAdd = function() end
    TUNING = TUNING or {}
    TUNING.MAX_FIRE_DAMAGE_PER_SECOND = 120
    local conditions = records("dst.player.condition_changed")
    local function exposure(inst, scale)
        local hp = inst.components.health
        hp.fire_damage_scale, hp.externalfiredamagemultipliers = scale, one
        hp.fire_timestart, hp.firedamagecaptimer = 1, 0
        local before = #conditions
        hp:DoFireDamage(1)
        assert(#conditions == before)
        for _ = 1, 60 do
            advance()
            hp:DoFireDamage(1)
        end
        step(1)
        hp:OnUpdate(1 / 30)
    end
    local before = #conditions
    local willow = fighter("willow", 38, "KU_WILLOW")
    exposure(willow, 0)
    assert(willow.components.health.currenthealth == 100 and #conditions == before)
    local health_before = a.components.health.currenthealth
    exposure(a, 1)
    exposure(a, 1)
    assert(a.components.health.currenthealth < health_before and #conditions == before + 4)
    for i = 1, 4 do
        local record = conditions[before + i]
        assert(record.condition == "fire_damage" and record.active == (i % 2 == 1))
        assert(record.player.userid == a.userid)
    end
end
