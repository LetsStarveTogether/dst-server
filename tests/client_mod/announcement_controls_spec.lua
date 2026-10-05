local T = require("tests.client_mod.support")
T.case("native announcement controls", function()
    local Scope = require("fieldkit/core/scope")
    local Class = T.Class
    local env = setmetatable({ Class = Class, ValidateLineNumber = function() end,
        FunctionOrValue = function(value, ...) if type(value) == "function" then return value(...) end; return value end,
        toarrayornil = function(...) if select("#", ...) > 0 then return { ... } end end,
    }, { __index = _G })
    local function native(path) return T.native(path, env) end
    local NativeWidget = native("widgets/widget")
    local Base = Class(NativeWidget)
    function Base._ctor(self, label)
        self.children, self.focus_flow, self.focus_flow_args, self.name = {}, {}, {}, label
        self.shown, self.enabled, self.focus, self.position = true, true, false, { x = 0, y = 0 }
        self.inst = T.entity({ entity = { SetParent = function() end } })
    end
    function Base:SetPosition(x, y) self.position = type(x) == "table" and x or { x = x, y = y } end
    function Base:GetPosition() return self.position end
    function Base:Show() self.shown = true end
    function Base:Hide() self.shown = false end
    function Base:SetOnClick(fn) self.onclick = fn end
    function Base:SetText(text) self.text = text end
    for _, method in ipairs({ "SetScale", "SetTint", "ScaleTo", "RotateTo", "SetTextSize", "SetTextColour",
        "SetTextFocusColour", "ClearHoverText", "StopUpdating", "StopFollowMouse", "OnGainFocus", "OnLoseFocus" }) do
        Base[method] = function() end
    end
    local Widget = Class(Base, Base._ctor)
    package.loaded["widgets/textbutton"] = function() return Widget("button") end
    local cooking = { cookbook_recipes = { cookbook = {
        soup = { health = 10, hunger = 20, sanity = 5 }, custom = { custom_cookbook_details_fn = function() end },
    } } }
    package.loaded.cooking = cooking
    env.require = function(module)
        if module == "widgets/widget" then return Widget end
        if module == "widgets/ingredientui" then return function(_, _, _, _, _, _, owner, recipe_type)
            local node = Widget("ingredient"); node.owner, node.recipe_type = owner, recipe_type; return node
        end end
        if module == "cooking" then return cooking end
        return function() return Widget("image") end
    end
    env.resolvefilepath, env.RoundBiasedUp = function(value) return value end, function(value) return math.floor(value + .5) end
    env.EQUIPSLOTS, env.CHARACTER_INGREDIENT = { BODY = "body" }, { HEALTH = "health", MAX_HEALTH = "max_health", OLDAGE = "oldage" }
    env.TUNING, env.STRINGS, env.GetGameModeProperty = { OLDAGE_HEALTH_SCALE = .4 }, { NAMES = {} }, function() return false end
    env.MOVE_UP, env.MOVE_DOWN, env.MOVE_LEFT, env.MOVE_RIGHT = 1, 2, 3, 4
    local CraftingIngredients = native("widgets/redux/craftingmenu_ingredients")
    local NativePage = native("widgets/redux/cookbookpage_crockpot")
    local player, hud = { replica = {} }, Widget("HUD")
    player.HUD, hud.is_screen = hud, true
    local states = { test = { meta = { build_state = "build" } },
        twigs = { meta = { build_state = "hide" }, recipe = { forward_ingredients = { "twigs_variant" } } },
        twigs_variant = { meta = { build_state = "build" }, recipe = {} },
    }
    hud.controls = { craftingmenu = { GetRecipeState = function(_, key) return states[key] end } }
    player.replica.builder = { IngredientMod = function() return .5 end, HasTechIngredient = function() return true, 2 end,
        HasCharacterIngredient = function() return true, 5 end, IsFreeBuildMode = function() return false end }
    player.HasTag = function() return true end
    local amulet = { prefab = "greenamulet", replica = { inventoryitem = { GetAtlas = function() end, GetImage = function() end } } }
    player.replica.inventory = { GetEquippedItem = function() return amulet end, Has = function() return true, 5 end }
    local function ingredient(kind, amount)
        return { type = kind, amount = amount, GetAtlas = function() end, GetImage = function() end }
    end
    env.Ingredient = ingredient
    local recipe = { name = "test", ingredients = { ingredient("twigs", 3) },
        character_ingredients = { ingredient("health", 10) },
        tech_ingredients = { ingredient("invisible", 1), ingredient("sculpting_material", 2) } }
    local ctx = { player = player, hud = hud, config = { announcements = true }, scope = Scope.new(player, error),
        G = { UICOLOURS = {}, MOVE_UP = 1, MOVE_DOWN = 2, MOVE_LEFT = 3, MOVE_RIGHT = 4,
            TheCookbook = { IsUnlocked = function() return { has_eaten = true } end },
            STRINGS = { NAMES = {}, UI = { COOKBOOK = { FOOD_EFFECTS_NONE = "None" } } } },
        text = function(_, _, en) return en end, report = function(_, err) error(err) end,
    }
    local hooks, sent = {}, {}
    ctx.announcements = { food = function(_, data, screen) sent[#sent + 1] = { data = data, screen = screen }; return true end }
    local Controls = require("fieldkit/ui/announcement_controls")
    Controls.install({ AddClassPostConstruct = function(path, fn) hooks[path] = fn end }, function() return ctx end)
    local materials = hud:AddChild(CraftingIngredients(player, 6, recipe))
    local original = materials.SetRecipe
    Controls.attach(ctx)
    Controls.attach(ctx)
    assert(materials.ingredient_widgets[1].fieldkit_ingredient.category == "tech"
        and materials.ingredient_widgets[1].fieldkit_ingredient.index == 2)
    assert(materials.ingredient_widgets[2].recipe_type == "twigs_variant"
        and materials.ingredient_widgets[2].fieldkit_ingredient.recipe == "test"
        and materials.ingredient_widgets[2].fieldkit_ingredient.category == "item",
        "native forwarded ingredient crafting never changes what the parent recipe consumes")
    assert(materials.ingredient_widgets[3].recipe_type == "oldage"
        and materials.ingredient_widgets[3].fieldkit_ingredient.category == "character")
    assert(materials.ingredient_widgets[4].fieldkit_ingredient == nil, "green amulet decoration is not a health ingredient")
    local old = materials.ingredient_widgets[2]
    materials:SetRecipe(recipe)
    assert(not old.inst:IsValid() and materials.ingredient_widgets[2].fieldkit_ingredient.recipe == "test")
    materials:Kill()
    assert(ctx.announcement_widgets[materials] == nil and materials.SetRecipe == original,
        "destroyed native ingredient panels release their local hook instead of accumulating in the HUD scope")

    local screen = Widget("CookbookPopupScreen")
    screen.owner, screen.is_screen = player, true
    hooks["screens/cookbookpopupscreen"](screen)
    local page = screen:AddChild(Widget("page"))
    page.parent_screen, page.category, page.all_recipes = player, "cookbook", {
        { prefab = "soup", unlocked = true, has_eaten = true }, { prefab = "custom", unlocked = true, has_eaten = true },
    }
    page.details_root, page.recipe_grid = page:AddChild(Widget("details")), page:AddChild(Widget("grid"))
    page.PopulateRecipeDetailPanel = function(_, data) return data end
    local populate = page.PopulateRecipeDetailPanel
    hooks["widgets/redux/cookbookpage_crockpot"](page)
    local button
    for child in pairs(page.children) do if child.name == "button" then button = child end end
    assert(button and button.shown)
    page.recipe_grid:SetFocus()
    assert(screen:OnFocusMove(ctx.G.MOVE_RIGHT, true) and button.focus and not page.recipe_grid.focus,
        "native Widget focus traversal reaches the food action")
    button.onclick()
    assert(sent[1].screen == screen and sent[1].data.prefab == "soup")
    T.each("unavailable cookbook actions", {
        { name = "custom details", prefab = "custom", unlocked = true, has_eaten = true },
        { name = "uneaten", prefab = "soup", unlocked = true, has_eaten = false },
        { name = "locked", prefab = "soup", unlocked = false, has_eaten = true },
    }, function(data)
        page:PopulateRecipeDetailPanel(page.all_recipes[1])
        button:SetFocus()
        page:PopulateRecipeDetailPanel(data)
        assert(not button.shown and page.recipe_grid.focus and not button.focus)
        assert(not screen:OnFocusMove(ctx.G.MOVE_RIGHT, true))
    end)
    page:PopulateRecipeDetailPanel(page.all_recipes[1])
    page.spinners = { page:AddChild(Widget("spinner")) }
    page.recipe_grid.items = { {} }
    NativePage._DoFocusHookups(page)
    assert(screen:OnFocusMove(ctx.G.MOVE_RIGHT, true) and button.focus, "native filter focus updates preserve the action link")
    assert(page.PopulateRecipeDetailPanel ~= populate)
    ctx.scope:close()
    assert(not button.inst:IsValid() and page.PopulateRecipeDetailPanel == populate
        and not screen.fieldkit_announcement_context and page.recipe_grid.focus_flow[ctx.G.MOVE_RIGHT] == nil)
end)
T.done()
