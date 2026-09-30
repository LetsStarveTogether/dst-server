local json = require("json")
local observer = {}
local emit, active_call
local active, sequence = {}, 0
local wrappers = setmetatable({}, { __mode = "k" })
local entries = setmetatable({}, { __mode = "k" })

function observer.refresh()
    if emit == nil then return false end
    -- Native shutdown enters here without going through the SDK dispatcher.
    local index = ShardGameIndex
    if index ~= nil and type(index.SaveCurrent) == "function" and not entries[index.SaveCurrent] then
        local save_current = index.SaveCurrent
        local function current(self, ...)
            observer.refresh()
            return save_current(self, ...)
        end
        entries[current] = true
        index.SaveCurrent = current
    end
    local original = assert(SaveGame, "SaveGame is unavailable")
    assert(type(original) == "function")
    if wrappers[original] then return false end

    local function wrapped(isshutdown, callback, ...)
        -- A Mod can delegate to the previously wrapped SaveGame. Observe the
        -- outer call once while retaining that Mod's arguments and callback.
        if active_call ~= nil and not active_call.completed then
            return original(isshutdown, callback, ...)
        end
        if not TheNet:GetIsServer() then return original(isshutdown, callback, ...) end
        sequence = sequence + 1
        local observed, world, session_id, snapshot_id = pcall(function()
            local current = TheWorld
            return current, current ~= nil and current.meta ~= nil and current.meta.session_identifier or nil,
                TheNet:GetCurrentSnapshot()
        end)
        if not observed then world, session_id, snapshot_id = nil, nil, nil end
        local record = {
            save_id = sequence,
            session_id = session_id,
            snapshot_id = snapshot_id,
            shutdown = isshutdown == true,
        }
        local function report(event, reason)
            pcall(emit, {
                event = event, save_id = record.save_id, session_id = record.session_id or json.null,
                snapshot_id = record.snapshot_id or json.null, shutdown = record.shutdown, error = reason,
            })
        end
        for _, pending in pairs(active) do
            if pending.session_id == record.session_id and pending.snapshot_id == record.snapshot_id then
                pending.overlapping, record.overlapping = true, true
            end
        end
        active[record.save_id] = record
        report("save_started")
        local function oncomplete(...)
            if not record.completed then
                record.completed = true
                active[record.save_id] = nil
                local ok, reason = pcall(function()
                    if not observed then return "observation_failed" end
                    if record.overlapping then return "overlapping_snapshot" end
                    if TheWorld ~= world or world == nil or world.meta == nil
                        or world.meta.session_identifier ~= record.session_id then
                        return "session_changed"
                    end
                    if type(record.session_id) ~= "string" or record.session_id == ""
                        or type(record.snapshot_id) ~= "number" or record.snapshot_id < 0
                        or record.snapshot_id > 9007199254740991
                        or record.snapshot_id ~= math.floor(record.snapshot_id)
                        or TheNet:GetCurrentSnapshot() <= record.snapshot_id then
                        return "snapshot_unconfirmed"
                    end
                end)
                if not ok then reason = "observation_failed" end
                report(reason == nil and "save_complete" or "save_unconfirmed", reason)
            end
            -- Preserve all original callback arguments, results and errors, including Shutdown.
            if callback ~= nil then return callback(...) end
        end
        local previous_call = active_call
        active_call = record
        local function finish(ok, ...)
            active_call = previous_call
            if not ok then
                if not record.completed then
                    record.completed = true
                    active[record.save_id] = nil
                    report("save_failed", "native_save_failed")
                end
                error((...), 0)
            end
            return ...
        end
        return finish(pcall(original, isshutdown, oncomplete, ...))
    end
    wrappers[wrapped] = true
    SaveGame = wrapped
    return true
end

function observer.install(publish)
    assert(type(publish) == "function")
    emit = publish
    return observer.refresh()
end

return observer
