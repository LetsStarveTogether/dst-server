local observer = {}

local function pack(...) return { n = select("#", ...), ... } end

local function text(value)
    return type(value) == "string" and value ~= "" and not value:find("[%z\r\n]")
end

-- Observe the native decoder's existing callback; do not parse or replace save data.
function observer.install(emit)
    local original = ShardIndex ~= nil and ShardIndex.GetSaveDataFile or nil
    if type(original) ~= "function" or type(debug) ~= "table"
        or type(debug.getupvalue) ~= "function" or type(debug.setupvalue) ~= "function"
        or type(debug.getinfo) ~= "function" then return false end
    local position, native
    for index = 1, 100 do
        local name, value = debug.getupvalue(original, index)
        if name == nil then break end
        if name == "OnLoadSaveDataFile" and type(value) == "function" then
            local info = debug.getinfo(value, "S")
            if info ~= nil and info.what == "Lua" and info.source:match("shardindex%.lua$") then
                position, native = index, value
            end
            break
        end
    end
    if native == nil then return false end
    local sandbox, native_assert, table_size = RunInSandbox, assert, GetTableSize
    local pending = {}
    local function publish(record, event, fields)
        local output = {
            event = event, session_id = record.session_id, world_file = record.world_file,
        }
        for key, value in pairs(fields or {}) do output[key] = value end
        pcall(emit, output)
    end
    local function observed(file, callback, load_success, source, ...)
        local record = pending[callback]
        if record == nil then return native(file, callback, load_success, source, ...) end
        pending[callback] = nil
        local decoder_unchanged = RunInSandbox == sandbox and assert == native_assert and GetTableSize == table_size
        local values = pack(pcall(native, file, callback, load_success, source, ...))
        record.finished = true
        if not values[1] then
            local phase, reason = "decode", "native_error"
            local failure = type(values[2]) == "string" and values[2] or ""
            local function ends(message)
                local ending = message .. " [" .. file .. "]"
                return failure:sub(-#ending) == ending
            end
            if record.callback_entered then phase = "world"
            elseif not load_success then phase, reason = "read", "read_failed"
            elseif source == nil then phase, reason = "read", "encoded_nil"
            elseif type(source) == "string" and #source == 0 then phase, reason = "read", "encoded_empty"
            elseif ends("Corrupt Save file") then reason = "parse_failed"
            elseif ends("ShardIndex:GetSaveData: Savedata is NIL on load") then reason = "decoded_nil"
            elseif ends("ShardIndex:GetSaveData: Savedata is empty on load") then reason = "decoded_empty" end
            publish(record, "world_load_failed", {
                phase = phase, reason = reason, read_succeeded = load_success == true,
                callback_entered = record.callback_entered,
                source_bytes = type(source) == "string" and #source or 0,
                decoder_unchanged = decoder_unchanged,
            })
            error(values[2], 0)
        end
        return unpack(values, 2, values.n)
    end
    if debug.setupvalue(original, position, observed) ~= "OnLoadSaveDataFile" then return false end
    ShardIndex.GetSaveDataFile = function(self, file, callback, ...)
        local ok, session_id = pcall(function() return self:GetSession() end)
        if not ok or not text(session_id) or not text(file) or type(callback) ~= "function" then
            return original(self, file, callback, ...)
        end
        local record = { session_id = session_id, world_file = file, callback_entered = false }
        local function delivered(...)
            record.callback_entered = true
            publish(record, "world_load_decoded")
            return callback(...)
        end
        pending[delivered] = record
        publish(record, "world_load_started")
        local values = pack(pcall(original, self, file, delivered, ...))
        if not values[1] then
            pending[delivered] = nil
            if not record.finished then
                publish(record, "world_load_failed", {
                    phase = "read", reason = "native_error", read_succeeded = false,
                    callback_entered = false, source_bytes = 0,
                    decoder_unchanged = false,
                })
            end
            error(values[2], 0)
        end
        return unpack(values, 2, values.n)
    end
    return true
end

return observer
