local Parameters = require("fieldkit/core/parameters")
local Actions = require("fieldkit/game/actions")
local Inventory = require("fieldkit/game/inventory")
local Query = require("fieldkit/game/query")
local Geometry = require("fieldkit/rules/geometry")
local M = {}
local slots = { meatrack = 3, meatrack_hermit = 1, meatrack_hermit_multi = 9 }

function M.new(ctx)
    local self, player = {}, ctx.player
    local inv, controller = player.replica.inventory, player.components.playercontroller
    local function text(zh, en) return ctx:text(zh, en) end
    local function supported(rack)
        local container = rack and rack:IsValid() and rack.replica and rack.replica.container
        return container and slots[rack.prefab] and container:GetNumSlots() == slots[rack.prefab]
            and not container:AcceptsStacks() and not container:IsReadOnlyContainer() and not rack:HasTag("burnt")
    end
    local function materials()
        local overflow = inv:GetOverflowContainer()
        if not Inventory.readable(ctx, inv) or Inventory.busy(ctx, inv)
            or overflow and (not Inventory.readable(ctx, overflow) or Inventory.busy(ctx, overflow)) then return end
        local result = {}
        for _, entry in ipairs(Inventory.items(ctx)) do
            if entry.slot and entry.item:HasTag("dryable") then
                local count = Inventory.stack_size(entry.item)
                if count == nil then return end
                entry.count = count
                result[#result + 1] = entry
            end
        end
        return result
    end
    function self:ingredients()
        local grouped, result = {}, {}
        for _, entry in ipairs(materials() or {}) do
            local prefab = entry.item.prefab
            if not grouped[prefab] then
                grouped[prefab] = { prefab = prefab, item = entry.item, count = 0 }
                result[#result + 1] = grouped[prefab]
            end
            grouped[prefab].count = grouped[prefab].count + entry.count
        end
        table.sort(result, function(a, b) return a.prefab < b.prefab end)
        return result
    end
    function self:start(targets, prefab)
        if ctx.config.work == false then return false, text("交互辅助已关闭", "Interaction assistance is disabled") end
        local available, reason = ctx:ready()
        if not available then return false, reason end
        if ctx.executor:is_running() then return false, text("请先停止当前操作", "Stop the current operation first") end
        if inv:GetActiveItem() then return false, text("请先放下鼠标物品再操作晾晒架", "Put down the cursor item before using drying racks") end
        if prefab ~= nil and (type(prefab) ~= "string" or prefab == "") then return false, "Invalid drying ingredient" end
        local sources = materials()
        if not sources then return false, text("等待物品栏和背包同步", "Wait for inventory and backpack updates") end
        local budget = 0
        for _, entry in ipairs(sources) do if entry.item.prefab == prefab then budget = budget + entry.count end end
        if prefab and budget == 0 then return false, text("身上没有所选晾晒原料", "The selected drying ingredient is not in your inventory") end
        local racks, seen = {}, {}
        for _, rack in ipairs(targets or {}) do
            if not seen[rack] and supported(rack) and Query.visible(ctx, rack)
                and player:GetDistanceSqToInst(rack) <= ctx.params.search_radius ^ 2 then
                racks[#racks + 1], seen[rack] = rack, true
            end
        end
        if #racks == 0 then return false, text("范围内没有可用的晾晒架", "No supported drying racks in range") end
        if #racks > Parameters.LIMIT then return false, text("晾晒架过多，请缩小范围", "Too many drying racks; narrow the selection") end
        table.sort(racks, function(a, b)
            local da, db = player:GetDistanceSqToInst(a), player:GetDistanceSqToInst(b)
            return da < db or da == db and a.GUID < b.GUID
        end)
        local job = { input = { mode = "world" }, timeout = 600,
            name = text("晾晒收取与补挂", "Harvest and refill drying racks"), harvested = 0, filled = 0, unfilled = 0,
            error_message = text("晾晒操作失败，已停止", "Drying operation failed; stopped"),
            timeout_message = text("晾晒操作超时，已停止", "Drying operation timed out; stopped") }
        local index, slot, phase = 1, 1, "open"
        local ticket, pending, deadline, expect_salt
        local function summary()
            return text("晾晒完成：收取 ", "Drying complete: harvested ") .. job.harvested
                .. text("，补挂 ", ", hung ") .. job.filled
                .. (prefab and text("，剩余空槽 ", ", remaining empty slots ") .. job.unfilled or "")
        end
        local function changed() return "failed", text("晾晒架或库存已改变，操作停止", "Drying rack or inventory changed; stopped") end
        function job:step(now)
            if inv:GetActiveItem() then return "failed", text("鼠标物品已改变，晾晒停止", "Cursor item changed; drying stopped") end
            local rack = racks[index]
            if not rack then return "done", summary() end
            if not supported(rack) or not Query.visible(ctx, rack) then return changed() end
            local container = rack.replica.container
            deadline = deadline or now + 15
            if phase == "open" then
                if Inventory.readable(ctx, container) and not Inventory.busy(ctx, container) then
                    if ticket then ticket:close(); ticket = nil end
                    phase, deadline = "harvest", now + 6
                elseif now >= deadline or ticket and ticket.failed then
                    return "failed", text("晾晒架未能打开，已停止", "Drying rack could not be opened; stopped")
                elseif not ticket and not controller:IsBusy() then
                    local action, button = Actions.pick(ctx, rack, { RUMMAGE = true })
                    if not action then return "failed", text("原生开架动作不可用", "Native rack opening action is unavailable") end
                    ticket, reason = Actions.dispatch(ctx, action, button)
                    if not ticket then return "failed", reason end
                end
                return "running"
            end
            if not Inventory.readable(ctx, container) or not container:CanBeOpened() then return changed() end
            if pending then
                local state = pending:state()
                if state == "changed" then return changed() end
                if state ~= "done" then
                    if now >= deadline then return "failed", self.timeout_message end
                    return "running"
                end
                pending, deadline = nil, now + 6
                if phase == "fill" then self.filled, budget = self.filled + 1, budget - 1
                else self.harvested = self.harvested + 1 end
                if expect_salt then phase, expect_salt = "salt", false
                else
                    if phase == "salt" then phase = "harvest" end
                    slot = slot + 1
                end
            end
            if Inventory.busy(ctx, inv) or Inventory.busy(ctx, container) or controller:IsBusy() then
                if now >= deadline then return "failed", self.timeout_message end
                return "running"
            end
            if slot > slots[rack.prefab] then
                if phase == "harvest" and prefab then phase, slot = "fill", 1
                else index, phase, slot, deadline = index + 1, "open", 1, nil; return "running" end
            end
            local entry, why = Inventory.slot(ctx, container, slot)
            if not entry then
                if why ~= "busy" then return changed() end
                if now >= deadline then return "failed", self.timeout_message end
                return "running"
            end
            if phase == "salt" then
                -- Native hermit racks add salt on the next static tick. Filling early drops it on the ground.
                if not entry.item then
                    if now >= deadline then return "failed", text("盐尚未同步，已停止补挂", "Salt did not synchronize; refilling stopped") end
                    return "running"
                end
                if entry.item.prefab ~= "saltrock" then return changed() end
            end
            if phase ~= "fill" then
                if not entry.item or entry.item:HasTag("dryable") then slot, deadline = slot + 1, now + 6; return "running" end
                if Inventory.capacity(ctx, inv, entry.item, container) < entry.count then
                    return "failed", text("物品栏和背包空间不足，晾晒停止", "Inventory and backpack lack space; drying stopped")
                end
                local salt = entry.item.components and entry.item.components.driedsalticon
                expect_salt = phase == "harvest" and rack.prefab == "meatrack_hermit_multi"
                    and salt and salt.showicon:value() or false
                pending, why = Inventory.move(ctx, entry, player, entry.count,
                    expect_salt and { source_replacement = "saltrock" } or nil)
            elseif entry.item or budget == 0 then
                if not entry.item then self.unfilled = self.unfilled + 1 end
                slot, deadline = slot + 1, now + 6
                return "running"
            else
                local stock = materials()
                if not stock then
                    if now >= deadline then return "failed", self.timeout_message end
                    return "running"
                end
                local source
                for _, candidate in ipairs(stock) do
                    if candidate.item.prefab == prefab then source = candidate; break end
                end
                if not source then budget = 0; return "running" end
                if not container:CanTakeItemInSlot(source.item, slot) then return changed() end
                pending, why = Inventory.move(ctx, source, rack, 1)
            end
            if not pending then return "failed", text("晾晒物品已不可移动", "The drying item can no longer be moved") end
            deadline = now + 6
            return "running"
        end
        function job:cancel(why) Actions.stop(ctx, ticket, why) end
        return ctx.executor:start(job)
    end
    local function targets(area)
        return Query.nearby(ctx, ctx.params.search_radius, nil, nil, nil, function(rack)
            local p = rack:GetPosition()
            return supported(rack) and (not area or Geometry.contains(area, p.x, p.z))
        end, Parameters.LIMIT + 1)
    end
    function self:nearby(prefab) return self:start(targets(), prefab) end
    function self:area(area, prefab)
        if type(area) ~= "table" or type(area.corners) ~= "table" or #area.corners < 3 then
            return false, text("请先框选区域", "Select an area first")
        end
        return self:start(targets(area.corners), prefab)
    end
    return self
end

return M
