local T = require("tests.client_mod.support")
local Fixtures = require("tests.client_mod.support.inventory")
local Drying = require("fieldkit/features/drying")
local Inventory = require("fieldkit/game/inventory")
local Executor = require("fieldkit/core/executor")

local function fixture()
    local s = Fixtures.new()
    s.entities, s.moves, s.opens = {}, 0, 0
    local function entity(prefab, x)
        s.nextguid = s.nextguid + 1
        return T.entity({ GUID = s.nextguid, prefab = prefab, x = x })
    end
    function s:food(prefab, count, dryable) return self:item(prefab, count, { tags = { dryable = dryable == true } }) end
    local player = entity("wilson")
    local inv = s:inventory(player, 12)
    s.after_move = function(box, slot, source)
        if source.salted then
            source.salted = false
            s.salt = function() box.slots[slot] = s:item("saltrock") end
        end
    end
    function s:rack(prefab, x)
        local e = entity(prefab or "meatrack", x)
        local box = self:container(e, ({ meatrack = 3, meatrack_hermit = 1, meatrack_hermit_multi = 9 })[e.prefab], {
            open = false, stacks = false, accept = function(_, source) return source:HasTag("dryable") end,
        })
        self.entities[#self.entities + 1] = e
        return e, box
    end
    function s:backpack()
        inv.overflow = self:container(entity("backpack"), 4)
        return inv.overflow
    end
    local request = s.request
    function s:request(fn, owners)
        self.moves = self.moves + 1
        return request(self, fn, owners)
    end
    local controller = { ismastersim = true }
    function controller:IsEnabled() return true end
    function controller:IsBusy() return s.actionbusy end
    function controller:ClearActionHold() end
    function controller:DoAction(action)
        assert(not s.pending)
        s.opens, player.bufferedaction = s.opens + 1, action
        s.pending = function()
            if s.reject_open then action.failed(); return end
            for _, rack in ipairs(s.entities) do rack.replica.container.open = false end
            action.target.replica.container.open = true
            player.bufferedaction = nil
            action.success()
        end
    end
    player.components.playercontroller = controller
    player.components.playeractionpicker = { GetLeftClickActions = function(_, _, target)
        if s.noaction then return {} end
        local action = { target = target, doer = player, action = { id = "RUMMAGE" } }
        function action:IsValid() return target:IsValid() end
        function action:AddSuccessAction(fn) self.success = fn end
        function action:AddFailAction(fn) self.failed = fn end
        return { action }
    end, GetRightClickActions = function() return {} end }
    local ctx = { player = player, config = {}, params = { search_radius = 20 }, G = {
        ACTIONS = {}, CanEntitySeeTarget = function() return true end,
        TheSim = { FindEntities = function() return s.entities end },
    } }
    function ctx:text(_, en) return en end
    function ctx:ready() return not s.focus end
    ctx.executor = Executor.new(function() return s.now end, function() return not s.focus end,
        function(message) s.messages[#s.messages + 1] = message end, error)
    ctx.drying = Drying.new(ctx)
    function s:tick() self.now = self.now + .1; ctx.executor:tick() end
    function s:deliver_salt() local salt = self.salt; self.salt = nil; if salt then salt() end end
    function s:finish()
        for _ = 1, 300 do
            self:tick()
            if not ctx.executor.current then return end
            self:deliver()
            self:deliver_salt()
        end
        error("drying operation did not terminate")
    end
    return ctx, s, inv
end

T.case("harvest unique racks and preserve raw food", function()
    local ctx, s, inv = fixture()
    local a, first = s:rack("meatrack", 1)
    local b, second = s:rack("meatrack_hermit", 2)
    first.slots[1], first.slots[2], first.slots[3] = s:food("meat_dried"), s:food("meat", 1, true), s:food("spoiled_food")
    second.slots[1] = s:food("smallmeat_dried")
    assert(ctx.drying:start({ a, b, a })); s:finish()
    assert(s.moves == 3 and s.opens == 2 and first.slots[2].prefab == "meat" and first.slots[1] == nil)
    assert(Inventory.count(ctx, "meat_dried") == 1 and Inventory.count(ctx, "spoiled_food") == 1)
    assert(second.open and not first.open, "native opening closes prior rack; final rack remains inspectable")
end)
T.case("finite refill budget across backpack and racks", function()
    local ctx, s, inv = fixture()
    local a, first = s:rack("meatrack", 1)
    local b, second = s:rack("meatrack", 2)
    inv.slots[1] = s:food("meat", 2, true)
    local backpack = s:backpack()
    backpack.slots[1] = s:food("meat", 2, true)
    first.slots[1] = s:food("meat_dried")
    second.slots[2] = s:food("smallmeat", 1, true)
    local choices = ctx.drying:ingredients()
    assert(#choices == 1 and choices[1].prefab == "meat" and choices[1].count == 4)
    assert(ctx.drying:start({ a, b }, "meat")); s:finish()
    assert(s.moves == 5 and first.slots[3].prefab == "meat" and second.slots[1].prefab == "meat")
    assert(second.slots[2].prefab == "smallmeat" and second.slots[3] == nil,
        "own stock fills native empty slots once without replacing unfinished food")
    assert(s.messages[#s.messages]:find("remaining empty slots 1"), "stock exhaustion reports unfinished empty slots")
end)
T.each("salt replication", { "immediate", "delayed" }, function(timing)
    T.each("refill", { false, true }, function(refill)
        local ctx, s, inv = fixture()
        local rack, box = s:rack("meatrack_hermit_multi")
        local dried = s:food("meat_dried")
        dried.salted, box.slots[1] = true, dried
        dried.components.driedsalticon = { showicon = { value = function() return dried.salted end } }
        if refill then inv.slots[1] = s:food("meat", 1, true) end
        assert(ctx.drying:start({ rack }, refill and "meat" or nil))
        s:tick(); s:deliver(); s:tick(); s:tick(); s:deliver()
        assert(s.moves == 1 and s.salt)
        if timing == "delayed" then
            for _ = 1, 10 do s:tick() end
            assert(s.moves == 1 and box.slots[1] == nil, "wait for salt before treating the slot as empty")
        end
        s:deliver_salt(); s:finish()
        assert(s.moves == (refill and 3 or 2) and Inventory.count(ctx, "saltrock") == 1)
        assert(Inventory.count(ctx, "meat_dried") == 1, "source replacement must retain the original product")
        assert(refill and box.slots[1].prefab == "meat" or not refill and box.slots[1] == nil)
    end)
end)
T.case("full receiver preserves products", function()
    local ctx, s, inv = fixture()
    local rack, box = s:rack()
    box.slots[1] = s:food("meat_dried")
    for slot = 1, inv:GetNumSlots() do inv.slots[slot] = s:food("log", 20) end
    assert(ctx.drying:start({ rack })); s:finish()
    assert(s.moves == 0 and box.slots[1] and s.messages[#s.messages]:find("lack space"), "full inventory cannot eject products")
end)
T.case("manual close and late open cancellation", function()
    local ctx, s, inv = fixture()
    local rack, box = s:rack()
    inv.slots[1] = s:food("meat", 2, true)
    assert(ctx.drying:start({ rack }, "meat"))
    s:tick(); s:deliver(); s:tick()
    box.open = false
    s:finish()
    assert(s.opens == 1 and s.moves == 0, "manual closing stops the task without reopening")
    assert(ctx.drying:start({ rack }, "meat"))
    s:tick(); ctx.executor:cancel("manual"); s:deliver(); s:finish()
    assert(s.moves == 0 and inv.slots[1].count == 2, "cancelled opening never starts delayed transfers")
end)
T.case("snapshot supply budget and locked ingredients", function()
    local ctx, s, inv = fixture()
    local rack, box = s:rack()
    inv.slots[1], inv.slots[2] = s:food("meat", 1, true), s:food("smallmeat", 8, true)
    inv.slots[2].locked = true
    assert(#ctx.drying:ingredients() == 1)
    assert(not ctx.drying:start({ rack }, "smallmeat"))
    assert(ctx.drying:start({ rack }, "meat"))
    inv.slots[1].count = 10
    s:finish()
    assert(s.moves == 1 and inv.slots[1].count == 9, "startup material budget cannot grow during the batch")
    inv.active = s:food("meat", 1, true)
    assert(not ctx.drying:start({ rack }), "cursor must be empty before native opening")
end)
T.case("move rejection is not replayed", function()
    local ctx, s = fixture()
    local rack, box = s:rack()
    box.slots[1], s.reject = s:food("meat_dried"), true
    assert(ctx.drying:start({ rack })); s:finish()
    assert(s.moves == 1 and box.slots[1] and s.messages[#s.messages]:find("timed out"), "refused move is never retried or counted")
end)
T.case("focus takeover", function()
    local ctx, s = fixture()
    local rack, box = s:rack()
    box.slots[1] = s:food("meat_dried")
    assert(ctx.drying:start({ rack })); s:tick(); s:deliver(); s:tick(); s:tick()
    s.focus = true
    s:tick(); s:deliver(); s:finish()
    assert(s.moves == 1 and not ctx.executor.current, "focus takeover leaves the sent transfer alone and schedules nothing else")
end)
T.case("area and maximum batch", function()
    local ctx, s = fixture()
    local a, first = s:rack("meatrack", 1)
    local b, second = s:rack("meatrack", 10)
    first.slots[1], second.slots[1] = s:food("meat_dried"), s:food("meat_dried")
    T.tasks(ctx.player)
    ctx.scope = require("fieldkit/core/scope").new(ctx.player, error)
    ctx.G.TheWorld = { Map = { GetPlatformAtPoint = function() end } }
    local function handler() return { Remove = function() end } end
    ctx.G.TheInput = { AddMoveHandler = handler, AddMouseButtonHandler = handler }
    ctx.work = { preview = function() end }
    function ctx:report(err) error(err) end
    function ctx:notify(message) s.messages[#s.messages + 1] = message end
    ctx.selection = require("fieldkit/features/selection").new(ctx)
    assert(ctx.selection:mark_start({ x = 0, z = -1 }))
    assert(ctx.selection:mark_end({ x = 2, z = 1 }))
    local widgets = require("tests.client_mod.support.widgets")
    widgets.install()
    package.loaded["widgets/screen"] = widgets
    local env = setmetatable({ TheFrontEnd = { PopScreen = function() end } }, { __index = _G })
    local Panel = setfenv(assert(loadfile("mods/fieldkit/scripts/fieldkit/ui/panel.lua")), env)()
    local panel = setmetatable({ ctx = ctx, section = "drying_food", drying_scope = "area", intent = {} }, Panel)
    ctx.screen = panel
    for _, row in ipairs(panel:Rows()) do
        if row.label == "Harvest only; keep unfinished food" then row.fn(); break end
    end
    assert(ctx.executor.current and ctx.screen == nil, "the real selection and panel start a drying area job")
    s:finish()
    assert(first.slots[1] == nil and second.slots[1], "area only visits racks inside the selected polygon")
    for _ = 1, 128 do s:rack() end
    assert(not ctx.drying:nearby(), "oversized batches are rejected instead of silently truncated")
    ctx.scope:close()
end)

T.done()
