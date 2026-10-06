return function(entity, advance, frames)
    require("mathutil")
    local Eater = require("components/eater")
    local Health = require("components/health")
    local Temperature = require("components/temperature")
    local health = require("dst_server").health
    local food = entity("berries", 100)
    food.tags.edible_VEGGIE = true
    food.components.edible = {OnEaten = function() end, HandleEatRemove = function() end}
    local guests = {}
    local boss = entity("bearger", 201)
    boss.tags.epic = true
    local hit = {target = boss, damageresolved = 10}
    local tick, ms = GetTick(), GetTimeReal()
    for i = 1, 64 do
        local guest = entity("wilson", 100 + i, "KU_" .. i)
        guests[i] = guest
        TheWorld:PushEvent("ms_playerjoined", guest)
        for _ = 1, 6 do guest:PushEvent("onhitother", hit) end
        guest.components.eater = setmetatable({inst = guest, preferseating = {"VEGGIE"}}, {__index = Eater})
    end
    for _, guest in ipairs(guests) do assert(guest.components.eater:Eat(food)) end
    assert(#frames == 4)
    local function record(i) return json.decode(frames[i]:sub(10)) end
    CanEntityBecomeCorpse = function() return false end
    NotifyPlayerProgress = function() end
    local function kill(victim)
        local hp = setmetatable({
            inst = victim, currenthealth = 1, maxhealth = 1, penalty = 0, nonlethal_pct = 0, nofadeout = true,
        }, {__index = Health})
        hp:DoDelta(-1, nil, "combat", true, guests[64], true)
        assert(hp:IsDead())
    end
    kill(entity("frog", 300))
    Temperature.SetTemperature({inst = guests[2], current = 1, mintemp = -10, maxtemp = 80, overheattemp = 70}, -1)
    assert(#frames == 5)
    local cold = record(5)
    assert(cold.event == "dst.player.condition_changed" and cold.data.player.userid == "KU_2")
    assert(cold.data.condition == "freezing" and cold.data.active and cold.tick == tick)
    kill(guests[1])
    kill(boss)
    assert(#frames == 7 and record(6).data.victim.userid == "KU_1" and record(7).data.is_epic)
    for i, guest in ipairs(guests) do guest.userid = "KU_NEW_" .. i end
    food.prefab = "rot"
    local next_session = '新的"session"'
    TheWorld.state.cycles, TheWorld.meta.session_identifier = 2, next_session

    local function next_batch(paused)
        local tasks = TheWorld.pendingtasks or {}
        local task = assert(next(tasks), "backlog needs a scheduled task")
        assert(not next(tasks, task), "only one task should wait between ticks")
        local before = #frames
        advance(paused)
        local sent = #frames - before
        assert(sent >= 1 and sent <= 4, "a static tick must emit between one and four deferred results")
    end
    next_batch()
    local after_batch, craft_tick = #frames, GetTick()
    guests[64]:PushEvent("builditem", {recipe = {name = "axe"}, item = entity("axe", 200)})
    assert(#frames == after_batch, "new work must wait behind older meals")
    -- Static ticks continue while gameplay is paused.
    while next(TheWorld.pendingtasks) do next_batch(true) end
    assert(#frames == 133 and health().errors == 0 and health().play_queue_dropped == 0)
    local meals, contacts = 0, 0
    for i = 1, #frames do
        local event = record(i)
        assert(event.seq == i)
        if event.event == "dst.player.ate" then
            meals = meals + 1
            assert(event.tick == tick and event.monotonic_ms == ms)
            assert(event.session_id == "SESSION" and event.cycle == 1)
            assert(event.data.player.userid == "KU_" .. meals and event.data.food == "berries")
        elseif event.event == "dst.player.combat_contact" then
            contacts = contacts + 1
            assert(event.data.source.userid == "KU_" .. contacts and event.data.target.guid == boss.GUID)
        end
    end
    assert(meals == 64 and contacts == 64)
    local kill_record = record(#frames - 1)
    assert(kill_record.event == "dst.entity.death" and kill_record.data.victim.guid == 300)
    assert(kill_record.data.afflicter.userid == "KU_64" and kill_record.tick == tick and kill_record.monotonic_ms == ms)
    assert(kill_record.session_id == "SESSION" and kill_record.cycle == 1)
    local craft = record(#frames)
    assert(craft.event == "dst.player.crafted" and craft.tick == craft_tick)
    assert(craft.data.player.userid == "KU_NEW_64" and craft.session_id == next_session and craft.cycle == 2)

    -- Four can be sent now and another 128 can wait. Native meals still succeed after that.
    advance()
    local before = #frames
    for i = 1, 64 do kill(entity("spider", 300 + i)) end
    assert(#frames == before + 4)
    for _ = 1, 69 do assert(guests[1].components.eater:Eat(food)) end
    assert(#frames == before + 4 and health().faults["play.queue"] == "queue_full" and health().errors == 1)
    while next(TheWorld.pendingtasks) do next_batch() end
    assert(#frames == before + 132 and health().telemetry_status == "active" and health().play_queue_dropped == 1)
    for i = 1, 64 do assert(record(before + i).data.victim.guid == 300 + i) end
    advance()
    before = #frames
    for _ = 1, 11 do assert(guests[1].components.eater:Eat(food)) end
    local schedule = TheWorld.DoStaticTaskInTime
    TheWorld.DoStaticTaskInTime = function() return nil end
    advance() -- Four more meals go out, then scheduling the remaining three fails.
    assert(#frames == before + 8 and not next(TheWorld.pendingtasks) and health().play_queue_dropped == 4)
    assert(guests[1].components.eater:Eat(food) and health().play_queue_dropped == 5)
    TheWorld.DoStaticTaskInTime = schedule
    local static_tick = GetStaticTick
    GetStaticTick = nil
    assert(guests[1].components.eater:Eat(food) and health().play_queue_dropped == 6)
    GetStaticTick = static_tick
    advance()
    assert(guests[1].components.eater:Eat(food) and #frames == before + 9)
    io.write("ok\n")
end
