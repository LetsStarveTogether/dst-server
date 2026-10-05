local T = require("tests.client_mod.support")
local Fishing = require("fieldkit/features/fishing")
local Executor = require("fieldkit/core/executor")
local Projectile = T.native("components/complexprojectile", { Class = T.Class, Vector3 = T.Vector3 })

local function fixture(mode)
    local s = { now = 0, requests = {}, rpc = {}, messages = {}, castable = true }
    local player, item = T.entity(), T.entity({ prefab = "oceanfishingrod", GUID = 2 })
    local G = { ACTIONS = {}, EQUIPSLOTS = { HANDS = "hands" }, Vector3 = T.Vector3,
        TUNING = { OCEAN_FISHING = { REEL_ACTION_REPEAT_DELAY = .3 } },
        GetTime = function() return s.now end,
        RPC = { RightClick = "right", SetMovementPredictionEnabled = "prediction" },
        SendRPCToServer = function(...) s.rpc[#s.rpc + 1] = { ... } end }
    for code, id in ipairs({ "OCEAN_FISHING_CAST", "OCEAN_FISHING_REEL", "OCEAN_FISHING_CATCH", "OCEAN_FISHING_STOP" }) do
        G.ACTIONS[id] = { id = id, code = code }
    end
    local function net(initial)
        return { current = initial, set = function(self, value) self.current = value end,
            value = function(self) return self.current end }
    end
    local Rod = T.native("components/oceanfishingrod_replica", { Class = T.Class,
        net_entity = function() return net() end, net_tinybyte = function() return net(0) end,
        net_smallbyte = function() return net(20) end })
    local rod = Rod(item)
    item.replica.oceanfishingrod = rod
    s.rod, s.item = rod, item
    local inventory = { hands = item }
    function inventory:GetEquippedItem() return self.hands end
    function inventory:GetActiveItem() return self.active end
    player.replica.inventory = inventory
    function player:GetDistanceSqToPoint(x, _, z) return (self.x - x)^2 + (self.z - z)^2 end
    local controller = { ismastersim = mode == "host" }
    function controller:IsEnabled() return true end
    function controller:IsBusy() return s.busy == true end
    function controller:GetPlatformRelativePosition(x, z) return nil, x, z end
    function controller:ClearActionHold() end
    function controller:RemoteStopWalking() s.stopped = true end
    if mode ~= "remote" then
        controller.locomotor = { Stop = function() s.stopped = true end,
            Clear = function(self) self.bufferedaction = nil end }
    end
    function controller:DoAction(action)
        s.requests[#s.requests + 1] = action
        player.bufferedaction = action
        if self.locomotor then self.locomotor.bufferedaction = action end
        if mode == "remote" then action.non_preview_cb()
        elseif mode == "predict" and not s.delay_preview then action.preview_cb() end
    end
    player.components.playercontroller = controller
    local fishing_action = T.eval(assert(T.source("componentactions")
        :match("local function GetFishingAction%b().-\nend")) .. "\nreturn GetFishingAction", { ACTIONS = G.ACTIONS })
    local picker = {}
    function picker:GetPointActions(point, equipped, right)
        assert(right and equipped == item)
        local target = rod:GetTarget()
        local definition = target and fishing_action(player, target) or not target and s.castable and G.ACTIONS.OCEAN_FISHING_CAST
        if not definition then return {} end
        local action = { doer = player, invobject = item, action = definition, rotation = 0 }
        function action:IsValid() return item:IsValid() end
        function action:GetActionPoint() return point end
        function action:AddSuccessAction(fn) self.success = fn end
        function action:AddFailAction(fn) self.failure = fn end
        return { action }
    end
    player.components.playeractionpicker = picker
    local ctx = { player = player, G = G, config = { work = true }, params = { fish_count = 1 } }
    function ctx:text(_, en) return en end
    function ctx:ready() return true end
    ctx.executor = Executor.new(G.GetTime, function() return true end,
        function(message) s.messages[#s.messages + 1] = message end, error)
    ctx.equipment = { ready = function(_, job) return ctx.executor.current == job end }
    ctx.fishing = Fishing.new(ctx)
    function s:tick(now) self.now = now or self.now + .1; ctx.executor:tick() end
    function s:target(tags)
        local target = T.entity({ tags = tags or {} })
        rod._target:set(target)
        player:AddTag("fishing_idle")
        return target
    end
    function s:deliver(identified)
        local action = self.requests[#self.requests]
        if mode == "host" then action.success()
        else player:PushEvent("performaction", identified and { action = action } or nil) end
        player:ClearBufferedAction()
        if controller.locomotor then controller.locomotor:Clear() end
    end
    return ctx, s, player, inventory
end

T.each("ocean fishing cycle", { "host", "predict", "remote" }, function(mode)
    local ctx, s, player = fixture(mode)
    assert(ctx.fishing:start({ x = 10, z = 0 }))
    local job = ctx.executor.current
    ctx.params.fish_count = 20
    s:tick(0); s:tick(.1)
    assert(#s.requests == 1 and s.requests[1].action.id == "OCEAN_FISHING_CAST")
    s:target({ fishinghook = true }); s:deliver(); s:tick(.2); s:tick(.3)
    assert(#s.requests == 1, "leave the bobber in the water while waiting for a bite")
    local fish = s:target({ partiallyhooked = true })
    s.rod._line_tension:set(2); s:tick(.4)
    assert(#s.requests == 1, "high line tension must not generate a reeling request")
    s.rod._line_tension:set(0); s:tick(.5)
    assert(s.requests[2].action.id == "OCEAN_FISHING_REEL")
    s:deliver(); s:tick(.6)
    assert(#s.requests == 2, "native reel repeat interval is respected")
    fish:RemoveTag("partiallyhooked"); fish:AddTag("oceanfishing_catchable")
    s.rod._line_tension:set(2); s:tick(.7)
    assert(s.requests[3].action.id == "OCEAN_FISHING_CATCH", "a catchable fish can be caught without another pull")
    s:deliver(); s:tick(.8)
    assert(job.caught == 0, "an action echo alone is not a caught fish")
    Projectile(fish); s.rod._target:set(nil); player:RemoveTag("fishing_idle"); s:tick(.9)
    assert(job.caught == 1 and ctx.executor.current == nil, "the captured session limit survives parameter edits")
    assert(T.listener_count(player) == 0)
end)

T.case("remote catch retains the launch until a delayed receipt", function()
    local ctx, s, player = fixture("remote")
    local fish = s:target({ oceanfishing_catchable = true })
    assert(ctx.fishing:start({ x = 10, z = 0 }))
    local job = ctx.executor.current
    s:tick(0)
    Projectile(fish); s.rod._target:set(nil); s:tick(.1)
    assert(job.caught == 0)
    fish:Remove(); s:deliver(); s:tick(.2)
    assert(job.caught == 1 and ctx.executor.current == nil)
end)

T.each("an escaped fish cannot count from an attempt alone", { "anonymous", "identified" }, function(receipt)
    local ctx, s = fixture("remote")
    s:target({ oceanfishing_catchable = true })
    assert(ctx.fishing:start({ x = 10, z = 0 }))
    local job = ctx.executor.current
    s:tick(0); s.rod._target:set(nil); s:deliver(receipt == "identified"); s:tick(.1); s:tick(9)
    assert(job.caught == 0 and not ctx.executor.current and #s.requests == 1)
end)

T.case("recast follows a confirmed catch and the native animation", function()
    local ctx, s, player = fixture("remote")
    ctx.params.fish_count = 5
    local first = s:target({ oceanfishing_catchable = true })
    assert(ctx.fishing:start({ x = 10, z = 0 }))
    local job = ctx.executor.current
    s:tick(0); s:deliver(); Projectile(first)
    s.rod._target:set(nil); player:RemoveTag("fishing_idle"); s.busy = true
    s:tick(.1); s:tick(.2)
    assert(job.caught == 1 and #s.requests == 1, "catch animation must finish before another cast")
    s.busy = false; s:tick(.3)
    assert(s.requests[2].action.id == "OCEAN_FISHING_CAST" and s.requests[2]:GetActionPoint().x == 10)
    s:target({ fishinghook = true }); s:deliver(); s:tick(.4)
    local second = s:target({ oceanfishing_catchable = true })
    s:tick(.5); s:deliver(); Projectile(second); s.rod._target:set(nil); s:tick(.6)
    assert(job.caught == 2 and ctx.executor.current == job)
    ctx.executor:cancel()
end)

T.each("cancellation closes delayed actions", { "cancelled", "manual", "focus", "invalid" }, function(reason)
    local ctx, s, player, inventory = fixture("predict")
    s.delay_preview = true
    assert(ctx.fishing:start({ x = 10, z = 0 }))
    s:tick(0)
    local action, job = s.requests[1], ctx.executor.current
    ctx.executor:cancel(reason)
    action.preview_cb(); player:PushEvent("performaction"); s:tick(.1)
    assert(#s.rpc == 0 and #s.requests == 1 and job.caught == 0 and inventory.hands == s.item)
    assert(T.listener_count(player) == 0 and player:GetBufferedAction() == nil)
end)

T.each("changed fishing conditions stop", { "rod", "lost", "range", "cast_timeout", "cast_rejected" }, function(change)
    local ctx, s, player, inventory = fixture("host")
    if change == "lost" then s:target({ fishinghook = true }) end
    assert(ctx.fishing:start({ x = 10, z = 0 }))
    local job = ctx.executor.current
    if change == "rod" then inventory.hands = nil
    elseif change == "lost" then s.rod._target:set(nil)
    elseif change == "range" then player.x = 100
    elseif change == "cast_rejected" then s.castable = false end
    s:tick(0)
    if change == "cast_timeout" then s:tick(9) end
    assert(not ctx.executor.current and job.caught == 0 and #s.requests <= 1)
end)

T.done()
