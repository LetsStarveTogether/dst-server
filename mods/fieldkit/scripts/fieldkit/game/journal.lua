local M = {}
local limits = { wormhole = 16, ride = 16, boss = 12, buff = 16, weather = 16 }
local numbers = { "at", "due", "remaining", "x", "z", "tx", "tz", "duration", "low", "high", "end_low", "end_high", "number" }
local strings = { "key", "kind", "name", "prefab", "state", "owner", "clock" }

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < 1e12
end
local function identifier(value)
    return type(value) == "string" and value ~= "" and #value <= 200 and value or nil
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
    local key = identifier(session) and session .. ":" .. (world:HasTag("cave") and "cave" or "forest") or nil
    local owner = tostring(ctx.player.userid or "") .. ":" .. ctx.player.prefab
    local function master_session()
        local state = world.net and world.net.components.shardstate
        return state and identifier(state:GetMasterSessionId()) or nil
    end
    local master, loaded_at, source = master_session(), nil, nil
    local self, entries, changed, worlds, cleared, restored, sources = {}, {}, {}, {}, {}, {}, {}
    local serial, dirty, previous_time, earliest_time = 0, false, nil, nil
    local function prune_future(now)
        if not now then return end
        for _, books in ipairs({ worlds, sources }) do
            for _, book in ipairs(books) do
                -- Delayed room identity must still discard the other shard's old future timeline.
                local cutoff = book.key == key and now or master and book.master == master and earliest_time
                if cutoff then
                    for index = #book.entries, 1, -1 do
                        if book.entries[index].at > cutoff + 2 then
                            table.remove(book.entries, index)
                            dirty = true
                        end
                    end
                end
            end
        end
    end
    function self:time()
        local state, day = world.state, G.TUNING.TOTAL_DAY_TIME
        if not finite(state.cycles) or not finite(state.time) or not finite(day) or day <= 0 then return end
        local now = (state.cycles + state.time) * day
        earliest_time = math.min(earliest_time or now, now)
        if previous_time and now < previous_time - 2 then
            for id, record in pairs(entries) do
                if record.at > now + 2 then entries[id], changed[id], dirty = nil, true, true end
            end
            prune_future(now)
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
        local saved = { key = key, master = master, owner = owner, entries = {} }
        if not master then
            for _, book in ipairs(sources) do
                if book.key == key and book.owner == owner then saved.master = book.master; break end
            end
        end
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
            if previous.key ~= key then
                local retained = { key = previous.key, master = previous.master, owner = previous.owner, entries = {} }
                for _, record in ipairs(previous.entries) do
                    -- An unresolved room at shutdown cannot safely retain another shard's personal snapshot.
                    local stale = record.clock == "player" and record.owner == owner
                        and (not previous.master or previous.master == master or not master and ctx.scope.closed)
                    if not stale then retained.entries[#retained.entries + 1] = record end
                end
                result[#result + 1] = retained
                break
            end
        end
        worlds, dirty = result, false
        ctx.store:set("journal", result)
    end
    local function restore_personal()
        if not loaded_at then return end
        local selected, now = nil, self:time()
        if not now then return end
        for _, book in ipairs(sources) do
            if book.owner == owner and (book.key == key or master and book.master == master) then
                selected = book
                break
            end
        end
        if source == selected then return end
        for id in pairs(restored) do if not changed[id] then entries[id] = nil end end
        source, restored, dirty = selected, {}, true
        for _, record in ipairs(selected and selected.entries or {}) do
            if record.clock == "player" and record.owner == owner and record.at <= now + 2
                and not changed[record.key] and not cleared[record.kind]
                and record.remaining and record.remaining > 0 and record.remaining <= 86400
                and loaded_at + record.remaining > G.GetTime() then
                local copy = clean(record)
                copy.due = loaded_at + copy.remaining
                entries[copy.key], restored[copy.key] = copy, true
            end
        end
        trim()
    end
    if ctx.store then
        ctx.scope:defer(ctx.store:on_ready(function()
            if ctx.scope.closed then return end
            local saved = ctx.store:get("journal")
            local now = self:time()
            loaded_at = G.GetTime()
            for _, book in ipairs(type(saved) == "table" and saved or {}) do
                if type(book) == "table" and type(book.key) == "string" and #book.key <= 200 and type(book.entries) == "table" then
                    local valid = { key = book.key, master = identifier(book.master),
                        owner = type(book.owner) == "string" and #book.owner <= 128 and book.owner or nil, entries = {} }
                    for index, record in ipairs(book.entries) do
                        if index > 76 then break end
                        local copy = clean(record)
                        if copy and (copy.clock ~= "player" or valid.owner and copy.owner == valid.owner) then
                            valid.entries[#valid.entries + 1] = copy
                            if copy.clock ~= "player" and book.key == key and now and copy.at <= now + 2
                                and not changed[copy.key] and not cleared[copy.kind] then entries[copy.key] = copy end
                        end
                    end
                    worlds[#worlds + 1] = valid
                    if #worlds == 2 then break end
                end
            end
            sources = worlds
            prune_future(now)
            restore_personal()
            trim()
            self:flush()
        end))
    end
    if world.net then
        ctx.scope:listen("mastersessioniddirty", function()
            local value = master_session()
            if value == master then return end
            master, dirty = value, true
            prune_future(self:time())
            restore_personal()
            self:flush()
        end, world.net)
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
