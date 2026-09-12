local json = require("json")
local wire = require("dst_server.wire")
local bootstrap = {}
local prefix = "DST_DRIVER|"
local options
local component
local started = false
local announced = false

local function publish(record)
    record.nonce = options ~= nil and options.nonce or json.null
    return pcall(function()
        nolineprint(prefix .. wire.encode(record, 64 * 1024 - #prefix))
    end)
end

function bootstrap.report(category)
    publish({ error = category, generation = options ~= nil and options.generation or json.null })
end

local function configure(encoded)
    assert(type(encoded) == "string" and #encoded <= 64 * 1024)
    local value = wire.decode(encoded)
    assert(wire.is_object(value))
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
    return {
        nonce = value.nonce,
        profile = value.profile,
        actions = value.actions,
        generation = generation,
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
            return
        end
        options = value
        if not pcall(function() require("dst_server.rpc").install(options) end) then
            bootstrap.report("installation_failed")
            options = nil
            return
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
