local T = require("tests.client_mod.support")
local Scope = require("fieldkit/core/scope")
local Journal = require("fieldkit/game/journal")

local function fixture(saved, ready, session, owner, options)
    options = options or {}
    local state = { now = 20, writes = 0, callbacks = {}, data = { journal = saved } }
    state.master = options.master == false and "" or options.master or session or "world-a"
    local player = T.entity({ prefab = options.prefab or "wilson", userid = owner or "KU_TEST" })
    state.tasks = T.tasks(player)
    local world = T.entity({ state = { cycles = options.cycles or 3, time = options.time or .5 }, meta = { session_identifier = session or "world-a" },
        tags = { cave = options.cave == true } })
    world.net = T.entity({ components = { shardstate = { GetMasterSessionId = function() return state.master end } } })
    function state:identify(master)
        self.master = master
        world.net:PushEvent("mastersessioniddirty")
    end
    local store = { ready = ready ~= false }
    function store:get(key) return state.data[key] end
    function store:set(key, value) state.writes = state.writes + 1; state.data[key] = value; return true end
    function store:on_ready(fn)
        if self.ready then fn() else state.callbacks[fn] = true end
        return function() state.callbacks[fn] = nil end
    end
    function state:load()
        store.ready = true
        for fn in pairs(self.callbacks) do self.callbacks[fn] = nil; fn() end
    end
    local ctx = { player = player, store = store, params = {}, scope = Scope.new(player, error),
        G = { TheWorld = world, GetTime = function() return state.now end, TUNING = { TOTAL_DAY_TIME = 480 } } }
    function ctx:text(_, en) return en end
    function ctx:notify(value) state.message = value end
    function ctx:report(value) error(value) end
    ctx.journal = Journal.new(ctx)
    return ctx, state
end

T.case("world identity, personal clocks, rollback and retention", function()
    local ctx, state = fixture()
    local book = ctx.journal
    book:add("wormhole", { x = 0, z = 0, tx = 20, tz = 20, number = 1 })
    book:put({ key = "buff:light", kind = "buff", clock = "player", due = 120, state = "light" })
    book:flush()
    assert(#state.data.journal[1].entries == 2)
    for _, item in ipairs(state.data.journal[1].entries) do
        if item.kind == "buff" then assert(item.due == nil and item.remaining == 100) end
    end
    local restored, same = fixture(state.data.journal, false)
    same.now = 9000
    same:load()
    assert(restored.journal:get("buff:light").due == 9100, "time spent disconnected cannot consume a personal effect")
    local copy = restored.journal:get("buff:light")
    copy.due = 0
    assert(restored.journal:get("buff:light").due == 9100, "readers do not mutate saved observations")
    local foreign = fixture(state.data.journal, true, nil, "KU_OTHER")
    assert(foreign.journal:get("buff:light") == nil and #foreign.journal:records("wormhole") == 1,
        "personal effects belong to the character and user who consumed the food")
    -- A different world never imports its predecessor's records.
    local other, other_state = fixture(state.data.journal, true, "world-b")
    assert(#other.journal:records("wormhole") == 0)
    other.journal:add("weather", { state = "rain" }); other.journal:flush()
    assert(#other_state.data.journal == 2)
    local back = fixture(other_state.data.journal)
    assert(#back.journal:records("wormhole") == 1)
    local future = book:add("boss", { prefab = "beequeen", due = 9000 })
    assert(future and #book:records("boss") == 1)
    ctx.G.TheWorld.state.cycles = 1
    assert(#book:records("boss") == 0 and #book:records("wormhole") == 0)
    ctx.G.TheWorld.state.cycles = 4
    assert(#book:records("boss") == 0, "discarded future observations cannot reappear after a rollback")
    for i = 1, 100 do book:add("ride", { duration = i }) end
    assert(#book:records("ride") == 16)
    assert(not book:put({ kind = "ride", key = "invalid", at = 0 / 0 }))
    ctx.scope:close(); restored.scope:close(); foreign.scope:close(); other.scope:close(); back.scope:close()
end)

T.case("late profile reads preserve edits and clears", function()
    local old = { { key = "world-a:forest", master = "world-a", owner = "KU_TEST:wilson", entries = {
        { key = "buff:light", kind = "buff", at = 100, clock = "player", remaining = 55, owner = "KU_TEST:wilson" },
        { key = "hole", kind = "wormhole", at = 100, number = 9 },
        { key = "boss", kind = "boss", at = 100, name = "Old" },
    } } }
    local ctx, state = fixture(old, false)
    ctx.journal:put({ key = "boss", kind = "boss", name = "New" })
    ctx.journal:clear("wormhole")
    assert(state.writes == 0)
    state:load()
    assert(ctx.journal:get("boss").name == "New" and #ctx.journal:records("wormhole") == 0)
    assert(ctx.journal:get("buff:light").due == 75 and state.writes == 1)
    ctx.scope:close()
    local closing, pending = fixture(old, false)
    closing.scope:close(); pending:load()
    assert(pending.writes == 0, "late callbacks cannot save after scope cleanup")
end)

local function personal(remaining)
    return { key = "buff:light", kind = "buff", state = "light", at = 100, clock = "player",
        remaining = remaining or 100, owner = "KU_TEST:wilson" }
end
local function saved_effect()
    return { { key = "world-a:forest", master = "world-a", owner = "KU_TEST:wilson", entries = { personal() } } }
end
local function effect_count(saved, master, owner)
    local count = 0
    for _, book in ipairs(saved) do
        for _, record in ipairs(book.entries) do
            if record.clock == "player" and book.master == master and record.owner == owner then count = count + 1 end
        end
    end
    return count
end
local function timers(ctx)
    package.loaded.cooking = { recipes = {} }
    local G = ctx.G
    G.TUNING.WORMLIGHT_DURATION = 100
    G.RPC, G.ACTIONS = {}, { EAT = {} }
    G.SendRPCToServer = function() end
    G.TheSim = { FindEntities = function() return {} end }
    G.STRINGS = { NAMES = {} }
    ctx.player.components.playercontroller = { DoAction = function() end, OnControl = function() end }
    ctx.player.AnimState = { GetCurrentAnimationName = function() return "idle_loop" end }
    return require("fieldkit/features/timers").new(ctx)
end

T.case("personal effects follow the character across distinct shard sessions", function()
    local forest, state = fixture()
    for _, kind in ipairs({ "wormhole", "ride", "boss", "weather" }) do forest.journal:add(kind, {}) end
    forest.journal:put({ key = "buff:light", kind = "buff", state = "light", clock = "player", due = 120 })
    forest.scope:close()
    local cave, below = fixture(state.data.journal, true, "cave-a", nil, { cave = true, master = "world-a" })
    assert(cave.journal:get("buff:light") and cave.journal:get("buff:light").due == 120,
        "confirmed food effects must follow the player to a different shard session")
    for _, kind in ipairs({ "wormhole", "ride", "boss", "weather" }) do
        assert(#cave.journal:records(kind) == 0, "world observations must remain on their own shard")
    end
    below.now = 50
    cave.scope:close()
    assert(#below.data.journal == 2 and effect_count(below.data.journal, "world-a", "KU_TEST:wilson") == 1)
    local reconnect, offline = fixture(below.data.journal, false, "cave-a", nil, { cave = true, master = "world-a" })
    offline.now = 9000
    offline:load()
    assert(reconnect.journal:get("buff:light").due == 9070, "disconnect time cannot consume personal effects")
    reconnect.scope:close()
    local back = fixture(offline.data.journal)
    assert(back.journal:get("buff:light").due == 90)
    for _, kind in ipairs({ "wormhole", "ride", "boss", "weather" }) do assert(#back.journal:records(kind) == 1) end
    back.scope:close()
end)

T.each("returning from a cave preserves the latest effect state", { "expiry", "death", "refresh" }, function(kind)
    local cave, state = fixture(saved_effect(), true, "cave-a", nil, { cave = true, master = "world-a" })
    local service = timers(cave)
    if kind == "expiry" then state.now = 121; service:update()
    elseif kind == "death" then
        cave.player:PushEvent("death")
        cave.player.tags.playerghost = true
        local writes = state.writes
        for _ = 1, 3 do service:update() end
        assert(state.writes == writes, "ghost polling must not repeatedly write cleared effects")
    else
        state.now = 50
        cave.player:PushEvent("oneat", { food = { prefab = "wormlight" } })
        state.now = 60
    end
    cave.scope:close()
    local back = fixture(state.data.journal)
    local effect = back.journal:get("buff:light")
    assert(kind == "refresh" and effect and effect.due == 110 or kind ~= "refresh" and not effect,
        "returning to the old shard cannot resurrect its earlier personal effect snapshot")
    back.scope:close()
end)

T.each("personal identity isolation", {
    { name = "different room", session = "world-b", master = "world-b" },
    { name = "different user", owner = "KU_OTHER", master = "world-a" },
    { name = "different character", prefab = "willow", master = "world-a" },
}, function(row)
    local ctx, state = fixture(saved_effect(), true, row.session, row.owner, row)
    assert(not ctx.journal:get("buff:light"))
    ctx.scope:close()
    if row.session then
        local back = fixture(state.data.journal)
        assert(back.journal:get("buff:light").due == 120, "another room cannot consume the offline character's effect")
        back.scope:close()
    end
end)

T.case("late master identity imports once without restarting the clock", function()
    local cave, state = fixture(saved_effect(), true, "cave-a", nil, { cave = true, master = false })
    assert(not cave.journal:get("buff:light"), "unknown room identity cannot import another shard's effects")
    state.now = 40
    cave.journal:add("weather", { state = "rain" }); cave.journal:flush()
    state:identify("world-a")
    assert(cave.journal:get("buff:light").due == 120, "identity delay cannot extend a timer after profile loading")
    state.now = 60; state:identify("world-a")
    assert(cave.journal:get("buff:light").due == 120, "repeated identity events cannot reload remaining time")
    assert(effect_count(state.data.journal, "world-a", "KU_TEST:wilson") == 1)
    cave.scope:close()
    assert(T.listener_count(cave.G.TheWorld.net) == 0)

    local same = saved_effect()
    same[1].key = "cave-a:cave"
    local current, pending = fixture(same, true, "cave-a", nil, { cave = true, master = false })
    assert(current.journal:get("buff:light").due == 120, "known current-shard data can load before shared identity arrives")
    pending.now = 50; pending:identify("world-a")
    assert(current.journal:get("buff:light").due == 120)
    current.scope:close()
end)

T.each("pending identity and profile preserve current changes", {
    { name = "death before identity", clear = true },
    { name = "food before identity" },
    { name = "death before profile", clear = true, late = true },
    { name = "food before profile", late = true },
}, function(row)
    local cave, state = fixture(saved_effect(), not row.late, "cave-a", nil, { cave = true, master = false })
    if row.clear then cave.journal:clear("buff")
    else cave.journal:put({ key = "buff:light", kind = "buff", clock = "player", due = 300 }) end
    cave.journal:flush()
    state.now = 40
    state:identify("world-a")
    if row.late then state:load() end
    local effect = cave.journal:get("buff:light")
    assert(row.clear and not effect or not row.clear and effect and effect.due == 300)
    assert(effect_count(state.data.journal, "world-a", "KU_TEST:wilson") == (row.clear and 0 or 1))
    cave.scope:close()
end)

T.case("expired delayed imports and legacy caches cannot resurrect", function()
    local cave, state = fixture(saved_effect(), true, "cave-a", nil, { cave = true, master = false })
    state.now = 121
    state:identify("world-a")
    assert(not cave.journal:get("buff:light"), "a clock exhausted while identity was pending is expired")
    cave.scope:close()
    local back = fixture(state.data.journal)
    assert(not back.journal:get("buff:light"))
    back.scope:close()

    local legacy = saved_effect()
    legacy[1].master, legacy[1].owner = nil, nil
    legacy[1].entries[#legacy[1].entries + 1] = { key = "hole", kind = "wormhole", at = 100 }
    local current, saved = fixture(legacy)
    assert(not current.journal:get("buff:light") and current.journal:get("hole"),
        "legacy effect ownership is unknown; world observations remain usable")
    current.scope:close()
    for _, book in ipairs(saved.data.journal) do
        for _, record in ipairs(book.entries) do assert(record.clock ~= "player") end
    end
end)

T.case("an empty newer character snapshot supersedes stale shard effects", function()
    local saved = saved_effect()
    table.insert(saved, 1, { key = "cave-a:cave", master = "world-a", owner = "KU_TEST:wilson", entries = {} })
    local forest = fixture(saved)
    assert(not forest.journal:get("buff:light"), "an empty character snapshot records expiry or death")
    forest.scope:close()
end)

T.case("unresolved identity at shutdown discards uncertain foreign effect caches", function()
    local saved = saved_effect()
    saved[1].entries[#saved[1].entries + 1] = { key = "hole", kind = "wormhole", at = 100 }
    local unknown, state = fixture(saved, true, "unknown-room", nil, { master = false })
    unknown.journal:put({ key = "buff:light", kind = "buff", clock = "player", due = 300 })
    unknown.scope:close()
    local same = fixture(state.data.journal, true, "unknown-room", nil, { master = false })
    assert(same.journal:get("buff:light").due == 300, "current-shard observations retain their own identity")
    same.scope:close()
    local original = fixture(state.data.journal)
    assert(not original.journal:get("buff:light") and original.journal:get("hole"),
        "unresolved room membership discards this owner's other-room effect estimate, retaining world observations")
    original.scope:close()
end)

T.case("shared effects respect rollback and bounded snapshots", function()
    local saved = saved_effect()
    saved[1].entries = {}
    for i = 1, 100 do
        local record = personal()
        record.key = "buff:" .. i
        saved[1].entries[i] = record
    end
    local cave, state = fixture(saved, true, "cave-a", nil, { cave = true, master = "world-a" })
    assert(#cave.journal:records("buff") == 16)
    cave.journal:flush()
    assert(effect_count(state.data.journal, "world-a", "KU_TEST:wilson") == 16)
    state:identify("world-a")
    assert(#cave.journal:records("buff") == 16)
    cave.G.TheWorld.state.cycles, cave.G.TheWorld.state.time = 0, 0
    assert(#cave.journal:records("buff") == 0)
    cave.G.TheWorld.state.cycles = 4
    state:identify("world-a")
    assert(#cave.journal:records("buff") == 0, "identity callbacks cannot revive discarded future observations")
    cave.scope:close()
    assert(#state.data.journal <= 2 and effect_count(state.data.journal, "world-a", "KU_TEST:wilson") == 0)
end)

local function saved_timeline()
    return {
        { key = "cave-a:cave", master = "world-a", owner = "KU_TEST:wilson", entries = {
            { key = "cave-past", kind = "wormhole", at = 27 * 480 },
            { key = "cave-future", kind = "boss", at = 29 * 480 },
            { key = "buff:future", kind = "buff", at = 29 * 480, clock = "player",
                remaining = 2000, owner = "KU_TEST:wilson" },
        } },
        { key = "world-a:forest", master = "world-a", owner = "KU_TEST:wilson", entries = {
            { key = "forest-future", kind = "weather", at = 29 * 480 },
        } },
    }
end

T.each("same-room rollback discards every shard's future observations", { "load", "active", "late identity" }, function(mode)
    local forest, state = fixture(saved_timeline(), true, "world-a", nil,
        { cycles = mode == "active" and 29 or 28, time = 0, master = mode ~= "late identity" and "world-a" })
    forest.G.TheWorld.state.cycles = 28
    assert(not forest.journal:get("forest-future"))
    forest.journal:flush()
    state.now, forest.G.TheWorld.state.cycles = 500, 29
    forest.journal:put({ key = "forest-new", kind = "weather", state = "rain" })
    forest.journal:flush()
    state.now, forest.G.TheWorld.state.cycles = 980, 30
    if mode == "late identity" then state:identify("world-a") end
    assert(forest.journal:get("forest-new"), "new observations made after rollback remain valid")
    assert(not forest.journal:get("buff:future"), "late identity cannot restore an effect from the old future timeline")
    forest.scope:close()
    local cave = fixture(state.data.journal, true, "cave-a", nil,
        { cave = true, master = "world-a", cycles = 30, time = 0 })
    assert(cave.journal:get("cave-past"), "observations before rollback remain valid")
    assert(not cave.journal:get("cave-future"), "revisiting after the clocks catch up cannot revive discarded observations")
    cave.scope:close()
end)

T.each("unrelated and unknown room calendars remain independent", { "different", "unknown" }, function(mode)
    local other, state = fixture(saved_timeline(), true, "world-b", nil,
        { master = mode ~= "unknown" and "world-b", cycles = 19, time = 0 })
    other.journal:add("weather", { state = "rain" })
    other.scope:close()
    local cave = fixture(state.data.journal, true, "cave-a", nil,
        { cave = true, master = "world-a", cycles = 30, time = 0 })
    assert(cave.journal:get("cave-future"), "another or unidentified room's younger calendar cannot erase this room's history")
    cave.scope:close()
end)

T.case("wormhole pairs require observed travel and stay stable in reverse", function()
    local ctx, state = fixture()
    local G, player = ctx.G, ctx.player
    local entities, explored = {}, true
    G.WORMHOLETYPE = T.eval("return " .. assert(T.source("constants"):match("WORMHOLETYPE%s*=%s*(%b{})")))
    G.CanEntitySeeTarget = function(_, entity) return not entity.dark end
    G.TheSim = { FindEntities = function() return entities end }
    player.CanSeePointOnMiniMap = function() return explored end
    local entrance = T.entity({ prefab = "wormhole", x = 1 })
    local exit = T.entity({ prefab = "wormhole", x = 101 })
    entities = { entrance }
    local wormholes = require("fieldkit/features/wormholes").new(ctx)
    player.x, entities, state.now = 100, { exit }, 21
    wormholes:update()
    assert(#wormholes:markers() == 0, "movement alone cannot invent a pair")
    player.x, entities, state.now = 0, { entrance }, 22
    wormholes:update()
    player:PushEvent("wormholetravel", G.WORMHOLETYPE.OCEANWHIRLPORTAL)
    player.x, entities = 100, { exit }; wormholes:update()
    assert(#wormholes:markers() == 0)
    player.x, entities, state.now = 0, { entrance }, 23; wormholes:update()
    player:PushEvent("wormholetravel", G.WORMHOLETYPE.WORM)
    player.x, entities, state.now = 100, { exit }, 24; wormholes:update()
    assert(#wormholes:markers() == 2 and wormholes:markers()[1].number == 1)
    player.player_classified = T.entity({ wormholetravelevent = { value = function() return G.WORMHOLETYPE.WORM end } })
    wormholes:update()
    player.player_classified:PushEvent("wormholetraveldirty")
    player.x, entities, state.now = 0, { entrance }, 25; wormholes:update()
    assert(#ctx.journal:records("wormhole") == 1 and wormholes:markers()[2].number == 1)
    explored = false
    assert(#wormholes:markers() == 0, "recorded endpoints still respect the current map's exploration")
    local previous = player.player_classified
    player.player_classified = nil; wormholes:update()
    assert(T.listener_count(previous) == 0)
    wormholes:clear(); assert(#ctx.journal:records("wormhole") == 0)
    ctx.scope:close()
    assert(T.listener_count(player) == 0)
end)
T.done()
