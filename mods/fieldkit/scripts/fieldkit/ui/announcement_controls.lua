local Scope = require("fieldkit/core/scope")
local Data = require("fieldkit/game/announcement_data")
local M = {}

local function scope_for(ctx, widget)
    if not ctx.config.announcements or ctx.scope.closed then return end
    if not ctx.announcement_widgets then
        ctx.announcement_widgets = {}
        ctx.scope:defer(function()
            for _, scope in pairs(ctx.announcement_widgets) do scope:close() end
            ctx.announcement_widgets = {}
        end)
    end
    local widgets = ctx.announcement_widgets
    if widgets[widget] then return end
    local scope = Scope.new(widget.inst, function(err) ctx:report(err) end)
    widgets[widget] = scope
    scope:listen("onremove", function()
        widgets[widget] = nil
        scope:close()
    end)
    return scope
end

local function ingredients(ctx, widget)
    if widget.owner ~= ctx.player then return end
    local scope = scope_for(ctx, widget)
    if not scope then return end
    local function stamp()
        local recipe, position = widget.recipe, 0
        if not recipe then return end
        -- Icon recipe_type can be a skill's forwarded recipe, not the consumed ingredient.
        for _, category in ipairs({ "tech", "item", "character" }) do
            local entries = category == "item" and recipe.ingredients or recipe[category .. "_ingredients"]
            for index, entry in ipairs(entries) do
                if category ~= "tech" or entry.type:sub(-9) == "_material" then
                    position = position + 1
                    local icon = widget.ingredient_widgets[position]
                    if icon then icon.fieldkit_ingredient = { recipe = recipe.name, category = category, index = index } end
                end
            end
        end
    end
    scope:wrap(widget, "SetRecipe", function(original)
        return function(self, ...)
            original(self, ...)
            stamp()
        end
    end)
    scope:defer(function()
        for _, icon in ipairs(widget.ingredient_widgets) do icon.fieldkit_ingredient = nil end
    end)
    stamp()
end

local function cookbook(ctx, screen)
    if screen.owner ~= ctx.player then return end
    local scope = scope_for(ctx, screen)
    if not scope then return end
    screen.fieldkit_announcement_context = ctx
    scope:defer(function() screen.fieldkit_announcement_context = nil end)
end

local function food_page(ctx, page)
    if page.parent_screen ~= ctx.player then return end
    local scope = scope_for(ctx, page)
    if not scope then return end
    local G, target = ctx.G
    local button = page:AddChild(require("widgets/textbutton")())
    button:SetText(ctx:text("宣告食物效果", "Announce food effects"))
    button:SetTextSize(22)
    button:SetTextColour(G.UICOLOURS.BROWN_DARK)
    button:SetTextFocusColour(G.UICOLOURS.GOLD_FOCUS)
    button:SetPosition(page.details_root:GetPosition().x, -220)
    button:SetOnClick(function()
        local screen = page.parent
        while screen and screen.fieldkit_announcement_context ~= ctx do screen = screen.parent end
        if screen and target then
            local ok, why = ctx.announcements:food(target, screen)
            if not ok and why then ctx:notify(why) end
        end
    end)
    local grid, old_right = page.recipe_grid, page.recipe_grid.focus_flow[G.MOVE_RIGHT]
    local old_args, old_tab = grid.focus_flow_args[G.MOVE_RIGHT], grid.next_in_tab_order
    local destination = function() return target and button or nil end
    grid:SetFocusChangeDir(G.MOVE_RIGHT, destination)
    for _, direction in ipairs({ G.MOVE_LEFT, G.MOVE_UP, G.MOVE_DOWN }) do button:SetFocusChangeDir(direction, grid) end
    scope:defer(function()
        if grid.focus_flow[G.MOVE_RIGHT] == destination then
            grid.focus_flow[G.MOVE_RIGHT], grid.focus_flow_args[G.MOVE_RIGHT] = old_right, old_args
        end
        if grid.next_in_tab_order == destination then grid.next_in_tab_order = old_tab end
        if button.inst:IsValid() then
            if button.focus then button:ClearFocus(); grid:SetFocus() end
            button:Kill()
        end
    end)
    local function update(data)
        target = data and data.unlocked and data.has_eaten and Data.food(ctx, page.category, data.prefab)
            and { category = page.category, prefab = data.prefab } or nil
        if target then button:Show()
        else
            if button.focus then button:ClearFocus(); grid:SetFocus() end
            button:Hide()
        end
    end
    scope:wrap(page, "PopulateRecipeDetailPanel", function(original)
        return function(self, data)
            local panel = original(self, data)
            update(data)
            return panel
        end
    end)
    local selected = G.TheCookbook.selected
    update(page.all_recipes[selected and selected[page.category] or 1])
end

function M.install(env, get_context)
    for class, bind in pairs({
        ["widgets/redux/craftingmenu_ingredients"] = ingredients,
        ["screens/cookbookpopupscreen"] = cookbook,
        ["widgets/redux/cookbookpage_crockpot"] = food_page,
    }) do
        env.AddClassPostConstruct(class, function(widget)
            local ctx = get_context()
            if ctx then bind(ctx, widget) end
        end)
    end
end

function M.attach(ctx)
    local function visit(widget)
        if widget.owner == ctx.player and widget.ingredient_widgets and widget.SetRecipe then ingredients(ctx, widget) end
        for child in pairs(widget.children or {}) do visit(child) end
    end
    visit(ctx.hud)
end

return M
