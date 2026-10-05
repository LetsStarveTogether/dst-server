local T = require("tests.client_mod.support")
local Scope = require("fieldkit/core/scope")
local InventoryFixture = require("tests.client_mod.support.inventory")

T.case("scope survives already queued callbacks", function()
    local actor, source = T.entity(), T.entity()
    local tasks, calls = T.tasks(actor), 0
    local scope = Scope.new(actor, error)
    local fn = function() calls = calls + 1 end
    local queued = scope:listen("dirty", fn, source)
    scope:periodic(1, fn)
    source:PushEvent("dirty")
    assert(calls == 1 and T.listener_count(source) == 1)
    scope:close()
    assert(T.listener_count(source) == 0 and tasks[1].cancelled)
    -- A cancelled engine task may already have queued its callback.
    queued(source)
    tasks[1].fn(actor)
    assert(calls == 1)
end)

T.case("native event ownership and duplicate removal", function()
    local source, a, b, calls = T.entity(), T.entity(), T.entity(), 0
    local callback = function(sender) assert(sender == source); calls = calls + 1 end
    a:ListenForEvent("dirty", callback, source)
    a:ListenForEvent("dirty", callback, source)
    b:ListenForEvent("dirty", callback, source)
    source:PushEvent("dirty")
    assert(calls == 3)
    a:RemoveEventCallback("dirty", callback, source)
    source:PushEvent("dirty")
    assert(calls == 4 and T.listener_count(source) == 1)
    b:RemoveEventCallback("dirty", callback, source)
    source:PushEvent("dirty")
    assert(calls == 4 and T.listener_count(source) == 0)
end)

T.case("native dispatch snapshots subscriptions", function()
    local source, actor, calls = T.entity(), T.entity(), {}
    local late = function() calls[#calls + 1] = "late" end
    local removed = function() calls[#calls + 1] = "removed" end
    actor:ListenForEvent("dirty", function()
        calls[#calls + 1] = "first"
        actor:RemoveEventCallback("dirty", removed, source)
        actor:ListenForEvent("dirty", late, source)
    end, source)
    actor:ListenForEvent("dirty", removed, source)
    source:PushEvent("dirty")
    assert(table.concat(calls, ",") == "first,removed")
    calls = {}
    source:PushEvent("dirty")
    assert(table.concat(calls, ",") == "first,late")
    actor:Remove()
    assert(T.listener_count(source) == 0 and actor.event_listening == nil)
end)

T.case("server receipts stay independent from prediction", function()
    local world, actor = InventoryFixture.new(), T.entity()
    local inv = world:inventory(actor, 2)
    inv.slots[1] = world:item("flint", 10)
    inv:TakeActiveItemFromCountOfSlot(1, 3)
    assert(world.calls == 1 and inv.busy and inv.active == nil and inv.slots[1].count == 10)
    local ok = pcall(function() inv:TakeActiveItemFromCountOfSlot(1, 2) end)
    assert(not ok, "the fake server must reject overlapping requests")
    world:deliver(true)
    assert(inv.busy and inv.active.count == 3 and inv.slots[1].count == 7)
    assert(not pcall(function() inv:TakeActiveItemFromCountOfSlot(1, 2) end), "busy release must precede the next request")
    world:deliver()
    assert(not inv.busy and inv.slots[1].count + inv.active.count == 10)
end)

T.case("shared primitives retain native semantics", function()
    local Base = T.Class(function(self, value) self.value = value end)
    function Base:read() return self.value end
    local Derived = T.Class(Base, function(self, value) Base._ctor(self, value) end)
    assert(Derived(3):read() == 3)
    local a, b = T.entity({ x = -2, z = 3 }), T.entity({ x = 1, z = 7 })
    assert(a:GetPosition():DistSq(b:GetPosition()) == 25 and a:GetDistanceSqToInst(b) == 25)
    local count = 0
    a:ListenForEvent("onremove", function() count = count + 1 end)
    a:Remove(); a:Remove()
    assert(not a:IsValid() and count == 1)
end)

T.case("parameter failures identify the row", function()
    local ok, reason = pcall(function()
        T.each("matrix", { { name = "delayed receipt" } }, function() error("sentinel") end)
    end)
    assert(not ok and reason:find("matrix/delayed receipt", 1, true) and reason:find("sentinel", 1, true))
end)
T.done()
