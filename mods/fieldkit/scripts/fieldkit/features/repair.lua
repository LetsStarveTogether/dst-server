local Actions = require("fieldkit/game/actions")
local Inventory = require("fieldkit/game/inventory")
local M = {}
local allowed = { ADDFUEL = true, ADDWETFUEL = true, REPAIR = true, SEW = true }
local defaults = {
    lantern = { "lightbulb" }, minerhat = { "lightbulb" }, lighter = { "willow_ember" },
    molehat = { "wormlight_lesser", "wormlight" }, thurible = { "nightmarefuel" },
    armorskeleton = { "nightmarefuel" }, orangeamulet = { "nightmarefuel" },
    yellowamulet = { "nightmarefuel" }, waxwelljournal = { "nightmarefuel" }, pocketwatch_weapon = { "nightmarefuel" },
}
local function valid_name(value) return type(value) == "string" and #value <= 80 and value:match("^[%w_]+$") end

function M.new(ctx)
    local G, player, inv = ctx.G, ctx.player, ctx.player.replica.inventory
    local self, rules, edited, blocked = {}, {}, {}, setmetatable({}, { __mode = "k" })
    local enabled = false
    for prefab, materials in pairs(defaults) do
        local copy = {}
        for i, name in ipairs(materials) do copy[i] = name end
        rules[prefab] = { percent = 25, materials = copy }
    end
    local function save()
        if not ctx.store or not ctx.store.ready then return end
        local copy = {}
        for prefab, rule in pairs(rules) do
            local materials = {}
            for i, name in ipairs(rule.materials) do materials[i] = name end
            copy[prefab] = { percent = rule.percent, materials = materials }
        end
        ctx.store:set("repair", copy)
    end
    if ctx.store then
        ctx.scope:defer(ctx.store:on_ready(function()
            if ctx.scope.closed then return end
            local saved = ctx.store:get("repair")
            local count = 0
            for _ in pairs(rules) do count = count + 1 end
            for prefab, rule in pairs(type(saved) == "table" and saved or {}) do
                if valid_name(prefab) and type(rule) == "table" and type(rule.percent) == "number"
                    and rule.percent >= 0 and rule.percent <= 100 and rule.percent == math.floor(rule.percent)
                    and not edited[prefab] and (rules[prefab] or count < 32) then
                    local materials = {}
                    for _, name in ipairs(type(rule.materials) == "table" and rule.materials or {}) do
                        if valid_name(name) and #materials < 8 then materials[#materials + 1] = name end
                    end
                    if not rules[prefab] then count = count + 1 end
                    rules[prefab] = { percent = rule.percent, materials = materials }
                end
            end
            if next(edited) then save() end
        end))
    end
    local function items()
        local all, seen = Inventory.items(ctx), {}
        for _, entry in ipairs(all) do seen[entry.item] = true end
        for _, item in pairs(inv:GetEquips()) do
            if not seen[item] and item:IsValid() and not item.replica.inventoryitem:IsLockedInSlot() then
                all[#all + 1] = { item = item, owner = inv }
            end
        end
        return all
    end
    local function owned(item)
        for _, entry in ipairs(items()) do
            if entry.item == item then return Inventory.readable(ctx, entry.owner), entry.owner end
        end
        return false
    end
    local function action(target, material)
        if not target or not target:IsValid() or not material or not material:IsValid() or target == material then return end
        local selected = Actions.query(ctx, function()
            return player.components.playercontroller:GetItemUseAction(material, target)
        end)
        return selected and allowed[selected.action.id] and selected.target == target
            and selected.invobject == material and selected or nil
    end
    function self:rule(prefab) return rules[prefab] or { percent = 0, materials = {} } end
    function self:targets()
        local result, seen = {}, {}
        for _, entry in ipairs(items()) do
            local item = entry.item
            if Inventory.usage(item) and not seen[item.prefab] then
                seen[item.prefab] = true
                result[#result + 1] = item
            end
        end
        table.sort(result, function(a, b) return a.prefab < b.prefab end)
        return result
    end
    local function edit(prefab)
        if not valid_name(prefab) then return end
        if not rules[prefab] then
            local count = 0
            for _ in pairs(rules) do count = count + 1 end
            if count >= 32 then return end
            rules[prefab] = { percent = 25, materials = {} }
        end
        edited[prefab] = true
        blocked = setmetatable({}, { __mode = "k" })
        return rules[prefab]
    end
    function self:set_percent(prefab, percent)
        if type(percent) ~= "number" or percent < 0 or percent > 100 or percent ~= math.floor(percent) then return false end
        local rule = edit(prefab)
        if not rule then return false end
        rule.percent = percent
        save()
        return true
    end
    function self:materials(target)
        local result, seen = {}, {}
        local selected = {}
        for _, name in ipairs(self:rule(target.prefab).materials) do selected[name] = true end
        for _, entry in ipairs(items()) do
            local material = entry.item
            if not seen[material.prefab] and action(target, material) then
                seen[material.prefab] = true
                result[#result + 1] = { prefab = material.prefab, name = material:GetDisplayName(), enabled = selected[material.prefab] == true }
            end
        end
        for name in pairs(selected) do
            if not seen[name] then
                result[#result + 1] = { prefab = name, name = G.STRINGS.NAMES[name:upper()] or name, enabled = true }
            end
        end
        table.sort(result, function(a, b) return a.prefab < b.prefab end)
        return result
    end
    function self:toggle_material(prefab, material)
        if not valid_name(material) then return false end
        local rule = edit(prefab)
        if not rule then return false end
        for i, name in ipairs(rule.materials) do
            if name == material then table.remove(rule.materials, i); save(); return true end
        end
        if #rule.materials >= 8 then return false end
        rule.materials[#rule.materials + 1] = material
        save()
        return true
    end
    local function start(target, material, selected)
        local before, amount, uses
        local ticket, pending
        local job = { name = ctx:text("维修：", "Repair: ") .. target:GetDisplayName(), timeout = 8,
            input = { mode = "world" }, repair = self,
            error_message = ctx:text("自动维修失败，已暂停此物品", "Repair failed; this item is suspended"),
            timeout_message = ctx:text("维修结果未确认，已暂停此物品", "Repair was not confirmed; this item is suspended") }
        function job:step(now)
            if pending then
                local state = pending:state()
                if state == "done" then return "done" end
                if state ~= "pending" or ticket.failed then return "failed", self.timeout_message end
                return "running"
            end
            local target_owned, target_owner = owned(target)
            local material_owned, material_owner = owned(material)
            if not target_owned or not material_owned then return "failed" end
            if Inventory.busy(ctx, inv) or Inventory.busy(ctx, target_owner) or Inventory.busy(ctx, material_owner)
                or not ctx.equipment:ready(self) then return "running" end
            if player.components.playercontroller:IsBusy() then return "running" end
            before, amount, uses = Inventory.usage(target), Inventory.stack_size(material), Inventory.usage(material)
            if not before then return "failed" end
            local rule = rules[target.prefab]
            if not ctx.params.auto_repair or not rule or rule.percent == 0 or before * 100 > rule.percent then return "done" end
            if not amount then return "running" end
            local current = action(target, material)
            if not current or current.action ~= selected.action then return "failed" end
            pending = { deadline = now + 6 }
            function pending:state()
                local value = Inventory.usage(target)
                if not owned(target) or not value then return "changed" end
                local remaining = material:IsValid() and Inventory.stack_size(material) or 0
                local current_uses = Inventory.usage(material)
                if value > before and (remaining and remaining < amount or uses and current_uses and current_uses < uses) then
                    return "done"
                end
                if G.GetTime() >= self.deadline then return "timeout" end
                return "pending"
            end
            inv._fieldkit_pending_repair = pending
            local reason
            ticket, reason = Actions.dispatch(ctx, current, "inventory")
            if not ticket then inv._fieldkit_pending_repair = nil; return "failed", reason end
            return "running"
        end
        function job:cancel(reason)
            if reason ~= "done" then blocked[target] = true end
            if reason == "cancelled" then ctx.params.auto_repair = false end
            Actions.stop(ctx, ticket, reason)
        end
        return ctx.executor:start(job)
    end
    function self:update()
        if not ctx.params.auto_repair or ctx.config.work == false then enabled = false; return end
        if not enabled then blocked = setmetatable({}, { __mode = "k" }); enabled = true end
        if ctx.executor.current or not ctx:ready() or inv:GetActiveItem() or Inventory.busy(ctx, inv) then return end
        local controller = player.components.playercontroller
        if controller:IsBusy() or player:GetBufferedAction() or player:HasTag("moving")
            or controller:IsAnyOfControlsPressed(G.CONTROL_PRIMARY, G.CONTROL_SECONDARY, G.CONTROL_ACTION,
                G.CONTROL_ATTACK, G.CONTROL_MOVE_UP, G.CONTROL_MOVE_DOWN, G.CONTROL_MOVE_LEFT, G.CONTROL_MOVE_RIGHT) then return end
        local all = items()
        for _, entry in ipairs(all) do
            local target, rule = entry.item, rules[entry.item.prefab]
            local value = rule and rule.percent > 0 and Inventory.usage(target)
            if value and value * 100 <= rule.percent and not blocked[target] then
                for _, prefab in ipairs(rule.materials) do
                    for _, source in ipairs(all) do
                        if source.slot and source.item.prefab == prefab and Inventory.readable(ctx, source.owner)
                            and not Inventory.busy(ctx, source.owner) then
                            local selected = action(target, source.item)
                            if selected then start(target, source.item, selected); return end
                        end
                    end
                end
            end
        end
    end
    ctx.scope:periodic(.5, function() self:update() end)
    return self
end

return M
