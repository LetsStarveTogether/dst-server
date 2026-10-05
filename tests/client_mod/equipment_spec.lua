local T = require("tests.client_mod.support")
local Fixtures = require("tests.client_mod.support.inventory")
local Equipment = require("fieldkit/game/equipment")
local Inventory = require("fieldkit/game/inventory")
local Actions = require("fieldkit/game/actions")

local function fixture(remote)
    local s = Fixtures.new()
    s.time, s.requests, s.ctrl = 0, 0, true
    local player = T.entity()
    local ctx = { player = player, executor = {}, G = { EQUIPSLOTS = { HANDS = "hands" },
        GetTime = function() return s.time end, FRAMES = 1 / 30,
        CONTROL_FORCE_INSPECT = 1, CONTROL_FORCE_ATTACK = 2, CONTROL_FORCE_TRADE = 3, CONTROL_FORCE_STACK = 4,
        ACTIONS = { EQUIP = { code = 1 }, UNEQUIP = { code = 2 }, DROP = { code = 3 } },
        RPC = { UseItemFromInvTile = 10 },
        TheSim = { RegisterFindTags = function() return {} end },
    } }
    function player:HasTag() return false end
    function ctx:text(_, en) return en end
    local function owner(size) return s:container(T.entity(), size) end
    local inv, pack = owner(3), owner(2)
    player.replica.inventory = inv
    function inv:GetActiveItem() return self.active end
    function inv:GetEquippedItem() return self.hands end
    function inv:GetEquips() return self.hands and { hands = self.hands } or {} end
    function inv:GetOverflowContainer() return self.pack end
    function inv:IsHeavyLifting() return self.heavy end
    inv.classified = { IsBusy = function() return inv.busy end }
    local G = ctx.G
    setmetatable(G.ACTIONS, { __index = function(actions, key)
        local action = { code = key }; rawset(actions, key, action); return action
    end })
    G.TheInput = { IsControlPressed = function(_, control) return s.ctrl and control == G.CONTROL_FORCE_TRADE end }
    local env = setmetatable({ Class = T.Class, require = function() return {} end }, {
        __index = function(_, key) if G[key] ~= nil then return G[key] end; return _G[key] end,
    })
    local Picker, Controller = T.native("components/playeractionpicker", env), T.native("components/playercontroller", env)
    local picker = { inst = player, GetInventoryActions = Picker.GetInventoryActions }
    function picker:SortActionList(actions, _, item)
        local result = {}
        for _, action in ipairs(actions) do result[#result + 1] = { action = action, invobject = item, options = {} } end
        return result
    end
    player.components.playeractionpicker = picker
    local controller = { inst = player, ismastersim = not remote, EncodeControlMods = Controller.EncodeControlMods,
        RemoteUseItemFromInvTile = Controller.RemoteUseItemFromInvTile }
    function controller:IsControlPressed(control) return G.TheInput:IsControlPressed(control) end
    function controller:IsBusy() return false end
    function controller:GetMapTarget() end
    player.components.playercontroller = controller
    ctx.scope = { wrap = function(_, object, key, replacement) object[key] = replacement(object[key]) end }
    Actions.attach(ctx)
    local function send(item)
        assert(not s.pending)
        s.pending, s.requests = item, s.requests + 1
    end
    G.SendRPCToServer = function(rpc, action, item, modifiers)
        assert(rpc == G.RPC.UseItemFromInvTile and modifiers == nil,
            "native RemoteUseItemFromInvTile must encode neutral modifiers even while raw Ctrl is held")
        assert(action ~= G.ACTIONS.DROP.code)
        send(item)
    end
    local classified_use = T.eval(
        assert(T.source("prefabs/inventory_classified"):match("local function IsBusy%b().-\nend")) .. "\n"
        .. assert(T.source("prefabs/inventory_classified"):match("local function TryNonNetworkedAction%b().-\nend")) .. "\n"
        .. assert(T.source("prefabs/inventory_classified"):match("local function UseItemFromInvTile%b().-\nend"))
        .. "\nreturn UseItemFromInvTile", env)
    function inv:UseItemFromInvTile(item)
        if remote then classified_use({ _parent = player }, item); return end
        local action = picker:GetInventoryActions(item)[1]
        assert(action.action ~= G.ACTIONS.DROP, "the native picker must not reinterpret equipment as a Ctrl drop")
        send(item)
    end
    function inv:EquipActionItem(item) assert(item == self.active); send(item) end
    function inv:TakeActiveItemFromEquipSlot(slot)
        assert(slot == "hands" and not self.active)
        send(self.hands)
        s.to_cursor = true
    end
    local make_item = s.item
    function s:item(prefab, count)
        local item = make_item(self, prefab, count, { maxsize = 40 })
        function item:CollectActions(_, _, actions)
            actions[#actions + 1] = inv.hands == self and G.ACTIONS.UNEQUIP or G.ACTIONS.EQUIP
        end
        item.replica.equippable = { EquipSlot = function() return "hands" end,
            ShouldPreventUnequipping = function() return item.prevent end }
        function item.replica.inventoryitem:CanGoInContainer() return not item.no_container end
        if count == nil then item.replica.stackable, item.components.stackable = nil, nil end
        return item
    end
    function s:deliver()
        local item, old = self.pending, inv.hands
        self.pending = nil
        if not item or self.reject then return end
        if self.to_cursor then
            inv.hands, inv.active, self.to_cursor = nil, item, nil
            return
        end
        for _, box in ipairs({ inv, pack }) do
            for slot, value in pairs(box.slots) do if value == item then box.slots[slot] = nil end end
        end
        if inv.active == item then inv.active = nil end
        if item == old then inv.hands = nil else inv.hands = item end
        if not old then return end
        for _, box in ipairs({ inv, inv.pack }) do
            for slot = 1, box.size do
                local value = box.slots[slot]
                if value and Fixtures.stackable(value, old) and value.count + old.count <= 40 then
                    value.count, old.removed = value.count + old.count, true
                    return
                end
            end
        end
        for _, box in ipairs({ inv, inv.pack }) do
            for slot = 1, box.size do if not box.slots[slot] then box.slots[slot] = old; return end end
        end
        error("equipment must never overflow into the cursor or ground")
    end
    ctx.equipment = Equipment.new(ctx)
    ctx.executor.current = {}
    return ctx, s, inv, pack
end

T.each("equipment transport", { "host", "remote" }, function(transport)
    T.each("source", { "inventory", "backpack", "cursor" }, function(source)
        local c, s, inv, pack = fixture(transport == "remote")
        local cane, axe = s:item("cane"), s:item("axe")
        inv.pack, inv.hands = pack, axe
        if source == "cursor" then inv.active = cane
        else (source == "inventory" and inv or pack).slots[1] = cane end
        local receipt = assert(c.equipment:equip(c.executor.current, cane))
        assert(receipt:state() == "pending" and s.requests == 1 and c.equipment:busy())
        s:deliver(); inv.busy = true
        assert(receipt:state() == "pending", "a predicted equipment slot does not acknowledge the swap")
        inv.busy = false
        assert(receipt:state() == "done" and inv.hands == cane and not c.equipment:busy())
        assert(c.player.components.playercontroller:EncodeControlMods() == 4,
            "ordinary raw Ctrl encoding is restored outside the task query")
        assert(c.equipment:equip(c.executor.current, axe)); s:deliver()
        assert(not c.equipment:busy() and inv.hands == axe)
    end)
end)

T.case("source disappearance and empty hand restoration", function()
    local c, s, inv = fixture()
    local cane = s:item("cane")
    inv.slots[1] = cane
    local receipt = assert(c.equipment:equip(c.executor.current, cane))
    inv.hands = cane
    assert(receipt:state() == "pending", "source-slot disappearance is part of the equipment receipt")
    inv.slots[1] = nil
    assert(receipt:state() == "done")
    s.pending = nil
    assert(c.equipment:unequip(c.executor.current, cane)); s:deliver()
    assert(not c.equipment:busy() and inv.hands == nil and inv.slots[1] == cane)
end)
T.case("replacement capacity and unknown stack size", function()
    local c, s, inv = fixture()
    local arrow, axe = s:item("blowdart_pipe", 1), s:item("axe")
    inv.hands, inv.active = arrow, axe
    for slot = 1, inv.size do inv.slots[slot] = s:item("log", 40) end
    assert(Inventory.capacity(c, inv, arrow) == 0, "the equipped item cannot receive itself")
    local receipt, why = c.equipment:equip(c.executor.current, axe)
    assert(not receipt and why == "full" and s.requests == 0, "a full bag cannot safely receive the cursor tool's replaced equipment")
    inv.slots[1] = s:item("blowdart_pipe", 39)
    arrow.count = 2
    assert(not c.equipment:equip(c.executor.current, axe), "capacity must cover the entire equipped stack")
    arrow.preview = 1
    local unavailable, reason = c.equipment:equip(c.executor.current, axe)
    assert(not unavailable and reason == "busy" and s.requests == 0, "unknown replaced-stack counts never become zero")
end)
T.case("native merge replaces old equipment GUID", function()
    local c, s, inv = fixture()
    local old, spare, axe = s:item("blowdart_pipe", 2), s:item("blowdart_pipe", 1), s:item("axe")
    inv.hands, inv.slots[1], inv.slots[2] = old, spare, axe
    local receipt = assert(c.equipment:equip(c.executor.current, axe))
    s:deliver()
    assert(old.removed and spare.count == 3 and receipt:state() == "done", "native stack merging preserves quantity despite the old GUID disappearing")
end)
T.each("native WX-78 equipment return", { "host", "remote" }, function(transport)
    local c, s, inv = fixture(transport == "remote")
    local env = { Class = T.Class, EQUIPSLOTS = c.G.EQUIPSLOTS, require = function() return {} end,
        table = setmetatable({ contains = function(values, value)
            for _, candidate in ipairs(values) do if candidate == value then return true end end
            return false
        end }, { __index = table }) }
    local NativeInventory = T.native("components/inventory", env)
    local old, spare, axe = s:item("blowdart_pipe", 2), s:item("blowdart_pipe", 1), s:item("axe")
    local pocket = s:container(T.entity({ prefab = "wx78_inventorycontainer" }), 1)
    pocket.slots[1], pocket.canbeopened = spare, true
    local priority = assert(T.source("containers"):match("(function params%.wx78_inventorycontainer%.priorityfn%b()%s*.-\nend)"))
    pocket.priorityfn = T.eval((priority:gsub("function params.wx78_inventorycontainer.priorityfn", "return function", 1)))
    spare.maxsize, spare.originalmaxsize = 65535, 40
    inv.hands, inv.slots[1], inv.slots[2], inv.slots[3] = old, s:item("log", 40), axe, pocket.inst
    old.prevslot = 1
    inv.inst, inv.maxslots, inv.itemslots, inv.equipslots = c.player, inv.size, inv.slots, { hands = old }
    function inv:GetEquippedItem(slot) return self.equipslots[slot or "hands"] end
    function inv:GetEquips() return self.equipslots end
    for _, method in ipairs({ "IsItemEquipped", "GetItemSlot", "RemoveItem", "Unequip",
        "GetSpecializedContainers", "GetNextAvailableSlot", "GiveItem" }) do
        inv[method] = NativeInventory[method]
    end
    for _, item in ipairs({ old, axe }) do
        item.components.equippable = {
            equipstack = item == old, equipslot = "hands",
            IsRestricted = function() return false end, ShouldPreventUnequipping = function() return false end,
            ToPocket = function() end, Unequip = function() end, Equip = function() end,
        }
        local invitem = item.components.inventoryitem
        invitem.owner, invitem.cangoincontainer = c.player, true
        function invitem:OnPickup() return false end
        function invitem:IsHeld() return true end
        function invitem:GetSlotNum() return inv:GetItemSlot(item) end
        function invitem:OnRemoved() self.owner = nil end
        function invitem:OnPutInInventory(owner) self.owner = owner end
        function invitem:RemoveFromOwner(whole, overstacked) return inv:RemoveItem(item, whole, nil, overstacked) end
    end
    assert(Inventory.count(c, old.prefab) == 2, "receipt destinations do not expand ordinary item sources")
    local receipt = assert(c.equipment:equip(c.executor.current, axe))
    s.pending = nil
    assert(NativeInventory.Equip(inv, axe))
    assert(spare.count == 3 and old.removed and inv:GetEquippedItem("hands") == axe,
        "the game's Equip/GiveItem moves the old darts into its native specialized receiver")
    spare.preview = 3
    assert(receipt:state() == "pending", "predicted destination quantity cannot acknowledge equipment")
    spare.preview, pocket.open = nil, false
    assert(receipt:state() == "pending", "a temporarily unreadable receiver cannot acknowledge equipment")
    pocket.open, spare.count = true, 2
    assert(receipt:state() == "pending", "equipping the tool alone does not prove the full replaced stack was stored")
    spare.count, inv.slots[2] = 3, axe
    assert(receipt:state() == "pending", "the source slot must also release the new tool")
    inv.slots[2], inv.slots[3] = nil, nil
    assert(receipt:state() == "pending", "a receiver outside personal storage cannot acknowledge ownership")
    inv.slots[3] = pocket.inst
    assert(receipt:state() == "done" and not c.equipment:busy(), "stable native pocket quantity completes the equipment receipt")
    old.removed, inv.equipslots.hands, inv.slots[2], pocket.open = false, old, axe, false
    local rejected, why = c.equipment:equip(c.executor.current, axe)
    assert(not rejected and why == "busy" and s.requests == 1, "unknown pocket contents cannot provide a quantity baseline")
end)
T.case("cancelled ownership across jobs", function()
    local c, s, inv = fixture()
    local cane, axe = s:item("cane"), s:item("axe")
    inv.slots[1], inv.hands = cane, axe
    local oldjob = c.executor.current
    local receipt = assert(c.equipment:equip(oldjob, cane))
    c.executor.current = {}
    assert(c.equipment:busy() and not c.equipment:ready(c.executor.current))
    assert(not c.equipment:equip(c.executor.current, axe) and s.requests == 1, "a cancelled in-flight request gates the next task")
    s:deliver()
    assert(receipt:state() == "done" and c.equipment:ready(c.executor.current) and inv.hands == cane and s.requests == 1,
        "settling a cancelled request never schedules a restoration")
end)
T.case("cleanup receipt survives equipment reattachment", function()
    local c, s, inv = fixture(true)
    local rod, axe = s:item("fishingrod"), s:item("axe")
    inv.hands, inv.slots[1] = rod, axe
    c.executor = require("fieldkit/core/executor").new(function() return s.time end,
        function() return not c.equipment:busy() end, function() end, error)
    local receipt
    local oldjob = { timeout = 10, step = function() return "running" end,
        cancel = function(job)
            assert(not c.equipment:unequip(nil, rod) and not c.equipment:unequip({}, rod),
                "cleanup admits only the job whose cancellation is running")
            receipt = assert(c.equipment:unequip(job, rod))
        end }
    local nextjob = { timeout = 10, step = function() return "running" end }
    assert(c.executor:start(oldjob))
    assert(not c.executor:start(nextjob) and not c.executor.current and s.requests == 1,
        "replacement must wait for the cleanup's native unequip to settle")
    local rebuilt = { player = c.player, G = c.G, executor = c.executor, text = c.text }
    c.equipment = Equipment.new(rebuilt)
    assert(c.equipment:busy() and not c.executor:start(nextjob) and s.requests == 1,
        "rebuilding the HUD cannot discard inventory requests from the old context")
    s:deliver()
    assert(receipt:state() == "done" and inv.hands == nil and c.executor:start(nextjob))
    assert(not c.equipment:busy() and inv._fieldkit_pending_equipment == nil and s.requests == 1)
    assert(not c.equipment:unequip(oldjob, rod), "finished jobs cannot regain equipment ownership")
end)
T.case("explicit cancellation can hold equipment when inventory is full", function()
    local c, s, inv = fixture(true)
    local rod = s:item("fishingrod")
    inv.hands = rod
    for slot = 1, inv.size do inv.slots[slot] = s:item("log", 40) end
    local denied, why = c.equipment:unequip(c.executor.current, rod)
    assert(not denied and why == "full" and s.requests == 0,
        "ordinary restoration cannot move an equipped item into the cursor")
    rod.locked = true
    assert(not c.equipment:unequip(c.executor.current, rod, true) and s.requests == 0)
    rod.locked = nil
    local receipt = assert(c.equipment:unequip(c.executor.current, rod, true))
    assert(receipt.cursor and receipt:state() == "pending" and s.requests == 1)
    s:deliver(); inv.busy = true
    assert(inv.hands == nil and inv.active == rod and receipt:state() == "pending")
    inv.busy = false
    assert(receipt:state() == "done" and not c.equipment:busy() and s.requests == 1,
        "hands and cursor must both settle before the full-inventory cancellation is acknowledged")
end)
T.case("guarded and unacknowledged equipment", function()
    local c, s, inv = fixture()
    local cane = s:item("cane")
    inv.slots[1] = cane
    cane.locked = true
    assert(not c.equipment:equip(c.executor.current, cane))
    cane.locked, inv.heavy = false, true
    assert(not c.equipment:equip(c.executor.current, cane) and s.requests == 0)
    inv.heavy, s.reject = false, true
    local receipt = assert(c.equipment:equip(c.executor.current, cane))
    s:deliver(); s.time = 7
    assert(receipt:state() == "timeout" and not c.equipment:busy() and s.requests == 1,
        "unacknowledged equipment has one bounded request and no automatic replay")
end)

T.done()
