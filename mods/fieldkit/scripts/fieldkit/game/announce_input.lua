local Metrics = require("fieldkit/game/metrics")
local M = {}

function M.intent(ctx)
    local input, controls = ctx.G.TheInput, ctx.hud.controls
    local entity = input:GetHUDEntityUnderMouse()
    local widget = entity and entity.widget
    local crafting = controls.craftingmenu
    local recipe, in_crafting
    while widget do
        local field = widget.fieldkit_announcement
        if field == "world" or field == "nightmare" or field == "ride" or field == "status"
            or field == "moon" or field == "world_temperature" or field == "naughtiness" then return field end
        if Metrics.definition(field) then return "metric", field end
        local metric = Metrics.widget(ctx, widget)
        if metric then return "metric", metric end
        if widget == controls.clock then return "world" end
        if widget.owner == ctx.player then
            if widget.fieldkit_ingredient then return "ingredient", widget.fieldkit_ingredient end
            local panel = ctx.hud.upgrademodulewidget
            local pool = panel and panel.chip_objectpools and panel.chip_objectpools[widget.moduletype]
            if widget.parent == panel and pool and pool[widget.chip_index] == widget then
                return "circuit", { bar = widget.moduletype, index = widget.chip_index,
                    netid = not panel:IsBusy() and widget._net_id or nil }
            end
            if widget.container and widget.num then return "item", widget.container:GetItemInSlot(widget.num) end
            if widget.equipslot then return "item", ctx.player.replica.inventory:GetEquippedItem(widget.equipslot) end
        end
        recipe = recipe or widget.recipe_name or widget.data and widget.data.recipe or widget.recipe
        if widget == crafting or crafting and widget == crafting.pinbar then in_crafting = true end
        widget = widget.parent
    end
    if recipe and in_crafting then return "recipe", recipe end
    if not entity and input:IsKeyDown(ctx.G.KEY_SHIFT) then
        local target = input:GetWorldEntityUnderMouse()
        if target then return "target", target end
        return "position"
    end
end

function M.new(ctx)
    local self, G = {}, ctx.G
    function self:press(button)
        if button ~= G.MOUSEBUTTON_LEFT then return false end
        if G.TheInput:ControllerAttached() or not G.TheInput:IsKeyDown(G.KEY_ALT)
            or G.TheFrontEnd:GetActiveScreen() ~= ctx.hud or G.TheFrontEnd.textProcessorWidget ~= nil
            or ctx.hud.modfocus ~= nil or not ctx.config.announcements then return false end
        local method, argument = M.intent(ctx)
        if not method then return false end
        -- A recognized announcement owns its gesture even when chat is on cooldown.
        -- Falling back here could unequip an item or craft a recipe on the same click.
        local sent, why = ctx.announcements[method](ctx.announcements, argument)
        if not sent and why then ctx:notify(why) end
        return true
    end
    return self
end

return M
