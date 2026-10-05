local T = require("tests.client_mod.support")
local Input = require("fieldkit/game/input")
local Scope = require("fieldkit/core/scope")
local function pack(...) return { n = select("#", ...), ... } end

local function fixture()
    local state = { effects = {} }
    function state:record(effect) self.effects[#self.effects + 1] = effect end
    function state:expect(...)
        local expected = { ... }
        assert(#self.effects == #expected and table.concat(self.effects, ",") == table.concat(expected, ","),
            "expected [" .. table.concat(expected, ",") .. "], got [" .. table.concat(self.effects, ",") .. "]")
        self.effects = {}
    end
    local player = T.entity()
    local hud = { controls = { inv = {} } }
    player.HUD = hud
    local frontend = { GetActiveScreen = function(self) return self.screen end, screen = hud }
    function hud:HasInputFocus()
        return self.controls.inv.open or frontend.screen ~= self or frontend.textProcessorWidget ~= nil
            or self.modfocus or self.other_focus
    end
    function hud:IsCraftingOpen() return self.crafting end
    function hud:IsSpellWheelOpen() return false end
    function hud:IsCommandWheelOpen() return false end
    function hud:IsUpgradeModuleWidgetInputFocus() return false end
    function hud:OnBecomeInactive() state:record("inactive") end
    local controller = { enabled = true }
    controller.classified = { iscontrollerenabled = { value = function() return controller.enabled end } }
    function controller:IsEnabled() return self.enabled and not hud:HasInputFocus() end
    function controller:OnControl(...)
        state:record("control"); state.supplied = pack(...); return "native", nil, "tail"
    end
    for _, name in ipairs({ "DoControllerUseItemOnSelfFromInvTile", "DoControllerUseItemOnSceneFromInvTile",
        "DoControllerDropItemFromInvTile", "DoControllerInspectItemFromInvTile" }) do controller[name] = function() end end
    player.components.playercontroller = controller
    local inv = { GetActiveItem = function(self) return self.active end,
        classified = { IsBusy = function() return false end } }
    player.replica.inventory = inv
    local G = {
        ThePlayer = player, TheFrontEnd = frontend, TheWorld = T.events({}), CONTROL_PRIMARY = 1, CONTROL_MOVE_UP = 2,
        CONTROL_INV_1 = 15, CONTROL_INV_10 = 24, CONTROL_INV_11 = 77, CONTROL_INV_15 = 81,
        TheNet = { IsServerPaused = function() return false end },
        TheInput = {
            AddMouseButtonHandler = function(_, fn) state.mouse = fn; return { Remove = function() state.mouse = nil end } end,
            GetHUDEntityUnderMouse = function() return true end,
        },
    }
    local ctx = { G = G, player = player, hud = hud, scope = Scope.new(player, error) }
    function ctx:text(_, en) return en end
    ctx.executor = { cancel = function(self, reason) state:record("cancel:" .. reason); self.current = nil end }
    ctx.selection = { handle_control = function() return false end }
    ctx.minimap = {
        handle_control = function(self) return self.focused end,
        consumes_pointer = function(self) return self.pointer end,
    }
    ctx.equipment = { busy = function() return state.equipment_pending end }
    return ctx, state
end

T.case("readiness", function()
    local ctx, state = fixture()
    local G, hud = ctx.G, ctx.hud
    local frontend, inv, controller = G.TheFrontEnd, ctx.player.replica.inventory, ctx.player.components.playercontroller
    local world = { input = { mode = "world" } }
    local inventory = { input = { mode = "inventory", cursor = true, placement = true } }
    local function reset()
        inv.active, ctx.executor.current, state.equipment_pending = nil, nil, false
        frontend.screen, frontend.textProcessorWidget = hud, nil
        hud.controls.inv.open, hud.modfocus, hud.other_focus, hud.crafting = false, nil, nil, nil
        controller.enabled, controller.deployplacer, G.IsPaused = true, nil, nil
        world.input.cursor, world.input.placement = nil, nil
    end
    T.each("policy", {
        { name = "idle", allow = { true, true, true } },
        { name = "anonymous cursor", set = function() inv.active = {} end, allow = { false, true, true } },
        { name = "inventory focus", set = function() hud.controls.inv.open = true end, allow = { false, true, false } },
        { name = "controller disabled", set = function() controller.enabled = false; hud.controls.inv.open = true end },
        { name = "editor", set = function() frontend.textProcessorWidget = {} end },
        { name = "mod focus", set = function() hud.modfocus = {} end },
        { name = "crafting focus", set = function() hud.controls.inv.open = true; hud.crafting = true end },
        { name = "overlay", set = function() frontend.screen = {} end },
        { name = "unknown focus", set = function() hud.other_focus = true end },
        { name = "nonmodal crafting", set = function() hud.crafting = true end, allow = { true, true, true } },
        { name = "placer", set = function() controller.deployplacer = {}; inv.active = { prefab = "pinecone" } end,
            allow = { false, true, true } },
        { name = "matching cursor", set = function()
            controller.deployplacer, inv.active = {}, { prefab = "pinecone" }
            world.input.cursor, world.input.placement = "pinecone", true
        end, allow = { true, true, true } },
        { name = "different cursor", set = function() inv.active = { prefab = "log" }; world.input.cursor = "pinecone" end,
            allow = { false, true, true } },
        { name = "pause", set = function() G.IsPaused = function() return true end end },
        { name = "equipment owner", set = function() state.equipment_pending, ctx.executor.current = true, world end,
            allow = { true, false, true } },
        { name = "equipment after cancellation", set = function() state.equipment_pending = true end,
            allow = { false, false, true } },
    }, function(case)
        reset()
        if case.set then case.set() end
        for index, mode in ipairs({ world, inventory, "ui" }) do
            local allowed
            if type(mode) == "table" then allowed = Input.ready(ctx, nil, mode) else allowed = Input.ready(ctx, mode) end
            assert(allowed == (case.allow ~= nil and case.allow[index] == true), "policy " .. index)
        end
    end)
    ctx.scope:close()
end)

T.case("cancelled take blocks new jobs but allows its owner to wait", function()
    local ctx = fixture()
    local Inventory = require("fieldkit/game/inventory")
    local state = require("tests.client_mod/support/inventory").new()
    local inv = state:inventory(ctx.player, 4)
    inv.slots[1] = state:item("log", 8)
    local requests = 0
    ctx.G.GetTime = function() return state.now end
    ctx.G.RPC = { TakeActiveItemFromCountOfSlot = 42 }
    ctx.G.SendRPCToServer = function() requests = requests + 1 end
    local owner = { input = { mode = "inventory", cursor = true } }
    ctx.executor.current = owner
    assert(Inventory.take(ctx, inv, 1, 1) and requests == 1 and not inv.classified:IsBusy())
    assert(Input.ready(ctx, nil, owner), "the running task must be able to wait for replication")
    ctx.executor:cancel("manual")
    assert(not Input.ready(ctx, nil, { input = { mode = "world" } }),
        "a world task cannot race an abandoned inventory request")
    assert(not Input.ready(ctx, nil, owner), "a cancelled inventory task cannot restart before its receipt")
    assert(Input.ready(ctx, "ui"), "the toolbox remains available while inventory is synchronizing")
    inv:TakeActiveItemFromCountOfSlot(1, 1)
    state:deliver()
    assert(Input.ready(ctx, nil, owner) and requests == 1,
        "confirmed source and cursor counts release admission without replaying the old request")
    ctx.scope:close()
end)

local function native_input(ctx, state)
    local G, hud, player = ctx.G, ctx.hud, ctx.player
    local frontend, inv, controller = G.TheFrontEnd, player.replica.inventory, player.components.playercontroller
    -- Retain the native HUD -> Input consumption boundary instead of calling PlayerController directly.
    local base = { OnControl = function() return false end }
    local env = setmetatable({ require = function() return base end, ValidateLineNumber = function() end,
        Class = T.Class, }, { __index = function(_, key) if G[key] ~= nil then return G[key] end; return _G[key] end })
    env.IsNotConsole = function() return false end
    env.TheInputProxy = env.TheInputProxy or {}
    env.TheInputProxy.GetInputDeviceCount = function() return 0 end
    T.native("events", env)
    local function native(path) return T.native(path, env) end
    local NativeHUD, NativeInput = native("screens/playerhud.lua"), native("input.lua")
    env.TheInput = G.TheInput
    hud.owner, hud.shown = player, true
    hud.controls.status = { GetResurrectButton = function() end }
    hud.controls.item_notification = { CheckControl = function() return false end }
    hud.controls.skilltree_notification = hud.controls.item_notification
    function controller:ShouldPlayerHUDControlBeIgnored() return false end
    function inv:IsVisible() return true end
    function inv:GetItemInSlot(slot) return slot end
    function inv:UseItemFromInvTile(item) state:record("use:" .. item) end
    function hud:OnControl(control, down, ...)
        state:record("hud")
        state.supplied = pack(control, down, ...)
        return NativeHUD.OnControl(self, control, down), nil, "tail"
    end
    function frontend:OnControl(...) state:record("frontend"); return hud:OnControl(...) end
    G.TheInput.IsControlPressed = function() return false end
    G.TheInput.mouse_enabled = true
    G.TheInput.oncontrol = { HandleEvent = function() error("consumed inventory keys must not reach PlayerController") end }

    return NativeInput
end

-- Each cursor transition starts with its own job and an empty effect log.
T.each("cursor policy", {
    { name = "default", allowed = { true, false, false, false } },
    { name = "any", mode = "inventory", cursor = true, allowed = { true, true, true, true } },
    { name = "named", cursor = "pinecone", allowed = { true, true, false, false } },
}, function(policy)
    T.each("item", {
        { name = "empty" },
        { name = "matching", item = { prefab = "pinecone" } },
        { name = "different", item = { prefab = "log" } },
        { name = "anonymous", item = {} },
    }, function(case, index)
        local ctx, state = fixture()
        local job = { input = { mode = policy.mode or "world", cursor = policy.cursor } }
        ctx.executor.current, ctx.player.replica.inventory.active = job, case.item
        Input.attach(ctx)
        assert(Input.ready(ctx, nil, job) == policy.allowed[index])
        ctx.player:PushEvent("newactiveitem", { item = case.item })
        if policy.allowed[index] then
            state:expect()
            assert(ctx.executor.current == job)
        else
            state:expect("cancel:manual")
            assert(ctx.executor.current == nil)
        end
        ctx.scope:close()
    end)
end)

T.each("cursor event without", { "job", "data", "policy" }, function(missing)
    local ctx, state = fixture()
    if missing ~= "job" then ctx.executor.current = {} end
    Input.attach(ctx)
    local data = missing ~= "data" and { item = {} } or nil
    ctx.player:PushEvent("newactiveitem", data)
    if missing == "policy" then state:expect("cancel:manual") else state:expect() end
    ctx.scope:close()
end)

T.each("native inventory hotkey", { { name = "slot 1", control = 15, slot = 1 },
    { name = "slot 10", control = 24, slot = 10 }, { name = "slot 11", control = 77, slot = 11 },
    { name = "slot 15", control = 81, slot = 15 } }, function(case)
    local ctx, state = fixture()
    local NativeInput = native_input(ctx, state)
    ctx.executor.current = { input = { mode = "world", cursor = "pinecone", placement = true } }
    ctx.player.replica.inventory.active = { prefab = "log" }
    Input.attach(ctx)
    NativeInput.OnControl(ctx.G.TheInput, case.control, true)
    state:expect("frontend", "cancel:manual", "hud", "use:" .. case.slot)
    assert(ctx.executor.current == nil)
    ctx.scope:close()
end)

T.case("HUD arguments and return arity", function()
    local ctx, state = fixture()
    native_input(ctx, state)
    Input.attach(ctx)
    local result = pack(ctx.hud:OnControl(ctx.G.CONTROL_INV_1, true, "kept", nil, 42))
    state:expect("cancel:manual", "hud", "use:1")
    assert(result.n == 3 and result[1] == true and result[2] == nil and result[3] == "tail")
    assert(state.supplied.n == 5 and state.supplied[1] == ctx.G.CONTROL_INV_1 and state.supplied[2] == true
        and state.supplied[3] == "kept" and state.supplied[4] == nil and state.supplied[5] == 42)
    ctx.scope:close()
end)

T.each("HUD pointer", {
    { name = "inventory job", inventory = true }, { name = "world job" },
    { name = "minimap", minimap = true }, { name = "accepted gesture", inventory = true, owned = true },
}, function(case)
    local ctx, state = fixture()
    ctx.executor.current = { input = { mode = case.inventory and "inventory" or "world", cursor = case.inventory } }
    ctx.minimap.pointer = case.minimap
    Input.attach(ctx)
    if case.owned then Input.claim_pointer(ctx, 1, true) end
    state.mouse(1, true)
    if case.minimap or case.owned then state:expect() else state:expect("cancel:focus") end
    assert(ctx.pointer_event == nil)
    if case.owned then
        state.mouse(1, true)
        state:expect("cancel:focus")
    end
    ctx.scope:close()
end)

T.each("controller ownership", { "minimap", "movement" }, function(owner)
    local ctx, state = fixture()
    ctx.executor.current = { input = { mode = "world" } }
    ctx.minimap.focused = owner == "minimap"
    Input.attach(ctx)
    local control = ctx.minimap.focused and ctx.G.CONTROL_PRIMARY or ctx.G.CONTROL_MOVE_UP
    local result = pack(ctx.player.components.playercontroller:OnControl(control, true, "kept", nil, 42))
    if ctx.minimap.focused then
        state:expect()
        assert(result.n == 1 and result[1] == true)
    else
        state:expect("cancel:manual", "control")
        assert(result.n == 3 and result[1] == "native" and result[2] == nil and result[3] == "tail")
        assert(state.supplied.n == 5 and state.supplied[1] == control and state.supplied[2] == true
            and state.supplied[3] == "kept" and state.supplied[4] == nil and state.supplied[5] == 42)
    end
    ctx.scope:close()
end)

T.case("focus and cleanup", function()
    local ctx, state = fixture()
    local controller = ctx.player.components.playercontroller
    local inactive, control = ctx.hud.OnBecomeInactive, controller.OnControl
    ctx.executor.current = { input = { mode = "world" } }
    Input.attach(ctx)
    ctx.hud:OnBecomeInactive()
    state:expect("cancel:focus", "inactive")
    assert(ctx.executor.current == nil)
    ctx.scope:close()
    assert(state.mouse == nil and T.listener_count(ctx.player) == 0 and T.listener_count(ctx.G.TheWorld) == 0)
    assert(ctx.hud.OnBecomeInactive == inactive and controller.OnControl == control)
    ctx.executor.current = { input = { mode = "world" } }
    ctx.player:PushEvent("newactiveitem", { item = {} })
    state:expect()
    ctx.hud:OnBecomeInactive()
    state:expect("inactive")
end)
T.done()
