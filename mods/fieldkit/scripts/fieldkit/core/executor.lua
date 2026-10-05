-- Features own their completion evidence. This owns exclusivity, bounds and cancellation.
local M = {}

function M.new(clock, ready, notify, report)
    local self = { current = nil, generation = 0 }

    local function allowed(job)
        local ok, accepted, reason = pcall(ready, job)
        if not ok then report(accepted); return false, job.error_message, "failed" end
        return accepted, reason, "focus"
    end

    local function finish(reason, message)
        -- Invalidate held input intents even when there is no running job yet.
        self.generation = self.generation + 1
        local job = self.current
        if not job then return end
        self.current = nil
        self.finishing = job
        if job.cancel then
            local ok, err = pcall(job.cancel, job, reason)
            if not ok then report(err) end
        end
        self.finishing = false
        if message then notify(message) end
    end

    function self:is_running()
        return self.current ~= nil
    end

    function self:cancel(reason)
        finish(reason or "cancelled")
    end

    function self:start(job)
        if type(job) ~= "table" or type(job.step) ~= "function"
            or type(job.timeout) ~= "number" or job.timeout ~= job.timeout or job.timeout <= 0 or job.timeout > 600 then
            return false, "Invalid operation"
        end
        local input = job.input or { mode = "world" }
        if type(input) ~= "table" or input.mode ~= "world" and input.mode ~= "inventory"
            or input.cursor ~= nil and input.cursor ~= true and type(input.cursor) ~= "string"
            or input.placement ~= nil and type(input.placement) ~= "boolean" then
            return false, "Invalid input policy"
        end
        if self.finishing then return false, "Operation cleanup is in progress" end
        job.input = input
        local accepted, reason = allowed(job)
        if not accepted then return false, reason end
        self:cancel("replaced")
        -- Cleanup may issue a native request, such as putting away a fishing rod.
        -- Its replication must settle before the next operation can take ownership.
        accepted, reason = allowed(job)
        if not accepted then return false, reason end
        job.started_at = clock()
        job.deadline = job.started_at + job.timeout
        self.current = job
        return true
    end

    function self:tick()
        local job = self.current
        if not job then return end
        local accepted, message, reason = allowed(job)
        if not accepted then finish(reason, message); return end
        local now = clock()
        if now >= job.deadline then finish("timeout", job.timeout_message); return end
        local ok, state, reason = pcall(job.step, job, now)
        if self.current ~= job then return end
        if not ok then
            report(state)
            finish("failed", job.error_message)
        elseif state == "done" or state == "failed" then
            finish(state, reason)
        elseif state ~= "running" then
            report("operation returned an invalid state")
            finish("failed", job.error_message)
        end
    end

    return self
end

return M
