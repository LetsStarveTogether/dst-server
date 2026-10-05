local T = require("tests.client_mod.support")
local Scope = require("fieldkit/core/scope")
local Journal = require("fieldkit/game/journal")

local function fixture(saved, ready, session, owner)
    local state = { now = 20, writes = 0, callbacks = {}, data = { journal = saved } }
    local player = T.entity({ prefab = "wilson", userid = owner or "KU_TEST" })
    state.tasks = T.tasks(player)
    local world = T.entity({ state = { cycles = 3, time = .5 }, meta = { session_identifier = session or "world-a" } })
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
    local old = { { key = "world-a:forest", entries = {
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
