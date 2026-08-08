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

local function assert_health(health, status, telemetry_error, events, errors)
    assert(health.protocol == 1)
    assert(health.telemetry_status == status)
    assert(health.telemetry_error == telemetry_error)
    assert(health.events_emitted == events)
    assert(health.errors == (errors or 0))
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

local function options(profile, actions)
    return {
        nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        profile = profile,
        actions = actions or { "CHOP" },
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
    local text = require("dst_server.values").text
    for _, character in ipairs({ "\195\169", "\228\189\160", "\240\159\144\142" }) do
        for included = 1, #character - 1 do
            local prefix = string.rep("a", 256 - included)
            assert(text(prefix .. character, 256) == prefix)
        end
        local complete = string.rep("a", 256 - #character) .. character
        assert(text(complete .. "x", 256) == complete)
    end

    local driver = require("dst_server")
    local health = driver.install(options("history"))

    assert_health(health, "active", json.null, 0)
    assert(type(watchers.cycles) == "function")

    watchers.cycles(TheWorld, 2)
    health = driver.health()
    assert_health(health, "active", json.null, 1)
    assert(outputs[1] == "DST_OTEL|{}")
end

function scenarios.empty_actions()
    BufferedAction = nil

    local driver = require("dst_server")
    local health = driver.install(options("history", {}))

    assert_health(health, "active", json.null, 0)
    assert(type(watchers.cycles) == "function")
end

function scenarios.critical()
    BufferedAction = nil

    local driver = require("dst_server")
    local health = driver.install(options("critical"))

    assert_health(health, "active", json.null, 0)
    assert(type(watchers.cycles) == "function")
end

function scenarios.telemetry_error_utf8()
    local failure = string.rep("a", 1023) .. "\228\189\160"
    TheWorld.ListenForEvent = function() error(failure, 0) end

    local driver = require("dst_server")
    local health = driver.install(options("critical"))

    assert_health(health, "failed", string.rep("a", 1023), 0)
    assert(#health.telemetry_error == 1023)
end

function scenarios.wrapper_failures()
    local action_calls = 0
    BufferedAction.Do = function(...)
        action_calls = action_calls + 1
        assert(select("#", ...) == 3)
        if select(3, ...) == "raise" then
            error("action failed", 0)
        end
        return nil, "action-result", nil
    end
    local shard_calls = 0
    Shard_UpdateWorldState = function(...)
        shard_calls = shard_calls + 1
        assert(select("#", ...) == 6)
        if select(6, ...) == "raise" then
            error("shard failed", 0)
        end
        return "shard-result", nil, 3
    end

    local driver = require("dst_server")
    assert_health(driver.install(options("history")), "active", json.null, 0)
    json.encode_compliant = function() error("telemetry encoding failed", 0) end

    local action = {
        action = { id = "CHOP" },
        doer = { HasTag = function(_, tag) return tag == "player" end },
    }
    local action_results = pack(BufferedAction.Do(action, nil, "action-extra"))
    assert(action_results.n == 3)
    assert(action_results[1] == nil and action_results[2] == "action-result")
    assert(action_results[3] == nil and action_calls == 1)
    assert(driver.health().errors == 1)

    local capture_results = pack(BufferedAction.Do(nil, nil, "action-extra"))
    assert(capture_results.n == 3)
    assert(capture_results[1] == nil and capture_results[2] == "action-result")
    assert(capture_results[3] == nil and action_calls == 2)
    assert(driver.health().errors == 2)

    local action_ok, action_error = pcall(BufferedAction.Do, action, nil, "raise")
    assert(action_ok == false and action_error == "action failed")
    assert(action_calls == 3 and driver.health().errors == 2)

    local shard_results = pack(
        Shard_UpdateWorldState(
            "1",
            REMOTESHARDSTATE.READY,
            "surface",
            {},
            "name",
            "shard-extra"
        )
    )
    assert(shard_results.n == 3)
    assert(shard_results[1] == "shard-result" and shard_results[2] == nil)
    assert(shard_results[3] == 3 and shard_calls == 1)
    assert(driver.health().errors == 3)

    local shard_capture_results = pack(
        Shard_UpdateWorldState(
            "1",
            REMOTESHARDSTATE.READY,
            {},
            {},
            "name",
            "shard-extra"
        )
    )
    assert(shard_capture_results.n == 3)
    assert(shard_capture_results[1] == "shard-result")
    assert(shard_capture_results[2] == nil and shard_capture_results[3] == 3)
    assert(shard_calls == 2 and driver.health().errors == 4)

    local shard_ok, shard_error = pcall(
        Shard_UpdateWorldState,
        "1",
        REMOTESHARDSTATE.READY,
        "surface",
        {},
        "name",
        "raise"
    )
    assert(shard_ok == false and shard_error == "shard failed")
    assert(shard_calls == 3)
    assert_health(driver.health(), "active", json.null, 0, 4)
    assert(#outputs == 0)
end

function scenarios.loot_limit()
    local listeners = {}
    local player = {
        prefab = "wilson",
        GUID = 1,
        userid = "KU_player",
        Transform = { GetWorldPosition = function() return 1, 0, 2 end },
        HasTag = function(_, tag) return tag == "player" end,
        ListenForEvent = function(_, name, callback) listeners[name] = callback end,
    }
    AllPlayers = { player }

    local encoded = 0
    json.encode_compliant = function(envelope)
        encoded = encoded + 1
        assert(envelope.event == "dst.player.picked")
        assert(#envelope.data.loot == 64)
        for index, item in ipairs(envelope.data.loot) do
            assert(item.guid == index + 1)
        end
        return "{}"
    end

    local driver = require("dst_server")
    local health = driver.install(options("history", {}))
    assert_health(health, "active", json.null, 0)
    assert(type(listeners.picksomething) == "function")

    local loot = {}
    for index = 1, 65 do
        loot[index] = { prefab = "twigs", GUID = index + 1 }
    end
    listeners.picksomething(player, { loot = { unpack(loot, 1, 64) } })
    assert_health(driver.health(), "active", json.null, 1)
    assert(encoded == 1 and #outputs == 1)

    listeners.picksomething(player, { loot = loot })
    health = driver.health()
    assert(health.events_emitted == 1)
    assert(health.errors == 1)
    assert(encoded == 1 and #outputs == 1)
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
    assert(BufferedAction._dst_original_do == nil)
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
    local driver = require("dst_server")
    for _, nonce in ipairs({
        "0123456789abcdef",
        "01arz3ndektsv4rrffq69g5fav",
        "81ARZ3NDEKTSV4RRFFQ69G5FAV",
        "01ARZ3NDEKTSV4RRFFQ69G5FAI",
    }) do
        local invalid = options("history")
        invalid.nonce = nonce
        assert(pcall(driver.install, invalid) == false)
    end

    TheWorld.ismastersim = false

    local ok = pcall(driver.install, options("history"))
    assert(ok == false)
    assert(require("dst_server.state").installed == false)
end

assert(scenarios[scenario], "unknown scenario: " .. scenario)()
io.write("ok\n")
