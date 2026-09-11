local state = require("dst_server.state")
local wire = require("dst_server.wire")
local telemetry = {}
local unpack_results = unpack or table.unpack
local messages = {
    callback_failed = true,
    encoding_failed = true,
    event_too_large = true,
    installation_failed = true,
}

local function publish(encoded)
    -- A print logger can reenter or fail after an earlier logger delivered the line.
    state.sequence = state.sequence + 1
    nolineprint(state.prefix .. encoded)
end

local function write(event_name, data)
    local world = TheWorld
    local cycle = world ~= nil and world.state ~= nil and world.state.cycles or nil
    local session = world ~= nil and world.meta ~= nil and world.meta.session_identifier or nil
    local encoded = wire.encode({
        v = state.protocol,
        nonce = state.nonce,
        generation = state.generation,
        session_id = session or json.null,
        seq = state.sequence + 1,
        event = event_name,
        tick = GetTick(),
        monotonic_ms = math.floor(GetTimeReal()),
        cycle = cycle == nil and json.null or cycle,
        data = data,
    }, state.max_line_bytes - #state.prefix)
    publish(encoded)
end

function telemetry.report(stage, message)
    if type(stage) ~= "string" or #stage > 128 or not stage:match("^[a-z][a-z0-9_.:]*$") then
        stage = "telemetry"
    end
    if not messages[message] then message = "callback_failed" end
    state.errors = state.errors + 1
    local diagnostic = { stage = stage, message = message, count = state.errors }
    state.last_error = diagnostic
    if pcall(write, "dst.telemetry.error", diagnostic) then return end

    -- A diagnostic must survive failure of the codec or game clocks, without recursion.
    local fallback = string.format(
        '{"v":2,"nonce":"%s","generation":%.0f,"session_id":null,"seq":%.0f,'
        .. '"event":"dst.telemetry.error","tick":0,"monotonic_ms":0,"cycle":null,'
        .. '"data":{"stage":"%s","message":"%s","count":%.0f}}',
        state.nonce, state.generation, state.sequence + 1, stage, message, state.errors
    )
    pcall(publish, fallback)
end

function telemetry.emit(event_name, data)
    local ok, failure = pcall(write, event_name, data)
    if not ok then
        telemetry.report(event_name, failure == "response_too_large" and "event_too_large" or "encoding_failed")
    end
end

function telemetry.guard(stage, callback, always)
    return function(...)
        if not state.telemetry_active and not always then return end
        if not pcall(callback, ...) then
            telemetry.report(stage, "callback_failed")
        end
    end
end

function telemetry.pack(...)
    return { n = select("#", ...), ... }
end

function telemetry.unpack(results, first)
    return unpack_results(results, first or 1, results.n)
end

return telemetry
