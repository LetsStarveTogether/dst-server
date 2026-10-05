local T = require("tests.client_mod.support")
local Actions = require("fieldkit/game/actions")
local function native(text, env, result) return T.eval(text .. "\nreturn " .. result, env) end

local G = { ACTIONS = { DROP = { id = "DROP", code = 7 }, WALKTO = {} }, RPC = { LeftClick = 1, RightClick = 2 },
    CONTROL_FORCE_INSPECT = 1, CONTROL_FORCE_ATTACK = 2, CONTROL_FORCE_TRADE = 3, CONTROL_FORCE_STACK = 4 }
G.PlayerController, G.PlayerActionPicker = {}, {}
local controller_source = T.source("components/playercontroller")
local decode = native(assert(controller_source:match("local MOD_CONTROLS =.-\n}")) .. "\n"
    .. assert(controller_source:match("function PlayerController:DecodeControlMods%b().-\nend")), G,
    "PlayerController.DecodeControlMods")
local point_actions = native(assert(T.source("components/playeractionpicker")
    :match("function PlayerActionPicker:GetPointActions%b().-\nend")), G, "PlayerActionPicker.GetPointActions")
local drop = native(assert(T.source("actions"):match("ACTIONS.DROP.fn = function%b().-\nend")), G, "ACTIONS.DROP.fn")
local server = { remote_controls = {} }
function server:IsControlPressed(control) return self.remote_controls[control] ~= nil end
local count, remaining = nil, 5
local inventory = { IsFloaterHeld = function() return false end }
function inventory:DropItem(_, whole)
    count = whole and remaining or 1
    remaining = remaining - count
    return true
end
local server_player = { components = { playercontroller = server, inventory = inventory } }
local item = { components = { inventoryitem = {}, stackable = {} } }
function item:CollectActions(_, _, _, list) list[1] = G.ACTIONS.DROP end
local picker = { inst = server_player }
function picker:SortActionList(list, point, object)
    return { { doer = server_player, invobject = object, action = list[1], options = {},
        GetActionPoint = function() return point end } }
end

local player = { components = {} }
function player:ListenForEvent() end
function player:RemoveEventCallback() end
local controller = { ismastersim = false, IsEnabled = function() return true end, IsBusy = function() return false end }
function controller:GetPlatformRelativePosition(x, z) return nil, x, z end
function controller:DoAction(action) self.action = action; action.non_preview_cb() end
player.components.playercontroller = controller
local sends = 0
function G.SendRPCToServer(rpc, code, x, z, target, released, modifiers)
    assert(rpc == G.RPC.LeftClick and code == G.ACTIONS.DROP.code and not target and released)
    sends = sends + 1
    decode(server, modifiers)
    local action = point_actions(picker, { x = x, z = z }, item, false)[1]
    assert(drop(action))
end
local ctx = { G = G, player = player, text = function(_, _, en) return en end }
local function request(whole)
    return { doer = player, action = G.ACTIONS.DROP, options = { wholestack = whole }, rotation = 0,
        IsValid = function() return true end, GetActionPoint = function() return { x = 4, z = 8 } end }
end
T.case("native drop RPC reconstruction", function()
    local ticket = assert(Actions.dispatch(ctx, request(false), "left"))
    assert(count == 1 and remaining == 4, "server reconstruction must preserve single-item drop intent")
    ticket:close()
    controller.action.non_preview_cb()
    assert(sends == 1, "a closed request cannot send again")
    ticket = assert(Actions.dispatch(ctx, request(true), "left"))
    assert(count == 4 and remaining == 0, "explicit whole-stack intent retains native semantics")
    ticket:close()
end)

T.each("attempt observation", { "identified", "anonymous", "removed target", "synchronous" }, function(kind)
    local actor, target, now = T.entity({ x = 2 }), T.entity({ x = 3 }), 1
    local pc = { ismastersim = false, IsEnabled = function() return true end, IsBusy = function() return false end }
    function pc:DoAction(action)
        self.action = action
        if kind == "synchronous" then action.non_preview_cb(); actor:PushEvent("performaction") end
    end
    function pc:GetPlatformRelativePosition(x, z) return nil, x, z end
    actor.components.playercontroller = pc
    local game = { ACTIONS = G.ACTIONS, RPC = G.RPC, GetTime = function() return now end, SendRPCToServer = function() end }
    local action = { doer = actor, target = target, action = { id = "PICK", code = 4 }, rotation = 0,
        IsValid = function() return true end, GetActionPoint = function() return target:GetPosition() end }
    local ticket = assert(Actions.dispatch({ player = actor, G = game }, action))
    if kind ~= "synchronous" then
        actor:PushEvent("performaction")
        assert(not ticket.attempt, "an unsent action has no observed attempt")
        action.non_preview_cb()
        actor:PushEvent("performaction", { action = {} })
        assert(not ticket.attempt, "identified foreign events cannot replace observations")
        if kind == "removed target" then target:Remove() end
        actor:PushEvent("performaction", kind == "identified" and { action = action } or nil)
    end
    local observed = assert(ticket.attempt, "retain the event's time and position")
    assert(observed.at == 1 and observed.position.x == 2 and observed.identified == (kind == "identified"))
    assert(kind == "removed target" and observed.target_position == nil or observed.target_position.x == 3)
    assert(ticket:performed() == (kind == "identified"), "an anonymous distant echo does not identify execution")
    actor.x, target.x, now = 20, 30, 5
    assert(observed.position.x == 2 and (not observed.target_position or observed.target_position.x == 3),
        "later movement cannot relocate an earlier attempt")
    ticket:close()
    actor:PushEvent("performaction"); action.non_preview_cb()
    assert(ticket.attempt == observed and T.listener_count(actor) == 0, "closed tickets cannot receive late observations")
end)

T.case("execution evidence respects native range and failures", function()
    local actor, target = T.entity(), T.entity({ x = 4 })
    local pc = { ismastersim = false, IsEnabled = function() return true end, IsBusy = function() return false end }
    function pc:DoAction(action) action.non_preview_cb() end
    function pc:GetPlatformRelativePosition(x, z) return nil, x, z end
    actor.components.playercontroller = pc
    local game = { ACTIONS = G.ACTIONS, RPC = G.RPC, GetTime = function() return 0 end, SendRPCToServer = function() end }
    local action = { doer = actor, target = target, action = { code = 4, distance = 2 }, rotation = 0,
        IsValid = function() return true end, GetActionPoint = function() return target:GetPosition() end }
    local ticket = assert(Actions.dispatch({ player = actor, G = game }, action))
    assert(not ticket:performed(), "sending the request provides no execution evidence")
    actor:PushEvent("performaction")
    actor.x = 4
    assert(not ticket:performed(), "later arrival does not relocate a distant anonymous echo")
    actor:PushEvent("performaction")
    assert(ticket:performed(), "native execution within action range can be paired with a result")
    actor:PushEvent("actionfailed", { action = action })
    assert(not ticket:performed(), "an identified rejection invalidates earlier execution hints")
    ticket:close()

    action.action.rangecheckfn = function() return false end
    ticket = assert(Actions.dispatch({ player = actor, G = game }, action))
    actor:PushEvent("performaction")
    assert(not ticket:performed(), "custom native range predicates take priority over collision distance")
    ticket:close()
end)

T.case("arrival matches native entity movement", function()
    local actor, target, owner = T.entity(), T.entity({ x = 10 }), T.entity()
    actor.Physics, target.Physics, owner.Physics = { GetRadius = function() return .5 end },
        { GetRadius = function() return .2 end }, { GetRadius = function() return 1.2 end }
    function target:GetPhysicsRadius() return 3 end
    local env = { LocoMotor = {}, ARRIVE_STEP = .15, Dest = function(entity)
        return { GetPoint = function() return entity.Transform:GetWorldPosition() end }
    end }
    local move = native(assert(T.source("components/locomotor"):match("function LocoMotor:GoToEntity%b().-\nend")),
        env, "LocoMotor.GoToEntity")
    local loco = { inst = actor }
    for _, method in ipairs({ "CancelPredictMoveTimer", "SetBufferedAction", "FindPath", "StartUpdatingInternal", "OnUpdate" }) do
        loco[method] = function() end
    end
    for _, action in ipairs({
        { action = {} },
        { action = {}, arrivedist = 7 },
        { action = {}, distance = .1 },
        { action = {}, distance = 5 },
        { action = { mindistance = 9 } },
        { action = { extra_arrive_dist = function(_, destination, _, distance)
            local x = destination:GetPoint()
            return x + distance
        end } },
    }) do
        action.target = target
        for _, stored in ipairs({ false, true }) do
            target.components.inventoryitem = stored and { GetGrandOwner = function() return owner end } or nil
            move(loco, target, action)
            assert(math.abs(Actions.arrival({ player = actor }, action) - loco.arrive_dist) < 1e-10,
                "shared execution range follows native movement, physics overrides and inventory ownership")
        end
    end
end)

-- Old RPC cleanup cannot replace a new buffered action, with or without prediction.
local function stop_context(predicting)
    local state, old, fresh = {}, {}, {}
    local actor = { bufferedaction = old }
    function actor:IsValid() return true end
    function actor:GetPosition() return { x = 0, z = 0 } end
    function actor:ClearBufferedAction() self.bufferedaction = nil; state.cleared = true end
    local pc = { ismastersim = false }
    function pc:ClearActionHold() state.hold = true end
    function pc:RemoteStopWalking() state.stopped = true end
    function pc:GetPlatformRelativePosition(x, z) return nil, x, z end
    if predicting then
        pc.locomotor = { bufferedaction = old, Stop = function() state.local_stop = true end }
        function pc.locomotor:Clear() self.bufferedaction = nil end
    end
    local receipt = { action = old, controller = pc, sent = true, close = function() state.closed = true end }
    local game = { ACTIONS = { WALKTO = { code = 1 } }, RPC = { SetMovementPredictionEnabled = 1 },
        SendRPCToServer = function(rpc, enabled)
            assert(rpc == 1 and enabled == false, "cancellation reasserts the existing non-predicting mode")
            state.sent = true
        end }
    return { G = game, player = actor }, receipt, fresh, state
end
T.each("cancellation prediction", { false, true }, function(predicting)
    T.each("ownership", {
        { name = "cancelled", reason = "cancelled", clear = true, stop = true, local_stop = true },
        { name = "focus", reason = "focus", clear = true, stop = true, local_stop = true },
        { name = "replaced", reason = "replaced", clear = true, stop = true, local_stop = true },
        { name = "manual", reason = "manual", clear = true, local_stop = true },
        { name = "invalid", reason = "invalid", clear = true, local_stop = true },
        { name = "completed", reason = "done" },
        { name = "new action", reason = "cancelled", fresh = true, local_stop = true },
    }, function(row)
        local c, receipt, fresh, state = stop_context(predicting)
        if row.fresh then c.player.bufferedaction = fresh end
        local original = c.player.bufferedaction
        Actions.stop(c, receipt, row.reason)
        assert(state.closed and (state.cleared == true) == (row.clear == true))
        assert((state.stopped == true) == (row.stop == true))
        assert((state.sent == true) == (row.stop == true and not predicting))
        assert((state.local_stop == true) == (predicting and row.local_stop == true))
        assert(c.player.bufferedaction == (not row.clear and original or nil))
    end)
end)

T.each("native non-predicting cancellation", { "cursor item", "boat" }, function(kind)
    local actor, remote = T.entity(), T.entity()
    local old, held = { Fail = function() end }, T.entity({ prefab = "cutgrass" })
    local env = { PlayerController = {}, LocoMotor = {}, CONTROL_PRIMARY = 1,
        ACTIONS = { WALKTO = { code = 1 }, LOOKAT = {}, DROP = G.ACTIONS.DROP },
        SetClientRequestedAction = function() end, ClearClientRequestedAction = function() end,
        CanEntitySeeTarget = function() return true end }
    local function method(path, class, name)
        return native(assert(T.source(path):match("function " .. class .. ":" .. name .. "%b().-\nend")), env, class .. "." .. name)
    end
    local pc = { inst = remote, ismastersim = true, remote_predicting = false,
        remote_controls = { [1] = 0 }, remote_vector = { y = 2 } }
    function pc:IsEnabled() return true end
    function pc:ClearRemotePredictData() end
    function pc:DecodeControlMods() end
    function pc:ClearControlMods() end
    pc.ResetRemoteController = method("components/playercontroller", "PlayerController", "ResetRemoteController")
    pc.OnRemoteToggleMovementPrediction = method("components/playercontroller", "PlayerController", "OnRemoteToggleMovementPrediction")
    pc.OnRemoteLeftClick = method("components/playercontroller", "PlayerController", "OnRemoteLeftClick")
    local loco = { inst = remote, bufferedaction = old, dest = { x = 20 }, wantstomoveforward = true,
        allow_platform_hopping = true }
    function loco:Stop() self.stopped = true end
    function loco:SetAllowPlatformHopping(value) self.allow_platform_hopping = value end
    loco.SetBufferedAction = method("components/locomotor", "LocoMotor", "SetBufferedAction")
    loco.Clear = method("components/locomotor", "LocoMotor", "Clear")
    pc.locomotor = loco
    remote.components.combat = { SetTarget = function() end }
    remote.components.playeractionpicker = {
        DoGetMouseActions = function() return { action = env.ACTIONS.DROP } end,
        GetDoubleClickActions = function() return {} end,
    }
    local controls_changed = 0
    function remote:ListenForEvent() controls_changed = controls_changed + 1 end
    function remote:RemoveEventCallback() controls_changed = controls_changed + 1 end
    if kind == "boat" then remote.platform, actor.platform = {}, {} end
    actor.replica.inventory = { GetActiveItem = function() return held end }
    local localpc = { ismastersim = false, ClearActionHold = function() end, RemoteStopWalking = function() end }
    function localpc:GetPlatformRelativePosition(x, z) return actor.platform, x, z end
    local game = { ACTIONS = env.ACTIONS, RPC = { LeftClick = 1, SetMovementPredictionEnabled = 2 } }
    function game.SendRPCToServer(rpc, value, x, z, target, released, modifiers)
        if rpc == game.RPC.LeftClick then
            -- Native reconstruction rejects WALKTO while the cursor's DROP is the left action.
            pc:OnRemoteLeftClick(value, { x = x, z = z }, target, released, modifiers)
        else
            assert(rpc == game.RPC.SetMovementPredictionEnabled and value == false)
            pc:OnRemoteToggleMovementPrediction(value)
        end
    end
    local receipt = { action = {}, controller = localpc, sent = true, close = function() end }
    Actions.stop({ player = actor, G = game }, receipt, "cancelled")
    assert(loco.stopped and loco.bufferedaction == nil and loco.dest == nil and not loco.wantstomoveforward,
        "cancelling before arrival removes the server action and its path")
    assert(pc.remote_vector.y == 0 and next(pc.remote_controls) == nil)
    assert(not pc.remote_predicting and loco.allow_platform_hopping and controls_changed == 0,
        "reasserting the existing mode preserves prediction listeners and boat hopping")
    assert(actor.replica.inventory:GetActiveItem() == held, "cancellation preserves the cursor item")
end)
T.case("late action and manual cancellation", function()
    local c, receipt, fresh, state = stop_context(true)
    receipt.controller.locomotor.bufferedaction = fresh
    Actions.stop(c, receipt, "cancelled")
    assert(state.closed and not state.stopped and not state.sent and state.cleared and not state.local_stop)
    assert(receipt.controller.locomotor.bufferedaction == fresh)
    c, receipt, _, state = stop_context(false)
    c.player.bufferedaction = nil
    Actions.stop(c, receipt, "cancelled")
    assert(state.stopped and state.sent, "an old in-flight server click still needs cancellation")
    c, receipt, _, state = stop_context(false)
    Actions.stop(c, receipt, "manual")
    assert(state.closed and state.cleared and not state.stopped and not state.sent)
end)

-- Query guards work without selection or placement features and preserve manual controls.
T.case("nested query guards and native picker", function()
    local pc = {
        IsControlPressed = function(_, control) return control <= 4, nil, "native" end,
        IsAxisAlignedPlacement = function() return true, nil, "native" end,
    }
    local actor = { components = { playercontroller = pc } }
    local scope = require("fieldkit/core/scope").new(actor, error)
    local c = { G = G, player = actor, scope = scope }
    local original_axis = pc.IsAxisAlignedPlacement
    Actions.attach(c)
    local function manual()
        for control = 1, 4 do assert(pc:IsControlPressed(control)) end
        local axis, middle, tail = pc:IsAxisAlignedPlacement()
        assert(axis and middle == nil and tail == "native")
        local pressed
        pressed, middle, tail = pc:IsControlPressed(1)
        assert(pressed and middle == nil and tail == "native")
    end
    local function guarded()
        for control = 1, 4 do assert(not pc:IsControlPressed(control)) end
        assert(not pc:IsAxisAlignedPlacement(), "queue points cannot be replaced by the mouse placer")
        local pressed, middle, tail = pc:IsControlPressed(99)
        assert(pressed == false and middle == nil and tail == "native", "unrelated controls retain native returns")
    end
    manual()
    local marker = {}
    local result = Actions.query(c, function()
        guarded()
        assert(Actions.query(c, function() guarded(); return marker end) == marker)
        guarded()
        local ok, err = pcall(Actions.query, c, function() guarded(); error(marker) end)
        assert(not ok and err == marker, "nested errors preserve the original error")
        guarded()
        return marker
    end)
    assert(result == marker and c._queue_query == nil)
    local ok, err = pcall(Actions.query, c, function() error(marker) end)
    assert(not ok and err == marker and c._queue_query == nil)
    manual()

    -- The native picker observes the guard even when all force modifiers are held.
    local pick = { inst = actor, GetPointActions = point_actions, SortActionList = picker.SortActionList }
    actor.components.playeractionpicker = pick
    local point = { x = 4, z = 8 }
    assert(not pick:GetPointActions(point, item, false)[1].options.wholestack)
    assert(Actions.candidates(c, nil, "left", item, point)[1].options.wholestack)
    manual()

    local wrapped = pc.IsControlPressed
    pc.IsControlPressed = function(self, ...) return wrapped(self, ...) end
    local later = pc.IsControlPressed
    scope:close()
    assert(pc.IsControlPressed == later and pc.IsAxisAlignedPlacement == original_axis)
    Actions.query(c, manual)
end)
T.case("native deployment query uses planned rotation", function()
    local pc = { IsAxisAlignedPlacement = function() return false end, IsControlPressed = function() return false end }
    local actor = { components = { playercontroller = pc } }
    local env = { ACTIONS = { DEPLOY = { id = "DEPLOY" }, DEPLOY_TILEARRIVE = {}, DEPLOY_FLOATING = {} },
        EntityScript = {}, require = function() end,
        orderedPairs = pairs }
    T.native("componentactions", env)
    local seed = T.entity({ actioncomponents = {} })
    for method, fn in pairs(env.EntityScript) do seed[method] = fn end
    seed:RegisterComponentActions("deployable")
    function seed:HasAnyTag() return false end
    local seen, raises
    seed.replica.inventoryitem = { CanDeploy = function(_, _, _, deployer, rotation)
        assert(deployer == actor)
        seen = rotation
        if raises then error("placement query failed") end
        return rotation == 135
    end }
    local pick = { inst = actor, GetPointActions = point_actions }
    function pick:SortActionList(list, position, invobject)
        local result = {}
        for _, definition in ipairs(list) do result[#result + 1] = { action = definition, invobject = invobject, pos = position } end
        return result
    end
    actor.components.playeractionpicker = pick
    local c, position = { player = actor }, { x = 1, z = 2 }
    local original = { Transform = { GetRotation = function() return 45 end } }
    for _, with_preview in ipairs({ false, true }) do
        pc.deployplacer = with_preview and original or nil
        assert(#Actions.candidates(c, nil, "right", seed, position) == 0)
        local list = Actions.candidates(c, nil, "right", seed, position, 135)
        assert(#list == 1 and list[1].action == env.ACTIONS.DEPLOY and seen == 135)
        assert(pc.deployplacer == (with_preview and original or nil) and c._queue_query == nil)
        raises = true
        assert(not pcall(Actions.candidates, c, nil, "right", seed, position, 135))
        assert(pc.deployplacer == (with_preview and original or nil) and c._queue_query == nil,
            "native query failure restores the real preview and input context")
        raises = false
    end
end)
T.done()
