local json = require("json")
local telemetry = require("dst_server.telemetry")
local wire = require("dst_server.wire")
local bootstrap = {}
local prefix = "DST_DRIVER|"
local options
local component
local started = false
local announced = false
local recovery_requested = false

local function publish(record)
    record.nonce = options ~= nil and options.nonce or json.null
    local ok, encoded = pcall(wire.encode, record, 64 * 1024 - #prefix - 1)
    if not ok then return false end
    local sent, failure = wire.write_line(prefix .. encoded)
    telemetry.output(sent, failure)
    return sent
end

function bootstrap.report(category)
    publish({ error = category, generation = options ~= nil and options.generation or json.null })
end

local function control(record)
    record.nonce = options ~= nil and options.nonce or json.null
    record.generation = options ~= nil and options.generation or json.null
    local ok = pcall(function()
        TheSim:LuaPrintRemote("DST_CONTROL|" .. wire.encode(record, 64 * 1024 - 13) .. "\n")
    end)
    if not ok then bootstrap.report("publication_failed") end
    return ok
end

local function configure(encoded)
    local document = wire.decode(encoded)
    assert(wire.is_object(document))
    local value = document.driver
    assert(wire.is_object(value))
    recovery_requested = wire.is_object(value.control) and value.control.recovery ~= nil
    assert(type(value.nonce) == "string" and #value.nonce == 26
        and value.nonce:match("^[0-7][0-9A-HJKMNP-TV-Z]+$"))
    assert(value.profile == "off" or value.profile == "timeline" or value.profile == "play")
    for key in pairs(value) do
        assert(key == "nonce" or key == "profile" or key == "control")
    end
    local generation = TheSim:GetNumLaunches()
    assert(type(generation) == "number" and generation >= 0 and generation <= 9007199254740991
        and generation == math.floor(generation))
    if value.control ~= nil then
        assert(wire.is_object(value.control))
        for key in pairs(value.control) do assert(key == "recovery") end
        if value.control.recovery ~= nil then
            assert(wire.is_object(value.control.recovery))
            require("dst_server.recovery").validate(value.control.recovery)
        end
    end
    return {
        nonce = value.nonce,
        profile = value.profile,
        generation = generation,
        control = value.control,
    }
end

local function announce()
    if options == nil or announced then return end
    announced = true
    if not publish({ generation = options.generation }) then
        bootstrap.report("publication_failed")
    end
end

function bootstrap.install()
    if options == nil then return nil end
    announce()
    local ok = pcall(function() require("dst_server").install(options) end)
    if not ok then bootstrap.report("installation_failed") end
    return ok
end

function bootstrap.attach(runtime)
    component = runtime
    component:Initialize()
end

function bootstrap.ready()
    local ok, sent = pcall(function()
        return publish({ health = require("dst_server").health() })
    end)
    if not ok or not sent then bootstrap.report("publication_failed") end
end

function bootstrap.start()
    if started then return end
    started = true
    -- Register before reading asynchronous configuration: native index loading may finish first.
    local gate_ok, resolve_recovery = pcall(function() return require("dst_server.recovery").gate(control) end)
    if not gate_ok then
        resolve_recovery = nil
        telemetry.report("recovery_gate.install", "installation_failed")
    end
    local original = SpawnPrefabFromSim
    if type(original) ~= "function" then
        bootstrap.report("installation_failed")
        return
    end
    local watch_spawn
    local function after_spawn(...)
        local guid = ...
        local inst = watch_spawn and Ents[guid]
        if inst ~= nil and inst == TheWorld and inst.ismastersim then
            if SpawnPrefabFromSim == watch_spawn then SpawnPrefabFromSim = original end
            watch_spawn = nil
            announce()
            if not pcall(inst.AddComponent, inst, "dst_server_runtime") then
                bootstrap.report("installation_failed")
            end
        end
        return ...
    end
    watch_spawn = function(...)
        -- A mod installed after us may still call this wrapper after the world has spawned.
        if watch_spawn == nil then return original(...) end
        return after_spawn(original(...))
    end
    SpawnPrefabFromSim = watch_spawn
    local received = false
    local function abandon_configuration()
        if resolve_recovery then
            resolve_recovery(recovery_requested and { mode = "invalid" } or nil)
        elseif recovery_requested then
            control({ event = "recovery_failed", error = "invalid_configuration" })
            Shutdown()
        end
    end
    local function configured(success, encoded)
        if received then return end
        received = true
        local ok, value = pcall(function()
            assert(success)
            return configure(encoded)
        end)
        if not ok then
            bootstrap.report("configuration_failed")
            abandon_configuration()
            return
        end
        options = value
        local recovery = options.control and options.control.recovery
        local observed = telemetry.install("load_observer", function()
            assert(require("dst_server.load_observer").install(control))
        end)
        if recovery and (resolve_recovery == nil or not observed) then
            bootstrap.report("installation_failed")
            control({ event = "recovery_failed", error = "installation_failed" })
            Shutdown()
            return
        end
        if not pcall(function() require("dst_server.rpc").install(options) end) then
            bootstrap.report("installation_failed")
            abandon_configuration()
            options = nil
            return
        end
        if resolve_recovery ~= nil then
            resolve_recovery(recovery)
        end
        if component ~= nil and not pcall(component.Initialize, component) then
            bootstrap.report("installation_failed")
        end
    end
    if not pcall(TheSim.GetPersistentString, TheSim, "../dst_server_driver.json", configured) then
        configured(false)
    end
end

return bootstrap
