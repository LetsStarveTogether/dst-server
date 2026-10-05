local Inventory = require("fieldkit/game/inventory")
local M = {}

function M.new(ctx)
    local self, inv = {}, ctx.player.replica.inventory
    local function text(zh, en) return ctx:text(zh, en) end
    local function unavailable() return nil, text("库存尚不可操作，请等待同步", "Inventory is unavailable; wait for synchronization") end
    local function stack_limit(item)
        local stack = item and item.replica.stackable
        return stack and (stack.OriginalMaxSize and stack:OriginalMaxSize() or stack:MaxSize()) or 1
    end
    local function usable(owner, slot)
        local entry = Inventory.slot(ctx, owner, slot)
        if not entry or entry.item and entry.item.replica.inventoryitem:IsLockedInSlot() then return unavailable() end
        return entry
    end
    local function cursor()
        if Inventory.busy(ctx, inv) then return end
        local item = inv:GetActiveItem()
        local count = 0
        if item then count = Inventory.stack_size(item) end
        if count ~= nil then return item, count end
    end
    local function step(kind, owner, slot, count)
        return { kind = kind, owner = owner, slot = slot, count = count }
    end
    local function scroll_plan(owner, slot, direction, count)
        local entry, why = usable(owner, slot)
        if not entry then return nil, why end
        local active, size = cursor()
        if size == nil then return unavailable() end
        if direction < 0 then
            if not active or entry.item and not Inventory.stackable(entry.item, active)
                or not owner:CanTakeItemInSlot(active, slot) then return unavailable() end
            local capacity = Inventory.slot_limit(owner, active, entry.item)
            count = math.min(count, size, capacity - entry.count)
            if count <= 0 then return nil, text("此槽位已满", "This slot is full") end
            local plan = {}
            if count == size then plan[1] = step("put", owner, slot, count)
            else for _ = 1, count do plan[#plan + 1] = step("put", owner, slot, 1) end end
            return plan
        end
        if not entry.item or active and not Inventory.stackable(entry.item, active) then return unavailable() end
        count = math.min(count, entry.count, stack_limit(entry.item) - size)
        if count <= 0 then return nil, text("鼠标堆栈已满", "The cursor stack is full") end
        if not active then return { step("take", owner, slot, count) } end
        if owner:AcceptsStacks() and size + entry.count <= stack_limit(entry.item)
            and owner:CanTakeItemInSlot(active, slot) then
            return { step("put", owner, slot, size), step("take", owner, slot, size + count) }
        end
        local owners = { inv }
        local overflow = inv:GetOverflowContainer()
        if overflow then owners[#owners + 1] = overflow end
        for _, temporary in ipairs(owners) do
            if Inventory.readable(ctx, temporary) and not Inventory.busy(ctx, temporary) and temporary:AcceptsStacks() then
                for index = 1, temporary:GetNumSlots() do
                    if not temporary:GetItemInSlot(index) and temporary:CanTakeItemInSlot(active, index) then
                        return { step("put", temporary, index, size), step("take", owner, slot, count),
                            step("put", temporary, index, count), step("take", temporary, index, size + count) }
                    end
                end
            end
        end
        return nil, text("增量取料需要一个空槽暂存鼠标物品", "Incremental pickup needs an empty slot for the cursor stack")
    end
    local function capacity(owner, item, source)
        return Inventory.capacity(ctx, owner, item, source)
    end
    local function destination(owner, item)
        local overflow, choices = inv:GetOverflowContainer(), {}
        if owner ~= inv and owner ~= overflow then
            if capacity(inv, item, owner) > 0 then return ctx.player end
        else
            for entity in pairs(inv:GetOpenContainers() or {}) do
                local target = entity:IsValid() and entity.replica.container
                if target and target ~= owner and target ~= overflow and target.type == "chest" and capacity(target, item, owner) > 0 then
                    choices[#choices + 1] = entity
                end
            end
            table.sort(choices, function(a, b) return a.GUID < b.GUID end)
            if choices[1] then return choices[1] end
            local other = inv
            if owner == inv then other = overflow end
            if other and capacity(other, item, owner) > 0 then return other.inst end
        end
    end
    local function return_plan()
        local active, remaining = cursor()
        if not active or remaining == nil then return unavailable() end
        local owners, result = { inv }, {}
        local overflow = inv:GetOverflowContainer()
        if overflow then owners[#owners + 1] = overflow end
        -- Merge across both inventories before occupying any empty slot.
        for pass = 1, 2 do
            for _, owner in ipairs(owners) do
                for slot = 1, owner:GetNumSlots() do
                    local entry = Inventory.slot(ctx, owner, slot)
                    if remaining > 0 and entry and (pass == 1 and entry.item or pass == 2 and not entry.item)
                        and owner:CanTakeItemInSlot(active, slot)
                        and (not entry.item or owner:AcceptsStacks() and Inventory.stackable(entry.item, active)) then
                        local maximum = Inventory.slot_limit(owner, active, entry.item)
                        local count = math.min(remaining, maximum - entry.count)
                        if count > 0 then
                            if entry.item or count == remaining then result[#result + 1] = step("put", owner, slot, count)
                            else for _ = 1, count do result[#result + 1] = step("put", owner, slot, 1) end end
                            remaining = remaining - count
                        end
                    end
                end
            end
        end
        if #result == 0 then return nil, text("物品栏和背包已满；物品留在鼠标上", "Inventory and backpack are full; item remains on the cursor") end
        return result
    end
    local function plan(intent)
        if intent.kind == "return" then return return_plan() end
        if intent.kind == "scroll" then return scroll_plan(intent.owner, intent.slot, intent.direction, intent.count) end
        local entry, why = usable(intent.owner, intent.slot)
        if not entry or not entry.item then return nil, why or text("槽位已空", "The slot is empty") end
        local target = destination(intent.owner, entry.item)
        if not target then return nil, text("没有已打开且可接收物品的容器", "No open container can receive this item") end
        local receiver = target.replica.container or target.replica.inventory
        local space = capacity(receiver, entry.item, intent.owner)
        local count = math.min(intent.count or entry.count, entry.count, space, stack_limit(entry.item))
        if count <= 0 then return unavailable() end
        local operation = step("move", intent.owner, intent.slot, count)
        operation.destination = target
        return { operation }
    end
    local function enqueue(intents)
        local available, why = ctx:ready("inventory")
        if not available then return false, why end
        local current = ctx.executor.current
        if current and current.storage == self then
            local previous, incoming = current.intents[#current.intents], intents[1]
            if #intents ~= 1 or incoming.kind ~= "scroll" or previous.kind ~= incoming.kind
                or previous.owner ~= incoming.owner or previous.slot ~= incoming.slot or previous.direction ~= incoming.direction then
                return false, text("请等待当前库存操作完成", "Wait for the current inventory operation")
            end
            if #current.intents - current.index + #intents > 32 then
                return false, text("库存操作队列已满", "Inventory operation queue is full")
            end
            for _, intent in ipairs(intents) do current.intents[#current.intents + 1] = intent end
            return true
        end
        local steps, reason = plan(intents[1])
        if not steps then return false, reason end
        local job = { storage = self, intents = intents, index = 1, input = { mode = "inventory", cursor = true, placement = true },
            name = text("库存操作", "Inventory operation"), timeout = 120,
            error_message = text("库存操作失败，已停止", "Inventory operation failed; stopped"),
            timeout_message = text("库存同步超时，已停止", "Inventory synchronization timed out; stopped") }
        local at, pending, deadline = 1
        function job:step(now)
            if pending then
                local state = pending:state()
                if state == "changed" then return "failed", text("库存已改变，操作停止", "Inventory changed; operation stopped") end
                if state ~= "done" then
                    if now >= deadline then return "failed", self.timeout_message end
                    return "running"
                end
                pending, at = nil, at + 1
            end
            if at > #steps then
                self.index = self.index + 1
                if self.index > #self.intents then
                    return "done", self.intents[1].kind == "return" and inv:GetActiveItem()
                        and text("剩余物品仍在鼠标上；没有更多空间", "Remaining items stay on the cursor; no more space") or nil
                end
                steps, reason = plan(self.intents[self.index])
                if not steps then return "failed", reason end
                at = 1
            end
            local operation = steps[at]
            if operation.kind == "move" then
                local entry = Inventory.slot(ctx, operation.owner, operation.slot)
                pending, reason = Inventory.move(ctx, entry or {}, operation.destination, operation.count)
            else pending, reason = Inventory[operation.kind](ctx, operation.owner, operation.slot, operation.count) end
            if not pending then return "failed", text("库存操作已不可执行", "The inventory operation is no longer available") end
            deadline = now + 6
            return "running"
        end
        return ctx.executor:start(job)
    end
    function self:scroll(owner, slot, direction, count)
        count = count or 1
        if direction ~= 1 and direction ~= -1 or type(count) ~= "number" or count ~= math.floor(count)
            or count < 1 or count > 20 then return false, "Invalid inventory intent" end
        return enqueue({ { kind = "scroll", owner = owner, slot = slot, direction = direction, count = count } })
    end
    function self:transfer(owner, slot, count)
        if count ~= nil and (type(count) ~= "number" or count ~= math.floor(count) or count < 1 or count > 65535) then
            return false, "Invalid inventory intent"
        end
        return enqueue({ { kind = "move", owner = owner, slot = slot, count = count } })
    end
    function self:return_active() return enqueue({ { kind = "return" } }) end
    function self:collect(owner, slot)
        local entry, why = usable(owner, slot)
        if not entry or not entry.item then return false, why end
        local active, size = cursor()
        if size == nil or active and not Inventory.stackable(active, entry.item) then return false, "Cursor item does not match" end
        local intents, remaining = {}, stack_limit(entry.item) - size
        for index = 1, owner:GetNumSlots() do
            local source = usable(owner, index)
            if remaining > 0 and source and source.item and (source.item == entry.item or Inventory.stackable(entry.item, source.item)) then
                local count = math.min(source.count, remaining)
                intents[#intents + 1] = { kind = "scroll", owner = owner, slot = index, direction = 1, count = count }
                remaining = remaining - count
            end
        end
        if #intents == 0 then return false, text("没有可合并的同类物品", "No matching items can be collected") end
        return enqueue(intents)
    end
    return self
end

return M
