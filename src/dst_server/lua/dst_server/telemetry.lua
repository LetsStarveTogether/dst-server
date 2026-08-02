local state = require("dst_server.state")
local telemetry = {}
local unpack_results = unpack or table.unpack

function telemetry.emit(event_name, data)
    state.sequence = state.sequence + 1
    local cycle = TheWorld.state ~= nil and TheWorld.state.cycles or nil
    local envelope = {
        v = state.protocol,
        nonce = state.nonce,
        seq = state.sequence,
        event = event_name,
        tick = GetTick(),
        monotonic_ms = math.floor(GetTimeReal()),
        cycle = cycle == nil and json.null or cycle,
        data = data,
    }
    local encoded = json.encode_compliant(envelope)
    if type(encoded) ~= "string"
        or #state.prefix + #encoded > state.max_line_bytes then
        state.errors = state.errors + 1
        return
    end
    print(state.prefix .. encoded)
    state.events_emitted = state.events_emitted + 1
end

function telemetry.guard(callback)
    return function(...)
        if not state.telemetry_active then
            return
        end
        local ok = pcall(callback, ...)
        if not ok then
            state.errors = state.errors + 1
        end
    end
end

function telemetry.pack(...)
    return { n = select("#", ...), ... }
end

function telemetry.unpack(results)
    return unpack_results(results, 1, results.n)
end

return telemetry
