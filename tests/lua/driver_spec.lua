local lua_root = assert(arg[1], "Lua module root is required")
local scenario = assert(arg[2], "scenario is required")
package.path = lua_root .. "/?.lua;" .. package.path

local outputs = {}
local watchers = {}
local saved = false

print = function(value)
    outputs[#outputs + 1] = value
end

json = {
    null = {},
    decode = function(value) return value end,
    encode_compliant = function() return "{}" end,
}

TheWorld = {
    ismastersim = true,
    state = { cycles = 1 },
    ListenForEvent = function() end,
    WatchWorldState = function(_, name, callback)
        watchers[name] = callback
    end,
    HasTag = function() return false end,
}
AllPlayers = {}
BufferedAction = { Do = function(...) return ... end }
Shard_UpdateWorldState = function(...) return ... end
REMOTESHARDSTATE = { READY = 1 }
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
c_save = function() saved = true end

local function assert_health(health, status, telemetry_error, events)
    assert(health.protocol == 1)
    assert(health.telemetry_status == status)
    assert(health.telemetry_error == telemetry_error)
    assert(health.events_emitted == events)
    assert(health.errors == 0)
end

local function trap_optional_modules()
    for _, name in ipairs({
        "dst_server.actions",
        "dst_server.player_events",
        "dst_server.telemetry",
        "dst_server.world_events",
    }) do
        package.preload[name] = function()
            error("optional module loaded: " .. name, 0)
        end
    end
end

local function options(profile)
    return {
        nonce = "0123456789abcdef",
        profile = profile,
        actions = { "CHOP" },
    }
end

local function pack(...)
    return { n = select("#", ...), ... }
end

local scenarios = {}

function scenarios.off()
    trap_optional_modules()
    TheWorld.ListenForEvent = nil
    TheWorld.WatchWorldState = nil
    BufferedAction = nil
    Shard_UpdateWorldState = nil
    GetTick = nil
    GetTimeReal = nil

    local driver = require("dst_server")
    local health = driver.install(options("off"))

    assert_health(health, "disabled", json.null, 0)
    assert(driver.call("save", {}) == true)
    assert(saved)
    assert(#outputs == 0)
end

function scenarios.active()
    local driver = require("dst_server")
    local health = driver.install(options("history"))

    assert_health(health, "active", json.null, 0)
    assert(type(watchers.cycles) == "function")

    watchers.cycles(TheWorld, 2)
    health = driver.health()
    assert_health(health, "active", json.null, 1)
    assert(outputs[1] == "DST_OTEL|{}")
end

function scenarios.partial_failure()
    BufferedAction.Do = function(...)
        assert(select("#", ...) == 3)
        assert(select(2, ...) == nil)
        assert(select(3, ...) == "action-extra")
        return nil, "action-result", nil
    end
    Shard_UpdateWorldState = function(...)
        assert(select("#", ...) == 6)
        assert(select(2, ...) == nil)
        assert(select(6, ...) == "shard-extra")
        return "shard-result", nil, 3
    end

    local residual_listener
    TheWorld.ListenForEvent = function(_, _, callback)
        residual_listener = callback
        error("world install failed", 0)
    end

    local driver = require("dst_server")
    local health = driver.install(options("history"))

    assert_health(health, "failed", "world install failed", 0)
    local repeated = driver.install({})
    assert_health(repeated, "failed", health.telemetry_error, 0)
    json.encode_compliant = function() error("telemetry should be inert") end
    GetTick = json.encode_compliant
    GetTimeReal = json.encode_compliant

    local action = {
        action = { id = "CHOP" },
        doer = { HasTag = function() return true end },
    }
    local action_results = pack(BufferedAction.Do(action, nil, "action-extra"))
    assert(action_results.n == 3)
    assert(action_results[1] == nil and action_results[2] == "action-result")
    assert(action_results[3] == nil)

    local shard_results =
        pack(Shard_UpdateWorldState("1", nil, {}, {}, "name", "shard-extra"))
    assert(shard_results.n == 3)
    assert(shard_results[1] == "shard-result" and shard_results[2] == nil)
    assert(shard_results[3] == 3)
    residual_listener(TheWorld, {})

    health = driver.health()
    assert_health(health, "failed", "world install failed", 0)
    assert(require("dst_server.state").action_sequence == 0)
    assert(#outputs == 0)
end

function scenarios.core_failure()
    TheWorld.ismastersim = false

    local driver = require("dst_server")
    local ok = pcall(driver.install, options("history"))
    assert(ok == false)
    assert(require("dst_server.state").installed == false)
end

assert(scenarios[scenario], "unknown scenario: " .. scenario)()
io.write("ok\n")
