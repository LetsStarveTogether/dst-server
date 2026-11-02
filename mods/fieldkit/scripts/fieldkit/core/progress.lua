local M = {}
local reasons = {
    { "changed", "目标已改变", "Target changed" },
    { "materials", "材料或工具不可用", "Materials or tools unavailable" },
    { "unreached", "未到达目标", "Target not reached" },
    { "rejected", "动作被拒绝", "Action rejected" },
    { "unavailable", "动作不可用", "Action unavailable" },
    { "unconfirmed", "结果未确认", "Result unconfirmed" },
}
local states = { done = { "已结束", "Finished" }, failed = { "失败", "Failed" },
    timeout = { "超时", "Timed out" }, focus = { "焦点改变", "Focus changed" },
    replaced = { "已替换", "Replaced" }, cancelled = { "已取消", "Cancelled" },
    manual = { "手动操作接管", "Manual input" }, invalid = { "角色已失效", "Player unavailable" } }

function M.skip(job, reason, count)
    count = count or 1
    job.skipped = (job.skipped or 0) + count
    job.reasons = job.reasons or {}
    reason = reason or "unconfirmed"
    job.reasons[reason] = (job.reasons[reason] or 0) + count
end

-- Copy only display data so the last result never retains entities or a running job.
function M.snapshot(job)
    if not job then return end
    local result = { name = job.name, completed = job.completed, skipped = job.skipped, reasons = {} }
    if job.remaining then result.remaining = job:remaining()
    elseif job.total and job.total > 0 then
        result.remaining = math.max(0, job.total - (job.completed or 0) - (job.skipped or 0))
    end
    for reason, count in pairs(job.reasons or {}) do result.reasons[reason] = count end
    return result
end

function M.reasons(ctx, result)
    local parts = {}
    for _, reason in ipairs(reasons) do
        local count = result.reasons and result.reasons[reason[1]]
        if count then parts[#parts + 1] = ctx:text(reason[2], reason[3]) .. " ×" .. count end
    end
    return table.concat(parts, " · ")
end

function M.label(ctx, result)
    local label = result.name or ""
    if result.state then
        local state = states[result.state] or states.cancelled
        label = label .. " · " .. ctx:text(state[1], state[2])
    end
    if result.completed ~= nil then label = label .. ctx:text(" · 完成 ", " · Completed ") .. result.completed end
    if result.remaining ~= nil then label = label .. ctx:text(" · 剩余 ", " · Remaining ") .. result.remaining end
    if result.skipped ~= nil then label = label .. ctx:text(" · 跳过 ", " · Skipped ") .. result.skipped end
    return label
end

return M
