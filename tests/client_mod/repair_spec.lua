local T = require("tests.client_mod.support")
local Fixtures = require("tests.client_mod.support.inventory")
local Inventory = require("fieldkit/game/inventory")
local Repair = require("fieldkit/features/repair")

local function fixture(mode)
    local s, player = Fixtures.new(), T.entity()
    T.tasks(player)
    local inv = s:inventory(player, 5)
    local ctx = { player = player, params = { auto_repair = true }, config = { work = true } }
    function ctx:text(_, en) return en end
    function ctx:ready() return not s.focus end
    local G = { GetTime = function() return s.now end, EQUIPSLOTS = { HANDS = "hands", HEAD = "head" },
        ACTIONS = { ADDFUEL = { id = "ADDFUEL", code = 2 }, SEW = { id = "SEW", code = 3 } },
        RPC = { ControllerUseItemOnItemFromInvTile = 1, SetMovementPredictionEnabled = 4 }, STRINGS = { NAMES = {} } }
    ctx.G = G
    ctx.scope = require("fieldkit/core/scope").new(player, error)
    ctx.executor = require("fieldkit/core/executor").new(G.GetTime, function() return not s.focus end,
        function(message) s.message = message end, error)
    ctx.equipment = { ready = function(_, job) return ctx.executor.current == job end }
    local pc = { inst = player, ismastersim = mode == "host", IsEnabled = function() return true end,
        IsBusy = function() return s.busy end, IsAnyOfControlsPressed = function() return s.held end,
        CanLocomote = function() return true end, ClearActionHold = function() end, RemoteStopWalking = function() end }
    player.components.playercontroller = pc
    local source = T.source("components/playercontroller")
    pc.GetItemUseAction = T.eval("local PlayerController = {}\n"
        .. assert(source:match("local function ValidateItemUseAction%b().-\nend")) .. "\n"
        .. assert(source:match("function PlayerController:GetItemUseAction%b().-\nend"))
        .. "\nreturn PlayerController.GetItemUseAction", G)
    player.components.playeractionpicker = { GetUseItemActions = function(_, target, item)
        if not ((target.prefab == "lantern" or target.prefab == "minerhat") and item.prefab == "lightbulb"
            or target.prefab == "winterhat" and item.prefab == "sewing_kit") then return {} end
        local act = { doer = player, target = target, invobject = item,
            action = target.prefab == "winterhat" and G.ACTIONS.SEW or G.ACTIONS.ADDFUEL, options = {},
            IsValid = function() return target:IsValid() and item:IsValid() end,
            AddSuccessAction = function(self, fn) self.success = fn end,
            AddFailAction = function(self, fn) self.failure = fn end }
        s.selected = act
        return { act }
    end }
    local function submitted(action)
        assert(not s.pending, "only one repair request may be outstanding")
        s.pending, s.calls = action, s.calls + 1
    end
    if mode ~= "remote" then
        pc.locomotor = {
            Stop = function() end,
            Clear = function(self) self.bufferedaction = nil end,
            PushAction = function(self, action) self.bufferedaction = action; submitted(action) end,
            PreviewAction = function(self, action)
                self.bufferedaction = action
                s.preview = action.preview_cb
            end,
        }
    end
    G.SendRPCToServer = function(rpc, code, target, material)
        if rpc == G.RPC.SetMovementPredictionEnabled then return end
        assert(rpc == G.RPC.ControllerUseItemOnItemFromInvTile and code == s.selected.action.code)
        assert(target == s.selected.target and material == s.selected.invobject)
        submitted(s.selected)
    end
    function s:item_with_usage(prefab, percent, count)
        local item = self:item(prefab, count)
        item.percent = percent
        item.replica.inventoryitem.classified = { percentused = { value = function() return item.percent end } }
        item.replica.inventoryitem.IsDeployable = function() return false end
        if not count then item.replica.stackable, item.components.stackable = nil, nil end
        return item
    end
    s.lamp, s.fuel = s:item_with_usage("lantern", 20), s:item_with_usage("lightbulb", 255, 5)
    inv.equips.hands, inv.slots[1] = s.lamp, s.fuel
    s.service = Repair.new(ctx)
    function s:step(dt)
        self.now = self.now + (dt or .1)
        self.service:update()
        ctx.executor:tick()
    end
    return s, ctx, inv
end

T.each("native repair dispatch and replicated receipts", { "host", "predict", "remote" }, function(mode)
    local s, ctx, inv = fixture(mode)
    s:step()
    if s.preview then s.preview() end
    assert(s.calls == 1 and ctx.executor.current)
    s:step()
    assert(s.calls == 1, "dispatch alone never counts as repair")
    s.lamp.percent = 50
    s:step()
    assert(ctx.executor.current and Inventory.busy(ctx, inv), "durability alone does not confirm our material was consumed")
    s.fuel.count = 4
    s:step()
    assert(not ctx.executor.current and not Inventory.busy(ctx, inv))
    s:step()
    assert(s.calls == 1, "a repaired item above its threshold consumes no more fuel")
    ctx.scope:close()
end)

T.each("automatic repair yields to player and other work", { "focus", "busy", "held", "cursor", "job", "off", "locked" }, function(kind)
    local s, ctx, inv = fixture("remote")
    if kind == "cursor" then inv.active = s.fuel
    elseif kind == "job" then ctx.executor.current = { name = "another task" }
    elseif kind == "off" then ctx.params.auto_repair = false
    elseif kind == "locked" then s.lamp.locked = true
    else s[kind] = true end
    s.service:update()
    assert(s.calls == 0 and (kind == "job" or not ctx.executor.current))
    ctx.scope:close()
end)

T.case("cancelled preview and unconfirmed repair cannot keep spending", function()
    local s, ctx, inv = fixture("predict")
    s:step()
    ctx.executor:cancel("cancelled")
    s.preview()
    assert(s.calls == 0 and not ctx.params.auto_repair)
    s:step(7)
    assert(not ctx.executor.current and not Inventory.busy(ctx, inv))
    s, ctx, inv = fixture("remote")
    s:step()
    s:step(7)
    s:step()
    assert(s.calls == 1 and not ctx.executor.current and s.message,
        "timeout suspends the item instead of retrying every periodic update")
end)

T.case("sewing uses tool durability and head equipment is included", function()
    local s, ctx, inv = fixture("remote")
    s.lamp.percent = 90
    local hat, kit = s:item_with_usage("winterhat", 10), s:item_with_usage("sewing_kit", 100)
    inv.equips.head, inv.slots[2] = hat, kit
    assert(s.service:set_percent("winterhat", 25))
    assert(s.service:toggle_material("winterhat", "sewing_kit"))
    s:step()
    assert(s.calls == 1 and s.pending.target == hat and s.pending.invobject == kit)
    hat.percent, kit.percent = 60, 80
    s:step()
    assert(not ctx.executor.current and not Inventory.busy(ctx, inv))
end)

T.each("repair rechecks the threshold at dispatch", { "repaired", "disabled" }, function(change)
    local s, ctx = fixture("remote")
    s.service:update()
    assert(ctx.executor.current and s.calls == 0)
    if change == "repaired" then s.lamp.percent = 90 else ctx.params.auto_repair = false end
    ctx.executor:tick()
    assert(s.calls == 0 and not ctx.executor.current, "a stale low-durability observation cannot authorize material use")
    ctx.scope:close()
end)

T.case("repair waits for the material backpack to finish syncing", function()
    local s, ctx, inv = fixture("remote")
    local backpack = s:container(T.entity({ prefab = "backpack" }), 4)
    inv.slots[1], inv.overflow, backpack.slots[1] = nil, backpack, s.fuel
    s.service:update()
    backpack.busy = true
    ctx.executor:tick()
    assert(s.calls == 0 and ctx.executor.current)
    backpack.busy = false
    ctx.executor:tick()
    assert(s.calls == 1 and s.pending.invobject == s.fuel)
    ctx.executor:cancel("manual"); ctx.scope:close()
end)

T.done()
