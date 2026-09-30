local json = require("json")
local wire = require("dst_server.wire")
local bootstrap = {}
local prefix = "DST_DRIVER|"
local options
local component
local started = false
local announced = false
local control_requested = false

local function publish(record)
    record.nonce = options ~= nil and options.nonce or json.null
    return pcall(function()
        nolineprint(prefix .. wire.encode(record, 64 * 1024 - #prefix))
    end)
end

function bootstrap.report(category)
    publish({ error = category, generation = options ~= nil and options.generation or json.null })
end

local function control(record)
    record.v = 1
    record.nonce = options ~= nil and options.nonce or json.null
    record.generation = options ~= nil and options.generation or json.null
    local ok = pcall(function()
        TheSim:LuaPrintRemote("DST_CONTROL|" .. wire.encode(record, 64 * 1024 - 13) .. "\n")
    end)
    if not ok then bootstrap.report("publication_failed") end
    return ok
end

local function configure(encoded)
    assert(type(encoded) == "string" and #encoded <= 64 * 1024)
    local value = wire.decode(encoded)
    assert(wire.is_object(value))
    control_requested = value.control ~= nil
    assert(type(value.nonce) == "string" and #value.nonce == 26
        and value.nonce:match("^[0-7][0-9A-HJKMNP-TV-Z]+$"))
    assert(value.profile == "off" or value.profile == "critical" or value.profile == "history")
    assert(type(value.actions) == "table" and not wire.is_object(value.actions))
    for index, action in pairs(value.actions) do
        assert(type(index) == "number" and index >= 1 and index <= #value.actions
            and index == math.floor(index) and type(action) == "string" and action ~= "")
    end
    local generation = TheSim:GetNumLaunches()
    assert(type(generation) == "number" and generation >= 0 and generation <= 9007199254740991
        and generation == math.floor(generation))
    if control_requested then
        assert(wire.is_object(value.control))
        for key in pairs(value.control) do assert(key == "observe_saves" or key == "recovery") end
        assert(value.control.observe_saves == nil or type(value.control.observe_saves) == "boolean")
        if value.control.recovery ~= nil then
            assert(wire.is_object(value.control.recovery))
            require("dst_server.recovery").validate(value.control.recovery)
        end
    end
    return {
        nonce = value.nonce,
        profile = value.profile,
        actions = value.actions,
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
    local ok = pcall(function()
        local health = require("dst_server").health()
        if not publish({ health = health }) then error("publication_failed", 0) end
    end)
    if not ok then bootstrap.report("publication_failed") end
end

function bootstrap.start()
    if started then return end
    started = true
    -- Register before reading asynchronous configuration: native index loading may finish first.
    local resolve_recovery = require("dst_server.recovery").gate(control)
    local original = SpawnPrefabFromSim
    if type(original) ~= "function" then
        bootstrap.report("installation_failed")
        return
    end
    local world_started = false
    SpawnPrefabFromSim = function(...)
        local guid = original(...)
        local inst = Ents[guid]
        if inst ~= nil and inst == TheWorld and inst.ismastersim and not world_started then
            world_started = true
            announce()
            if not pcall(inst.AddComponent, inst, "dst_server_runtime") then
                bootstrap.report("installation_failed")
            end
        end
        return guid
    end
    local received = false
    local function configured(success, encoded)
        if received then return end
        received = true
        local ok, value = pcall(function()
            assert(success)
            return configure(encoded)
        end)
        if not ok then
            bootstrap.report("configuration_failed")
            if resolve_recovery ~= nil then
                resolve_recovery(control_requested and { mode = "invalid" } or nil)
            end
            return
        end
        options = value
        if options.control ~= nil then
            local installed = pcall(function()
                assert(resolve_recovery ~= nil, "native index loading hook is unavailable")
                if options.control.observe_saves then
                    require("dst_server.save_observer").install(control)
                    require("dst_server.load_observer").install(control)
                end
            end)
            if not installed then
                bootstrap.report("installation_failed")
                control({ event = "recovery_failed", error = "installation_failed" })
                Shutdown()
                return
            end
        end
        if not pcall(function() require("dst_server.rpc").install(options) end) then
            bootstrap.report("installation_failed")
            if resolve_recovery ~= nil then
                resolve_recovery(options.control ~= nil and { mode = "invalid" } or nil)
            end
            options = nil
            return
        end
        if resolve_recovery ~= nil then
            resolve_recovery(options.control ~= nil and options.control.recovery or nil)
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
