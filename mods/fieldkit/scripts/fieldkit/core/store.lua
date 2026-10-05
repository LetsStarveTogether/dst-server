-- One profile file, decoded as data. Writes are serialized; a late read cannot erase edits.
local M = {}
local MAX_BYTES = 131072

function M.new(sim, json, notify)
    local self = { ready = false, data = {}, writing = false, revision = 0, saved = 0 }
    local waiting = {}
    local filename = "fieldkit_profile_v1"
    local function flush()
        if self.writing or self.revision == self.saved then return end
        local ok, encoded = pcall(json.encode, { version = 1, data = self.data })
        if not ok or #encoded > MAX_BYTES then notify("save_failed"); return end
        local revision = self.revision
        self.writing = true
        sim:SetPersistentString(filename, encoded, false, function(success)
            self.writing = false
            if success then
                self.saved = revision
            else
                notify("save_failed")
            end
            -- Each snapshot gets one attempt; edits queued behind it still need to be written.
            if self.revision > revision then flush() end
        end)
    end

    function self:get(key)
        return self.data[key]
    end

    function self:on_ready(fn)
        if self.ready then fn() else waiting[fn] = true end
        return function() waiting[fn] = nil end
    end

    function self:set(key, value)
        if not self.ready then return false end
        self.data[key] = value
        self.revision = self.revision + 1
        flush()
        return true
    end

    sim:GetPersistentString(filename, function(success, data)
        if success and type(data) == "string" and #data <= MAX_BYTES then
            local ok, decoded = pcall(json.decode, data)
            if ok and type(decoded) == "table" and decoded.version == 1 and type(decoded.data) == "table" then
                self.data = decoded.data
            else
                notify("read_failed")
            end
        end
        self.ready = true
        for fn in pairs(waiting) do
            waiting[fn] = nil
            fn()
        end
    end)
    return self
end

return M
