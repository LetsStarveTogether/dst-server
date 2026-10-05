-- A player/HUD owns exactly one scope. Closing it releases resources in reverse order.
local M = {}

function M.new(player, report)
    local self = { player = player, closed = false, cleanups = {} }

    function self:defer(fn)
        if self.closed then
            fn()
        else
            self.cleanups[#self.cleanups + 1] = fn
        end
        return fn
    end

    function self:close()
        if self.closed then return end
        self.closed = true
        for i = #self.cleanups, 1, -1 do
            local ok, err = pcall(self.cleanups[i])
            if not ok and report then report(err) end
        end
        self.cleanups = {}
    end

    function self:listen(event, fn, source)
        source = source or self.player
        local function guarded(...)
            if not self.closed then return fn(...) end
        end
        self.player:ListenForEvent(event, guarded, source)
        self:defer(function() self.player:RemoveEventCallback(event, guarded, source) end)
        return guarded
    end

    function self:periodic(seconds, fn)
        local task = self.player:DoPeriodicTask(seconds, function()
            if not self.closed then
                local ok, err = pcall(fn)
                if not ok then
                    if report then report(err) end
                    self:close()
                end
            end
        end)
        self:defer(function() task:Cancel() end)
        return task
    end

    function self:wrap(object, key, factory)
        local own = rawget(object, key)
        local previous = object[key]
        local replacement = factory(previous)
        local function wrapper(...)
            if self.closed then
                if previous then return previous(...) end
                return
            end
            return replacement(...)
        end
        object[key] = wrapper
        self:defer(function()
            -- A later mod may hold our wrapper. Leave its chain intact; ours is inert.
            if object[key] == wrapper then object[key] = own end
        end)
        return wrapper
    end

    return self
end

return M
