local MapInput = require("fieldkit/game/map_input")
local Inventory = require("fieldkit/game/inventory")
local M = {}

-- FrontEnd owns the intent; the subsequent raw broadcast must not cancel that same input.
function M.claim_pointer(ctx, button, down)
    ctx.pointer_event = { button = button, down = down }
end

local function cursor_allowed(policy, item)
    return not item or policy.cursor == true or type(policy.cursor) == "string" and policy.cursor == item.prefab
end

function M.ready(ctx, mode, job)
    local G, player, hud = ctx.G, ctx.player, ctx.hud
    mode = mode or job and job.input and job.input.mode or "world"
    if ctx.scope.closed or G.ThePlayer ~= player or not player:IsValid() or player.HUD ~= hud
        or player:HasTag("playerghost") or player.replica.health and player.replica.health:IsDead() then
        return false, ctx:text("角色当前不可用", "Player is not available")
    end
    local controller = player.components.playercontroller
    local allowed = controller and controller:IsEnabled()
    local editing = job ~= nil and MapInput.editing(ctx, job)
    if editing then
        allowed = controller and controller.classified and controller.classified.iscontrollerenabled:value()
    elseif mode == "inventory" then
        local frontend = G.TheFrontEnd
        local screen = frontend:GetActiveScreen()
        local inventory_focus = hud.controls and hud.controls.inv and hud.controls.inv.open
        local focus = hud:HasInputFocus()
        local classified = controller and controller.classified
        allowed = controller and (allowed or classified and classified.iscontrollerenabled:value())
            and (screen == nil or screen == hud) and frontend.textProcessorWidget == nil and hud.modfocus == nil
            and (not focus or inventory_focus)
            and not (focus and (hud:IsCraftingOpen() or hud:IsSpellWheelOpen() or hud:IsCommandWheelOpen()
                or hud:IsUpgradeModuleWidgetInputFocus()))
    else
        allowed = allowed and not hud:HasInputFocus()
    end
    if not allowed or G.IsPaused and G.IsPaused() or mode ~= "ui" and G.TheNet:IsServerPaused(editing) then
        return false, ctx:text("界面或暂停状态阻止了操作", "Another interface or pause prevents this operation")
    end
    if job then
        local policy, inventory = job.input or {}, player.replica.inventory
        if not inventory or not cursor_allowed(policy, inventory:GetActiveItem())
            or (controller.placer or controller.deployplacer) and not policy.placement then
            return false, ctx:text("鼠标物品或放置状态与操作不符", "Cursor item or placement no longer matches this operation")
        end
        if ctx.executor.current ~= job and Inventory.busy(ctx, inventory) then
            return false, ctx:text("库存仍在同步，请稍后重试", "Inventory is still synchronizing; try again shortly")
        end
        if ctx.equipment and ctx.executor.current ~= job and ctx.equipment:busy() then
            return false, ctx:text("装备仍在同步，请稍后重试", "Equipment is still synchronizing; try again shortly")
        end
    end
    return true
end

function M.attach(ctx)
    local G, player, scope = ctx.G, ctx.player, ctx.scope
    local function cancel(reason) ctx.executor:cancel(reason) end
    scope:listen("death", function() cancel("invalid") end)
    scope:listen("newactiveitem", function(_, data)
        local job = ctx.executor.current
        if job and data and not cursor_allowed(job.input or {}, data.item) then cancel("manual") end
    end)
    scope:listen("serverpauseddirty", function(_, data)
        if data and (data.pause or (data.autopause or data.gameautopause)
            and not MapInput.editing(ctx, ctx.executor.current)) then cancel("focus") end
    end, G.TheWorld)
    scope:wrap(ctx.hud, "OnBecomeInactive", function(original)
        return function(...)
            if not MapInput.editing(ctx, ctx.executor.current) then cancel("focus") end
            return original(...)
        end
    end)
    scope:wrap(ctx.hud, "OnControl", function(original)
        return function(hud, control, down, ...)
            if ctx.ground_dock and ctx.ground_dock:handle_control(control, down) then return true end
            -- The HUD consumes number keys before they can reach PlayerController.
            if down and (G.CONTROL_INV_1 and control >= G.CONTROL_INV_1 and control <= G.CONTROL_INV_10
                or G.CONTROL_INV_11 and control >= G.CONTROL_INV_11 and control <= G.CONTROL_INV_15) then
                cancel("manual")
            end
            return original(hud, control, down, ...)
        end
    end)
    local controls = {}
    for _, name in ipairs({ "CONTROL_MOVE_UP", "CONTROL_MOVE_DOWN", "CONTROL_MOVE_LEFT", "CONTROL_MOVE_RIGHT",
        "CONTROL_PRIMARY", "CONTROL_SECONDARY", "CONTROL_ACTION", "CONTROL_ATTACK", "CONTROL_CONTROLLER_ACTION",
        "CONTROL_CONTROLLER_ALTACTION", "CONTROL_CONTROLLER_ATTACK", "CONTROL_CANCEL", "CONTROL_INSPECT" }) do
        if G[name] then controls[G[name]] = true end
    end
    scope:wrap(player.components.playercontroller, "OnControl", function(original)
        return function(controller, control, down, ...)
            if ctx.ground_dock and ctx.ground_dock:handle_control(control, down) then return true end
            if ctx.minimap and ctx.minimap:handle_control(control, down) then return true end
            if ctx.selection:handle_control(controller, control, down, ...) then return true end
            if down and controls[control] then cancel("manual") end
            return original(controller, control, down, ...)
        end
    end)
    for _, method in ipairs({ "DoControllerUseItemOnSelfFromInvTile", "DoControllerUseItemOnSceneFromInvTile",
        "DoControllerDropItemFromInvTile", "DoControllerInspectItemFromInvTile", "OnMapAction" }) do
        scope:wrap(player.components.playercontroller, method, function(original)
            return function(...) cancel("manual"); return original(...) end
        end)
    end
    local mouse = G.TheInput:AddMouseButtonHandler(function(button, down)
        -- FrontEnd handles this same raw event first; only an accepted widget gesture owns it.
        local owned = ctx.pointer_event
        ctx.pointer_event = nil
        if owned and owned.button == button and owned.down == down then return end
        local job = ctx.executor.current
        if not down or not job then return end
        if MapInput.editing(ctx, job) or ctx.minimap and ctx.minimap:consumes_pointer()
            or ctx.ground_dock and ctx.ground_dock:consumes_pointer() then return end
        if G.TheInput:GetHUDEntityUnderMouse() then cancel("focus") end
    end)
    scope:defer(function() mouse:Remove() end)
end

return M
