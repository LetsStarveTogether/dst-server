local M = {}
local limits = { wormhole = 16, ride = 16, boss = 12, buff = 16, weather = 16 }
local numbers = { "at", "due", "remaining", "x", "z", "tx", "tz", "duration", "low", "high", "end_low", "end_high", "number" }
local strings = { "key", "kind", "name", "prefab", "state", "owner", "clock" }

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < 1e12
end
local function clean(record)
    if type(record) ~= "table" or not limits[record.kind] or type(record.key) ~= "string" or #record.key > 200
        or not finite(record.at) then return end
    local result = {}
    for _, key in ipairs(strings) do
        if type(record[key]) == "string" then result[key] = record[key]:sub(1, key == "key" and 200 or 128) end
    end
    for _, key in ipairs(numbers) do if finite(record[key]) then result[key] = record[key] end end
    return result
end

function M.new(ctx)
    local G, world = ctx.G, ctx.G.TheWorld
    local session = world.meta and world.meta.session_identifier or G.TheNet:GetSessionIdentifier()
    local key = type(session) == "string" and session ~= "" and session .. ":" .. (world:HasTag("cave") and "cave" or "forest") or nil
    local owner = tostring(ctx.player.userid or "") .. ":" .. ctx.player.prefab
    local self, entries, changed, worlds, cleared = {}, {}, {}, {}, {}
    local serial, dirty, previous_time = 0, false, nil
    function self:time()
        local state, day = world.state, G.TUNING.TOTAL_DAY_TIME
        if not finite(state.cycles) or not finite(state.time) or not finite(day) or day <= 0 then return end
        local now = (state.cycles + state.time) * day
        if previous_time and now < previous_time - 2 then
            for id, record in pairs(entries) do
                if record.at > now + 2 then entries[id], changed[id], dirty = nil, true, true end
            end
        end
        previous_time = now
        return now
    end
    local function trim()
        local counts, sorted = {}, {}
        for _, record in pairs(entries) do sorted[#sorted + 1] = record end
        table.sort(sorted, function(a, b) return a.at > b.at or a.at == b.at and a.key > b.key end)
        for _, record in ipairs(sorted) do
            counts[record.kind] = (counts[record.kind] or 0) + 1
            if counts[record.kind] > limits[record.kind] then entries[record.key] = nil end
        end
    end
    function self:records(kind)
        local result, now = {}, self:time()
        for _, record in pairs(entries) do
            if record.kind == kind and (not now or record.at <= now + 2) then result[#result + 1] = clean(record) end
        end
        table.sort(result, function(a, b) return a.at < b.at or a.at == b.at and a.key < b.key end)
        return result
    end
    function self:get(id)
        self:time()
        return entries[id] and clean(entries[id])
    end
    function self:put(record)
        record.at = record.at or self:time()
        local value = clean(record)
        if not value then return false end
        entries[value.key], changed[value.key], dirty = value, true, true
        trim()
        return true
    end
    function self:add(kind, record)
        record.kind, record.at = kind, record.at or self:time()
        if not record.at then return false end
        repeat
            serial = serial + 1
            record.key = kind .. ":" .. tostring(record.at) .. ":" .. serial
        until not entries[record.key]
        return self:put(record)
    end
    function self:remove(id) entries[id], changed[id], dirty = nil, true, true end
    function self:clear(kind)
        dirty = true
        for name in pairs(limits) do if not kind or kind == name then cleared[name] = true end end
        for id, record in pairs(entries) do if not kind or record.kind == kind then self:remove(id) end end
    end
    function self:flush()
        if not dirty or not key or not ctx.store or not ctx.store.ready then return end
        local saved = { key = key, entries = {} }
        for _, record in pairs(entries) do
            local copy = clean(record)
            if copy.clock == "player" then
                copy.remaining = math.max(0, (copy.due or G.GetTime()) - G.GetTime())
                copy.due, copy.owner = nil, owner
            end
            saved.entries[#saved.entries + 1] = copy
        end
        local result = { saved }
        for _, previous in ipairs(worlds) do
            if previous.key ~= key then result[#result + 1] = previous; break end
        end
        worlds, dirty = result, false
        ctx.store:set("journal", result)
    end
    if ctx.store then
        ctx.scope:defer(ctx.store:on_ready(function()
            if ctx.scope.closed then return end
            local saved = ctx.store:get("journal")
            local now = self:time()
            for _, book in ipairs(type(saved) == "table" and saved or {}) do
                if type(book) == "table" and type(book.key) == "string" and #book.key <= 200 and type(book.entries) == "table" then
                    local valid = { key = book.key, entries = {} }
                    for index, record in ipairs(book.entries) do
                        if index > 76 then break end
                        local copy = clean(record)
                        if copy then
                            valid.entries[#valid.entries + 1] = copy
                            if book.key == key and now and copy.at <= now + 2 and not changed[copy.key] and not cleared[copy.kind] then
                                if copy.clock ~= "player" then entries[copy.key] = copy
                                elseif copy.owner == owner and copy.remaining and copy.remaining > 0 and copy.remaining <= 86400 then
                                    copy.due = G.GetTime() + copy.remaining
                                    entries[copy.key] = copy
                                end
                            end
                        end
                    end
                    worlds[#worlds + 1] = valid
                    if #worlds == 2 then break end
                end
            end
            trim()
            self:flush()
        end))
    end
    ctx.scope:periodic(5, function()
        -- Personal effect clocks pause with the simulation and preserve remaining time on disconnect.
        for _, record in pairs(entries) do if record.clock == "player" then dirty = true; break end end
        self:flush()
    end)
    ctx.scope:defer(function() dirty = true; self:flush() end)
    return self
end

return M
