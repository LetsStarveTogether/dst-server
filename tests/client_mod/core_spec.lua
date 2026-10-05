local T = require("tests.client_mod.support")
local Scope = require("fieldkit/core/scope")
local Executor = require("fieldkit/core/executor")
local Config = require("fieldkit/core/config")
local Store = require("fieldkit/core/store")

T.case("scope_cleanup", function()
    local reports, order = {}, {}
    local scope = Scope.new({}, function(err) reports[#reports + 1] = err end)
    scope:defer(function() order[#order + 1] = 1 end)
    scope:defer(function() error("cleanup failed") end)
    scope:defer(function() order[#order + 1] = 3 end)
    scope:close()
    scope:close()
    assert(#reports == 1 and order[1] == 3 and order[2] == 1)
end)

T.case("scope_wrappers", function()
    local prototype = { sum = function(_, a, b) return a + b, nil, "tail" end }
    local object = setmetatable({}, { __index = prototype })
    local scope = Scope.new({})
    scope:wrap(object, "sum", function(previous)
        return function(self, a, b) return previous(self, a + 1, b) end
    end)
    local a, b, c = object:sum(2, 3)
    assert(a == 6 and b == nil and c == "tail")
    local ours = object.sum
    object.sum = function(...) return ours(...) end
    scope:close()
    assert(object:sum(2, 3) == 5, "a later wrapper survives with our old layer inert")
    scope = Scope.new({})
    scope:wrap(object, "extra", function() return function() return true end end)
    scope:close()
    assert(rawget(object, "extra") == nil)
end)

T.case("configuration_contract", function()
    local env = { locale = "zh" }
    setfenv(assert(loadfile("mods/fieldkit/modinfo.lua")), env)()
    local config = Config.read(env.configuration_options, function(name)
        if name == "names" then return false end
        if name == "text_scale" then return 999 end
        if name == "panel_key" or name == "cancel_key" or name == "cooking_key" then return 287 end
    end, "zh")
    assert(config.names == false and config.text_scale == 1 and config.language == "zh")
    assert(config.cancel_key == 287 and config.panel_key == 0 and config.cooking_key == 0)
    assert(config.search_radius == nil and config.fish_count == nil and config.loot_hivehat == nil,
        "task parameters and fixed item rules are not persistent configuration")
    assert(env.client_only_mod and not env.all_clients_require_mod)
end)

T.case("task_parameters", function()
    local Parameters = require("fieldkit/core/parameters")
    local params, another = Parameters.new(), Parameters.new()
    assert(params.craft_count == 5 and params.follow_keep_distance == false)
    assert(Parameters.cycle(params, "follow_keep_distance") and params.follow_keep_distance)
    assert(Parameters.cycle(params, "follow_keep_distance") and not params.follow_keep_distance)
    assert(Parameters.set(params, "craft_count", 10) and params.craft_count == 10 and another.craft_count == 5)
    assert(not Parameters.set(params, "craft_count", -1) and params.craft_count == 10)
    assert(not Parameters.set(params, "script", "return true") and params.script == nil)
    assert(not Parameters.cycle(params, "script"))
end)

local function executor()
    local state = { now = 0, ready = true, called = 0, cancelled = {}, reports = {} }
    local runner = Executor.new(function() return state.now end, function() return state.ready end,
        function() end, function(err) state.reports[#state.reports + 1] = err end)
    local function job()
        return { name = "finite", timeout = 3, step = function() state.called = state.called + 1; return "running" end,
            cancel = function(_, reason) state.cancelled[#state.cancelled + 1] = reason end }
    end
    return state, runner, job
end
T.each("executor_finish", { "replaced", "focus", "timeout", "failed", "done", "invalid_state" }, function(outcome)
    local state, runner, job = executor()
    local current = job()
    assert(runner:start(current)); runner:tick(); assert(state.called == 1)
    if outcome == "replaced" then assert(runner:start(job()))
    else
        if outcome == "focus" then state.ready = false
        elseif outcome == "timeout" then state.now = 4
        elseif outcome == "failed" then current.step = function() error("fault") end
        else current.step = function() return outcome end end
        runner:tick()
        assert(not runner:is_running() and state.called == 1)
    end
    assert(state.cancelled[1] == (outcome == "invalid_state" and "failed" or outcome))
    assert(#state.reports == ((outcome == "failed" or outcome == "invalid_state") and 1 or 0))
end)
T.each("invalid_timeout", { 0, -1, 601, math.huge, 0 / 0 }, function(timeout)
    local _, runner, job = executor()
    local invalid = job(); invalid.timeout = timeout
    assert(not runner:start(invalid))
end)
T.each("invalid_input", { true, { name = "mode", mode = "unknown" }, { name = "cursor", mode = "world", cursor = {} },
    { name = "placement", mode = "world", placement = "true" } }, function(input)
    local _, runner, job = executor()
    local invalid = job(); invalid.input = input
    assert(not runner:start(invalid))
end)
T.case("executor_cleanup_and_replacement_ownership", function()
    local _, runner, job = executor()
    assert(not runner:start(nil))
    local cleanup_start, stopping = nil, job()
    stopping.cancel = function() cleanup_start = runner:start(job()) end
    assert(runner:start(stopping)); runner:cancel()
    assert(cleanup_start == false and not runner:is_running(), "cleanup cannot leak a replacement operation")
    local replacement = job()
    assert(runner:start({ timeout = 3, step = function() runner:start(replacement); return "done" end }))
    runner:tick()
    assert(runner.current == replacement, "a completed old step cannot finish its replacement")
    runner:cancel()
end)

T.case("executor_rechecks_admission_after_cleanup", function()
    local state, runner, job = executor()
    local stopping, replacement = job(), job()
    stopping.cancel = function(self)
        assert(runner.finishing == self and runner.current == nil,
            "cleanup retains the old operation's identity without owning the running slot")
        state.ready = false
    end
    assert(runner:start(stopping))
    assert(not runner:start(replacement) and not runner:is_running(),
        "cleanup's pending native request prevents premature ownership transfer")
    assert(not replacement.started_at and not runner.finishing)
    state.ready = true
    assert(runner:start(replacement) and runner.current == replacement)
end)

T.case("store_read_write_lifecycle", function()
    -- Delayed read, concurrent edits, serialized writes, and a failed write that stays dirty.
    local read, writes, notices = nil, {}, {}
    local sim = {
        GetPersistentString = function(_, _, callback) read = callback end,
        SetPersistentString = function(_, _, encoded, _, callback)
            writes[#writes + 1] = { encoded, callback }
        end,
    }
    local json = {
        decode = function(value) assert(value == "valid"); return { version = 1, data = { old = 1 } } end,
        encode = function(value) return tostring(value.data.new or 0) end,
    }
    local store = Store.new(sim, json, function(reason) notices[#notices + 1] = reason end)
    assert(not store.ready and not store:set("new", 1) and #writes == 0)
    local loaded = 0
    store:on_ready(function()
        assert(store.ready and store:get("old") == 1)
        loaded = loaded + 1
    end)
    local scope = Scope.new({})
    scope:defer(store:on_ready(function() error("closed scope received a profile") end))
    scope:close()
    read(true, "valid")
    assert(store.ready and store:get("old") == 1 and loaded == 1)
    store:on_ready(function() loaded = loaded + 1 end)
    assert(loaded == 2, "subscribers after loading run immediately")
    assert(store:set("new", 2) and #writes == 1)
    assert(store:set("new", 3) and #writes == 1)
    writes[1][2](true)
    assert(#writes == 2 and writes[2][1] == "3")
    writes[2][2](false)
    assert(#notices == 1 and store.saved < store.revision)
    store:set("new", 4)
    assert(#writes == 3)
    writes[3][2](true)
    assert(store.saved == store.revision and not store.writing)
    store = Store.new(sim, json, function(reason) notices[#notices + 1] = reason end)
    store:on_ready(function() loaded = loaded + 1 end)
    read(true, "malformed")
    assert(store.ready and next(store.data) == nil and loaded == 3)
    store = Store.new({ GetPersistentString = function(_, _, callback) callback(false) end }, json, error)
    store:on_ready(function() loaded = loaded + 1 end)
    assert(store.ready and loaded == 4, "synchronous reads expose the same completion boundary")
end)

T.case("store_failed_write_keeps_queued_edit", function()
    local writes, notices = {}, {}
    local store = Store.new({
        GetPersistentString = function(_, _, callback) callback(false) end,
        SetPersistentString = function(_, _, encoded, _, callback)
            writes[#writes + 1] = { encoded, callback }
        end,
    }, { encode = function(value) return tostring(value.data.value) end },
        function(reason) notices[#notices + 1] = reason end)
    store:set("value", 1)
    store:set("value", 2)
    store:set("value", 3)
    assert(#writes == 1 and writes[1][1] == "1")
    writes[1][2](false)
    assert(#writes == 2 and writes[2][1] == "3" and store.writing,
        "a failed snapshot must still release the latest queued edit")
    writes[2][2](false)
    assert(#writes == 2 and not store.writing and store.saved < store.revision and #notices == 2,
        "a failed revision stays dirty without retrying itself")
    store:set("value", 4)
    assert(#writes == 3 and writes[3][1] == "4")
    writes[3][2](true)
    assert(store.saved == store.revision and not store.writing)
end)

T.case("dedicated_mod_is_inert", function()
    local registered = false
    local env = { GLOBAL = { TheNet = { IsDedicated = function() return true end } },
        require = function() registered = true; error("dedicated server loaded client code") end }
    setfenv(assert(loadfile("mods/fieldkit/modmain.lua")), env)()
    assert(not registered)
end)

T.done()
