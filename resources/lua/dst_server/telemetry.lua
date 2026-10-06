local state = require("dst_server.state")
local wire = require("dst_server.wire")
local telemetry = {}
local messages = {
    callback_failed = true,
    encoding_failed = true,
    event_too_large = true,
    installation_failed = true,
    queue_full = true,
    schedule_failed = true,
    write_failed = true,
}

local function publish(encoded)
    -- An attempted send consumes its sequence even when delivery is uncertain.
    state.sequence = state.sequence + 1
    local ok, failure = wire.write_line(state.prefix .. encoded)
    telemetry.output(ok, failure)
    return ok
end

local function write(event_name, data, occurrence)
    local tick, milliseconds, cycle, session
    if occurrence then
        tick, milliseconds = occurrence.tick, occurrence.monotonic_ms
        cycle, session = occurrence.cycle, occurrence.session_id
    else
        tick, milliseconds = GetTick(), math.floor(GetTimeReal())
        local world = TheWorld
        cycle = world and world.state and world.state.cycles
        session = world and world.meta and world.meta.session_identifier
    end
    local encoded = wire.encode_event({
        nonce = state.nonce,
        generation = state.generation,
        session_id = session or json.null,
        seq = state.sequence + 1,
        event = event_name,
        tick = tick,
        monotonic_ms = milliseconds,
        cycle = cycle == nil and json.null or cycle,
        data = data,
    }, state.max_line_bytes - #state.prefix - 1)
    return publish(encoded)
end

function telemetry.report(stage, message, count)
    if type(stage) ~= "string" or #stage > 128 or not stage:match("^[a-z][a-z0-9_.:]*$") then
        stage = "telemetry"
    end
    if not messages[message] then message = "callback_failed" end
    state.errors = state.errors + (count or 1)
    state.health_revision = state.health_revision + 1
    state.faults[stage] = message
    state.last_error = { stage = stage, message = message, count = state.errors, revision = state.health_revision }
end

function telemetry.output(ok, failure)
    if ok then telemetry.recover("output") else telemetry.report("output", failure) end
end

function telemetry.recover(stage)
    if state.faults[stage] ~= nil then
        state.faults[stage] = nil
        state.health_revision = state.health_revision + 1
    end
end

function telemetry.install(name, callback)
    local ok, supported = pcall(callback)
    state.capabilities[name] = not ok and "failed" or supported == false and "unsupported" or "active"
    state.health_revision = state.health_revision + 1
    if not ok then telemetry.report(name .. ".install", "installation_failed") end
    return ok and supported ~= false
end

function telemetry.emit(event_name, data, occurrence)
    if not state.telemetry_active then return end
    local ok, failure = pcall(write, event_name, data, occurrence)
    if not ok then
        telemetry.report("emit." .. event_name, failure == "response_too_large" and "event_too_large" or "encoding_failed")
    elseif next(state.faults) ~= nil then
        telemetry.recover("emit." .. event_name)
    end
end

local per_tick, capacity = 4, 128
local queue, head, size = {}, 1, 0
local budget_tick, remaining
local drain

local function refresh_budget()
    local ok, tick = pcall(GetStaticTick)
    if not ok or type(tick) ~= "number" then return false end
    if tick ~= budget_tick then budget_tick, remaining = tick, per_tick end
    return remaining ~= nil
end

local function drop_play(stage, reason, count)
    state.play_queue_dropped = state.play_queue_dropped + count
    telemetry.report(stage, reason, count)
end

local function discard_queue()
    drop_play("play.schedule", "schedule_failed", size)
    telemetry.recover("play.queue")
    -- A failed scheduler can be tried again on the next static tick.
    queue, head, size, remaining = {}, 1, 0, nil
end

local function schedule()
    return TheWorld:DoStaticTaskInTime(0, drain)
end

local function schedule_drain()
    local ok, task = pcall(schedule)
    if ok and task then
        telemetry.recover("play.schedule")
    else
        discard_queue()
    end
end

drain = function()
    if not refresh_budget() then return discard_queue() end
    while size > 0 and remaining > 0 do
        local row = queue[head]
        telemetry.emit(row.event, row.data, row)
        queue[head] = nil
        head, size, remaining = head % capacity + 1, size - 1, remaining - 1
    end
    if size < capacity then telemetry.recover("play.queue") end
    if size > 0 then schedule_drain() end
end

-- Callers snapshot game objects into plain fields before submitting a gameplay result.
function telemetry.play(event_name, data)
    if not state.telemetry_active or state.requested_profile ~= "play" then return end
    if not refresh_budget() then
        drop_play("play.schedule", "schedule_failed", 1)
    elseif size == 0 and remaining > 0 then
        remaining = remaining - 1
        telemetry.emit(event_name, data)
    elseif size == capacity then
        drop_play("play.queue", "queue_full", 1)
    else
        local world = TheWorld
        local row = {
            event = event_name, data = data, tick = GetTick(), monotonic_ms = math.floor(GetTimeReal()),
            cycle = world.state and world.state.cycles or json.null,
            session_id = world.meta and world.meta.session_identifier or json.null,
        }
        queue[(head - 1 + size) % capacity + 1] = row
        size = size + 1
        if size == 1 then schedule_drain() end
    end
end

local function guard_result(stage, ok, ...)
    if ok then
        telemetry.recover(stage)
        return ...
    end
    telemetry.report(stage, "callback_failed")
end

function telemetry.guard(stage, callback, always)
    return function(...)
        if not state.telemetry_active and not always then return end
        return guard_result(stage, pcall(callback, ...))
    end
end

function telemetry.pack(...)
    return { n = select("#", ...), ... }
end

return telemetry
