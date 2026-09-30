local json = require("json")
local recovery = {}
local known_errors = {}

local function fail(reason)
    known_errors[reason] = true
    error(reason, 0)
end

local function text(value, limit)
    return type(value) == "string" and #value > 0 and #value <= limit
        and not value:find("[%z\r\n]")
end

local function integer(value)
    return type(value) == "number" and value >= 0 and value <= 9007199254740991
        and value == math.floor(value)
end

function recovery.validate(request)
    assert(type(request) == "table" and (request.mode == "catalog" or request.mode == "apply"))
    assert(request.session_id == nil or text(request.session_id, 128))
    for key in pairs(request) do
        assert(key == "mode" or key == "session_id"
            or (request.mode == "apply" and (key == "snapshot_id" or key == "world_file")))
    end
    if request.mode == "apply" then
        assert(text(request.session_id, 128) and integer(request.snapshot_id) and request.snapshot_id > 0
            and text(request.world_file, 4096))
    end
    return request
end

-- This entrypoint also allows an isolated native probe to supply a truncation function.
-- Production uses an absolute target; its dedicated-server semantics require the P0 probe.
function recovery.run(index, request, emit, truncate)
    local session_id
    local ok, record = pcall(function()
        recovery.validate(request)
        if TheWorld ~= nil then fail("world_already_loaded") end
        session_id = index:GetSession()
        if not text(session_id, 128) then fail("missing_session") end
        if request.session_id ~= nil and request.session_id ~= session_id then fail("session_changed") end
        local latest = TheNet:GetWorldSessionFile(session_id)
        local fetch, snapshots, has_more, target, current = 100
        repeat
            snapshots, has_more = TheNet:ListSnapshots(session_id, TheNet:IsOnlineMode(), fetch)
            if type(snapshots) ~= "table" then fail("invalid_catalog") end
            local values = {}
            for _, snapshot in ipairs(snapshots) do
                if not integer(snapshot.snapshot_id)
                    or (snapshot.world_file ~= nil and not text(snapshot.world_file, 4096)) then
                    fail("invalid_catalog")
                end
                values[#values + 1] = { snapshot_id = snapshot.snapshot_id, world_file = snapshot.world_file or json.null }
                if snapshot.world_file ~= nil and snapshot.world_file == latest then current = snapshot end
                if snapshot.snapshot_id == request.snapshot_id then target = snapshot end
            end
            if request.mode == "catalog" then
                return {
                    event = "recovery_catalog", session_id = session_id,
                    latest_world_file = latest or json.null, snapshots = values, has_more = has_more == true,
                }
            end
            -- ponytail: native catalogs reread a prefix; reject targets beyond 6400 entries.
            if target ~= nil or not has_more or fetch >= 6400 then break end
            fetch = fetch * 2
        until false
        if target == nil then fail("target_not_found") end
        if target.world_file ~= request.world_file then fail("target_path_mismatch") end
        if current == nil then fail("current_not_found") end
        if current.snapshot_id < request.snapshot_id then fail("target_is_newer") end
        local changed = latest ~= request.world_file
        if changed then
            if current.snapshot_id == request.snapshot_id then fail("target_path_mismatch") end
            if truncate ~= nil then truncate(session_id, request.snapshot_id)
            else TheNet:TruncateSnapshots(session_id, request.snapshot_id) end
        end
        if index:GetSession() ~= session_id then fail("session_changed") end
        if TheNet:GetWorldSessionFile(session_id) ~= request.world_file then fail("target_not_selected") end
        return {
            event = "recovery_applied", session_id = session_id, snapshot_id = request.snapshot_id,
            world_file = request.world_file, changed = changed,
        }
    end)
    if not ok then
        record = {
            event = "recovery_failed", session_id = session_id or json.null,
            error = known_errors[record] and record or "native_recovery_failed",
        }
    end
    pcall(emit, record)
    Shutdown() -- Never continue into world loading, including after an error or lost reply.
    return ok
end

function recovery.gate(emit)
    local original = ShardIndex ~= nil and ShardIndex.Load or nil
    if type(original) ~= "function" then return nil end
    local resolved, request, pending, finished = false, nil, {}, false
    local start_server = StartDedicatedServer
    if type(start_server) == "function" then
        -- Authentication/offline startup can schedule a new Lua instance independently
        -- of OnFilesLoaded. Suppress that path before it can race recovery's Shutdown.
        StartDedicatedServer = function(...)
            local arguments, count = { ... }, select("#", ...)
            local function resume()
                if request == nil then return start_server(unpack(arguments, 1, count)) end
            end
            if resolved then return resume() end
            pending[#pending + 1] = resume
        end
    end
    ShardIndex.Load = function(self, callback, ...)
        return original(self, function(...)
            local arguments, count = { ... }, select("#", ...)
            local function resume()
                if request ~= nil then
                    if finished then return end
                    finished = true
                    return recovery.run(self, request, emit)
                end
                if callback ~= nil then return callback(unpack(arguments, 1, count)) end
            end
            if resolved then return resume() end
            pending[#pending + 1] = resume
        end, ...)
    end
    return function(value)
        if resolved then return end
        resolved, request = true, value
        local waiting = pending
        pending = nil
        for _, resume in ipairs(waiting) do resume() end
    end
end

return recovery
