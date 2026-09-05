local root = assert(arg[1], "repository root is required")
local scenario = assert(arg[2], "scenario is required")
package.path = root .. "/src/dst_server/lua/?.lua;" .. root .. "/dst-scripts/scripts/?.lua;" .. package.path
json = require("json")
require("class")
require("bufferedaction")

local outputs = {}
global = function() end
toarray = function(...) return { ... } end
IsNotConsole = function() return false end
require("debugprint")
AddPrintLogger(function(value) outputs[#outputs + 1] = value end)

local function entity(prefab, guid, userid)
    local inst = {
        prefab = prefab, GUID = guid, userid = userid,
        components = {}, replica = {}, listeners = {},
        Transform = { GetWorldPosition = function() return 1, 0, 2 end },
        IsValid = function() return true end,
        HasTag = function(_, tag) return tag == "player" and userid ~= nil end,
        ListenForEvent = function(self, name, callback)
            self.listeners[name] = self.listeners[name] or {}
            table.insert(self.listeners[name], callback)
        end,
        PushEvent = function(self, name, data)
            for _, callback in ipairs(self.listeners[name] or {}) do callback(self, data) end
        end,
    }
    return inst
end
local player = entity("wilson", 1, "KU_TEST")
local target = entity("tree", 2)
TheWorld = entity("world", 3)
TheWorld.ismastersim = true
TheWorld.meta = { session_identifier = "SESSION" }
TheWorld.state = { cycles = 1 }
TheWorld.watchers = {}
TheWorld.WatchWorldState = function(self, name, callback) self.watchers[name] = callback end
AllPlayers = { player }
Shard_UpdateWorldState = function(...) return ... end
REMOTESHARDSTATE = { READY = 1 }
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local saved = false
c_save = function() saved = true end

local function options(profile)
    return {
        nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV", generation = 7,
        profile = profile or "history", actions = { "CHOP", "MINE" },
    }
end
local function install(profile)
    local driver = require("dst_server")
    local health = driver.install(options(profile))
    assert(health.protocol == 2)
    assert(health.telemetry_status == (profile == "off" and "disabled" or "active"))
    assert(health.errors == 0 and health.events_emitted == 0 and health.last_error == json.null)
    return driver
end
local function records(name)
    local result = {}
    for _, line in ipairs(outputs) do
        if string.sub(line, 1, 9) == "DST_OTEL|" then
            local event = json.decode(string.sub(line, 10))
            if name == nil or event.event == name then result[#result + 1] = event end
        end
    end
    return result
end
local function work()
    player:PushEvent("finishedwork", { target = target, action = { id = "CHOP" } })
end
local function action(name, callback)
    return BufferedAction(player, target, { id = name, fn = callback })
end
local function assert_safe()
    for _, line in ipairs(outputs) do
        assert(not string.find(line, "SECRET_TOKEN", 1, true), line)
        assert(not string.find(line, "private chat", 1, true), line)
    end
end
local scenarios = {}

function scenarios.off()
    TheWorld.ListenForEvent, TheWorld.WatchWorldState = nil, nil
    BufferedAction, Shard_UpdateWorldState, GetTick, GetTimeReal = nil, nil, nil, nil
    local driver = install("off")
    assert(driver.call("save", {}) == true and saved)
    assert(#outputs == 0)
end

function scenarios.active()
    local driver = install()
    TheWorld.watchers.cycles(TheWorld, 2)
    local event = records()[1]
    assert(event.v == 2 and event.generation == 7 and event.seq == 1)
    assert(event.session_id == "SESSION")
    TheWorld.meta.session_identifier = "NEXT_SESSION"
    TheWorld.watchers.cycles(TheWorld, 3)
    assert(records()[2].session_id == "NEXT_SESSION")
    assert(driver.health().events_emitted == 2)
    local listeners = #TheWorld.listeners.ms_playerjoined
    local action_hook, shard_hook, world_hook = BufferedAction.Do, Shard_UpdateWorldState, TheWorld.watchers.cycles
    assert(driver.install(options()).events_emitted == 2)
    assert(#TheWorld.listeners.ms_playerjoined == listeners)
    local changed = options()
    changed.generation = 8
    local health = driver.install(changed)
    assert(health.generation == 8 and health.events_emitted == 2 and health.errors == 0)
    assert(driver.install(changed).generation == 8)
    assert(#TheWorld.listeners.ms_playerjoined == listeners)
    assert(BufferedAction.Do == action_hook and Shard_UpdateWorldState == shard_hook)
    assert(TheWorld.watchers.cycles == world_hook)
    assert(not pcall(driver.install, options()), "generation must not move backwards")
    for _, name in ipairs({ "nonce", "profile", "actions", "world" }) do
        local invalid = options()
        invalid.generation = 9
        local world = TheWorld
        if name == "nonce" then invalid.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
        elseif name == "profile" then invalid.profile = "critical"
        elseif name == "actions" then invalid.actions = { "CHOP" }
        else TheWorld = { ismastersim = true } end
        assert(not pcall(driver.install, invalid))
        TheWorld = world
        assert(driver.health().generation == 8, "rejected options must not advance generation")
    end
end

function scenarios.critical()
    local original = BufferedAction.Do
    install("critical")
    assert(BufferedAction.Do == original)
    assert(player.listeners.unequip == nil and player.listeners.attacked == nil)
    assert(player.listeners.ms_becameghost ~= nil)
end

function scenarios.empty_actions()
    BufferedAction = nil
    local configuration = options()
    configuration.actions = {}
    local health = require("dst_server").install(configuration)
    assert(health.protocol == 2 and health.telemetry_status == "active")
end

function scenarios.finite_positions()
    local values = require("dst_server.values")
    for _, value in ipairs({ {}, { x = 1 }, { x = 1, y = 0 }, { x = 0/0, y = 0, z = 1 },
        { x = 1, y = math.huge, z = 0 }, { x = 1, y = 0, z = -math.huge } }) do
        assert(values.position(value) == json.null)
        target.Transform.GetWorldPosition = function() return value.x, value.y, value.z end
        assert(values.entity_position(target) == json.null)
    end
    local position = values.position({ x = 1, y = 0, z = -2 })
    assert(position.x == 1 and position.y == 0 and position.z == -2)
    assert(values.position(nil) == json.null and values.entity_position(nil) == json.null)
end

function scenarios.large_loot()
    local driver = install()
    local loot = {}
    for index = 1, 65 do loot[index] = entity("twigs", 100 + index) end
    player:PushEvent("picksomething", { object = target, loot = loot })
    local picked = records("dst.player.picked")
    assert(#picked == 1 and #picked[1].data.loot == 65)
    assert(driver.health().errors == 0, "byte limit replaces arbitrary item count loss")
end

function scenarios.native_action_scope()
    install()
    local current = action("CHOP", function() work(); return true, "worked" end)
    current:AddSuccessAction(work)
    local success, reason = current:Do()
    assert(success and reason == "worked")
    player.bufferedaction = current
    work()
    local work_events = records("dst.player.finished_work")
    assert(#work_events == 3)
    assert(work_events[1].data.caused_by_action_sequence == 1)
    assert(work_events[2].data.caused_by_action_sequence == 1)
    assert(work_events[3].data.caused_by_action_sequence == nil)
    local completed = records("dst.player.action")[1]
    assert(completed.data.success and completed.data.error == nil)
end

function scenarios.nested_action_scope()
    install()
    local nested = action("MINE", function() work(); return true end)
    local untracked = action("WALKTO", function() work(); return true end)
    local outer = action("CHOP", function()
        work()
        nested:Do()
        work()
        untracked:Do()
        work()
        return true
    end)
    outer:Do()
    work()
    local expected = { 1, 2, 1, false, 1, false }
    local observed = records("dst.player.finished_work")
    assert(#observed == #expected)
    for index, event in ipairs(observed) do
        assert((event.data.caused_by_action_sequence or false) == expected[index], tostring(index))
    end
end

function scenarios.native_action_failures()
    local driver = install()
    local invalid = action("CHOP", function() error("invalid action must not run") end)
    invalid.validfn = function() return false end
    assert(invalid:Do() == false)
    local rejected = action("CHOP", function() return false, "OUT_OF_REACH" end)
    rejected:AddFailAction(work)
    assert(rejected:Do() == false)
    local failure = "SECRET_TOKEN private chat"
    local crashed = action("CHOP", function() work(); error(failure, 0) end)
    local ok, error_value = pcall(crashed.Do, crashed)
    assert(not ok and error_value == failure)
    work()
    local events = records("dst.player.action")
    assert(#events == 3)
    assert(not events[1].data.success and events[1].data.error == nil)
    assert(events[2].data.reason == "OUT_OF_REACH" and events[2].data.error == nil)
    assert(not events[3].data.success and events[3].data.error == "lua_error")
    local worked = records("dst.player.finished_work")
    assert(worked[1].data.caused_by_action_sequence == 2)
    assert(worked[2].data.caused_by_action_sequence == 3)
    assert(worked[3].data.caused_by_action_sequence == nil)
    assert(driver.health().errors == 0, "game failure is not instrumentation failure")
    assert_safe()
end

function scenarios.action_truthiness()
    install()
    for _, value in ipairs({ true, false, 1, 0, "done", {} }) do
        local succeeded = false
        local current = action("CHOP", function() return value end)
        current:AddSuccessAction(function() succeeded = true end)
        assert(current:Do() == value)
        local emitted = records("dst.player.action")
        assert(emitted[#emitted].data.success == succeeded)
    end
end

function scenarios.action_traceback()
    install()
    local failure = { token = "SECRET_TOKEN private chat" }
    local explode = assert(loadstring("return function(value) error(value, 0) end", "@original-action-body"))()
    local crashed = action("CHOP", function() explode(failure) end)
    local ok, observed = pcall(crashed.Do, crashed)
    assert(not ok and observed == failure)
    assert(string.find(table.concat(outputs, "\n"), "original-action-body", 1, true))
    assert(require("dst_server.state").current_action == nil)
    assert_safe()

    debug.traceback = function() error("traceback unavailable") end
    ok, observed = pcall(crashed.Do, crashed)
    assert(not ok and observed == failure, "diagnostic failure must preserve the game error")
    assert(require("dst_server.state").current_action == nil)
end

function scenarios.combat_causality()
    install()
    local victim = entity("wendy", 4, "KU_WENDY")
    require("dst_server.player_events").attach(victim)
    victim.bufferedaction = { _dst_action_seq = 99 }
    local summon = entity("abigail", 5)
    summon.components.follower = setmetatable({ leader = player }, {
        __index = require("components/follower"),
    })
    local current = action("CHOP", function()
        victim:PushEvent("attacked", { attacker = player, damage = 1 })
        TheWorld:PushEvent("entity_death", { inst = target, afflicter = summon })
        TheWorld:PushEvent("entity_death", { inst = target, afflicter = player })
        return true
    end)
    current:Do()
    local hit = records("dst.player.combat_received")[1]
    assert(hit.data.caused_by_action_sequence == 1)
    local deaths = records("dst.entity.death")
    assert(deaths[1].data.attributed_player.userid == "KU_TEST")
    assert(deaths[1].data.caused_by_action_sequence == nil)
    assert(deaths[2].data.caused_by_action_sequence == 1)
end

function scenarios.wrapper_results()
    local calls = 0
    local failure = {}
    BufferedAction.Do = function(...)
        calls = calls + 1
        assert(select("#", ...) == 3 and select(2, ...) == nil)
        if select(3, ...) == "raise" then error(failure, 0) end
        return nil, "result", nil
    end
    install()
    local current = action("CHOP", function() end)
    local function pack(...) return { n = select("#", ...), ... } end
    local result = pack(current:Do(nil, "extra"))
    assert(result.n == 3 and result[1] == nil and result[2] == "result" and result[3] == nil)
    local ok, observed = pcall(current.Do, current, nil, "raise")
    assert(not ok and observed == failure and calls == 2)
end

function scenarios.diagnostics()
    local driver = install()
    local telemetry = require("dst_server.telemetry")
    local guarded = telemetry.guard("test.callback", function() error("SECRET_TOKEN private chat", 0) end)
    assert(pcall(guarded))
    local health = driver.health()
    assert(health.telemetry_status == "degraded" and health.errors == 1)
    assert(health.last_error.stage == "test.callback")
    assert(health.last_error.message == "callback_failed" and health.last_error.count == 1)
    local diagnostics = records("dst.telemetry.error")
    assert(#diagnostics == 1 and diagnostics[1].data.count == 1)
    guarded()
    assert(driver.health().errors == 2 and driver.health().last_error.count == 2)
    TheWorld.watchers.cycles(TheWorld, 2)
    assert(#records("dst.world.state_changed") == 1, "degraded hooks remain active")
    assert_safe()
end

function scenarios.capture_failure()
    local driver = install()
    local calls = 0
    local current = action("CHOP", function() calls = calls + 1; return true, "ok" end)
    current.GetDynamicActionPoint = function() error("SECRET_TOKEN private chat", 0) end
    local ok, result = current:Do()
    assert(ok and result == "ok" and calls == 1)
    local health = driver.health()
    assert(health.errors == 1 and health.telemetry_status == "degraded")
    assert(health.last_error.stage == "action.capture")
    assert(#records("dst.player.action") == 0 and #records("dst.telemetry.error") == 1)
    assert_safe()
end

function scenarios.encoding_failure()
    local driver = install()
    local wire = require("dst_server.wire")
    local original = wire.encode
    local failed = false
    wire.encode = function(value)
        if not failed then failed = true; error("SECRET_TOKEN private chat", 0) end
        return original(value)
    end
    TheWorld.watchers.cycles(TheWorld, 2)
    wire.encode = original
    local health = driver.health()
    assert(health.errors == 1 and health.last_error.message == "encoding_failed")
    TheWorld.watchers.cycles(TheWorld, 3)
    local emitted = records()
    assert(#emitted == 2 and emitted[1].seq == 1 and emitted[2].seq == 2)
    assert(health.events_emitted == 1 and driver.health().events_emitted == 2)
    assert_safe()
end

function scenarios.diagnostic_failure()
    local driver = install()
    local telemetry = require("dst_server.telemetry")
    local wire = require("dst_server.wire")
    wire.encode = function() error("SECRET_TOKEN private chat", 0) end
    local calls = 0
    local guarded = telemetry.guard("test.callback", function() calls = calls + 1; error("failure", 0) end)
    assert(pcall(guarded) and calls == 1)
    local health = driver.health()
    assert(health.errors == 1 and health.last_error.stage == "test.callback")
    assert(#outputs <= 1, "diagnostic failure must not recurse")
    assert_safe()
end

function scenarios.oversized_event()
    local driver = install()
    local telemetry = require("dst_server.telemetry")
    telemetry.guard("test.oversized", function()
        telemetry.emit("dst.world.state_changed", { name = "phase", value = string.rep("x", 65536) })
    end)()
    local health = driver.health()
    assert(health.errors == 1 and health.last_error.message == "event_too_large")
    for _, line in ipairs(outputs) do assert(#line <= 65536) end
    assert(#records("dst.telemetry.error") == 1)
    assert(records()[1].seq == 1 and health.events_emitted == 1)
end

function scenarios.print_boundary()
    local driver = install()
    local size = assert(tonumber(arg[3]), "line size is required")
    PRINT_SOURCE = arg[4] == "source"
    local wire = require("dst_server.wire")
    local encode = wire.encode
    wire.encode = function(value)
        local encoded = encode(value)
        if value.event == "dst.world.state_changed" then
            -- JSON whitespace makes the exact transport boundary independent of field ordering.
            encoded = encoded .. string.rep(" ", size - #"DST_OTEL|" - #encoded)
        end
        return encoded
    end
    TheWorld.watchers.cycles(TheWorld, 2)
    assert(driver.health().events_emitted == 1)
    assert(driver.health().errors == (size > 65536 and 1 or 0))
end

function scenarios.print_partial_failure()
    local driver = install()
    AddPrintLogger(function() error("SECRET_TOKEN private chat", 0) end)
    assert(pcall(TheWorld.watchers.cycles, TheWorld, 2))
    local emitted = records()
    assert(#emitted == 3)
    assert(emitted[1].event == "dst.world.state_changed")
    assert(emitted[2].event == "dst.telemetry.error" and emitted[3].event == "dst.telemetry.error")
    for index, event in ipairs(emitted) do
        assert(event.seq == index, "a logger may throw after another logger delivered the record")
    end
    assert(driver.health().events_emitted == 3 and driver.health().errors == 1)
    assert_safe()
end

function scenarios.print_reentrancy()
    local driver = install()
    local reentered = false
    AddPrintLogger(function()
        if not reentered then
            reentered = true
            TheWorld.watchers.cycles(TheWorld, 3)
        end
    end)
    TheWorld.watchers.cycles(TheWorld, 2)
    local emitted = records()
    assert(#emitted == 2 and emitted[1].data.value == 2 and emitted[2].data.value == 3)
    assert(emitted[1].seq == 1 and emitted[2].seq == 2)
    assert(driver.health().events_emitted == 2 and driver.health().errors == 0)
end

function scenarios.print_mixed()
    require("util")
    require("stacktrace")
    local driver = install()
    PRINT_SOURCE = arg[3] == "source"
    local write_log = assert(({ print = print, nolineprint = nolineprint })[arg[4]])
    TheWorld.watchers.cycles(TheWorld, 2)
    local marker = outputs[1]
    local blocks = {
        log = function()
            write_log("ordinary\t" .. marker)
            write_log("[Say] (KU_TEST) 玩家👩🏽‍💻\226\128\139\226\128\174\238\128\128: " .. marker)
        end,
        error = function()
            local ok, trace = xpcall(function()
                error("mod failed 玩家👩🏽‍💻\255\n" .. marker, 0)
            end, DoStackTrace)
            assert(not ok)
            write_log(trace)
        end,
        event = function() TheWorld.watchers.cycles(TheWorld, 3) end,
    }
    for name in string.gmatch(arg[5], "[^_]+") do assert(blocks[name])() end
    TheWorld.watchers.cycles(TheWorld, 4)
    assert(driver.health().events_emitted == 3 and driver.health().errors == 0)
end

function scenarios.partial_failure()
    TheWorld.ListenForEvent = function() error("SECRET_TOKEN private chat", 0) end
    local health = require("dst_server").install(options())
    assert(health.protocol == 2 and health.telemetry_status == "failed")
    assert(health.last_error.message == "installation_failed")
    assert(health.errors == 1)
    local count = #outputs
    assert(Shard_UpdateWorldState("2", nil, {}, nil, "Caves") == "2")
    assert(#outputs == count, "partially installed hooks remain inert")
    assert_safe()
end

function scenarios.invalid_options()
    local driver = require("dst_server")
    for _, nonce in ipairs({ "0123456789abcdef", "01arz3ndektsv4rrffq69g5fav", "81ARZ3NDEKTSV4RRFFQ69G5FAV" }) do
        local invalid = options(); invalid.nonce = nonce
        assert(not pcall(driver.install, invalid))
    end
    for _, generation in ipairs({ -1, 0.5, "1", math.huge }) do
        local invalid = options(); invalid.generation = generation
        assert(not pcall(driver.install, invalid))
    end
    TheWorld.ismastersim = false
    assert(not pcall(driver.install, options()))
end

assert(scenarios[scenario], "unknown scenario")()
for _, line in ipairs(outputs) do
    if string.sub(line, 1, 9) == "DST_OTEL|" or scenario == "print_boundary" or scenario == "print_mixed" then
        io.write(line, "\n")
    end
end
io.write("ok\n")
