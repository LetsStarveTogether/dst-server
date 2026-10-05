local Scope = require("fieldkit/core/scope")
local Config = require("fieldkit/core/config")
local Parameters = require("fieldkit/core/parameters")
local Store = require("fieldkit/core/store")
local Executor = require("fieldkit/core/executor")
local Input = require("fieldkit/game/input")
local M = {}

function M.attach(G, player, config, store, params)
    local ctx = { G = G, player = player, hud = player.HUD, config = config, store = store,
        params = params or Parameters.new() }
    function ctx:text(zh, en) return config.language == "zh" and zh or en end
    function ctx:report(error) G.print("[Fieldkit] " .. tostring(error)) end
    function ctx:notify(message)
        self.message = tostring(message)
        self.message_until = G.GetTime() + 8
    end
    ctx.scope = Scope.new(player, function(err) ctx:report(err) end)
    function ctx:ready(mode) return Input.ready(self, mode) end
    ctx.executor = Executor.new(G.GetTime, function(job) return Input.ready(ctx, nil, job) end,
        function(message) ctx:notify(message) end, function(err) ctx:report(err) end)
    ctx.scope:defer(function() ctx.executor:cancel("invalid") end)
    ctx.scope:listen("onremove", function() ctx.scope:close() end)
    ctx.scope:listen("playerdeactivated", function() ctx.scope:close() end)
    local function mount()
        require("fieldkit/game/actions").attach(ctx)
        ctx.equipment = require("fieldkit/game/equipment").new(ctx)
        ctx.repair = require("fieldkit/features/repair").new(ctx)
        ctx.journal = require("fieldkit/game/journal").new(ctx)
        ctx.wormholes = require("fieldkit/features/wormholes").new(ctx)
        ctx.timers = require("fieldkit/features/timers").new(ctx)
        ctx.information = require("fieldkit/game/information").new(ctx)
        ctx.work = require("fieldkit/features/work").new(ctx)
        ctx.fishing = require("fieldkit/features/fishing").new(ctx)
        ctx.travel = require("fieldkit/features/travel").new(ctx)
        ctx.cooking = require("fieldkit/features/cooking").new(ctx)
        ctx.storage = require("fieldkit/features/storage").new(ctx)
        ctx.drying = require("fieldkit/features/drying").new(ctx)
        ctx.announcements = require("fieldkit/features/announcements").new(ctx)
        ctx.emotes = require("fieldkit/features/emotes").new(ctx)
        ctx.ground = require("fieldkit/features/ground").new(ctx)
        ctx.presentation = require("fieldkit/features/presentation").new(ctx)
        ctx.visual = require("fieldkit/features/visual").new(ctx)
        ctx.placement = require("fieldkit/features/placement").new(ctx)
        ctx.construction = require("fieldkit/features/construction").new(ctx)
        ctx.selection = require("fieldkit/features/selection").new(ctx)
        ctx.mapview = require("fieldkit/game/map_view").new(ctx)
        ctx.minimap = require("fieldkit/ui/minimap").new(ctx, ctx.mapview)
        ctx.awareness = require("fieldkit/features/awareness").new(ctx)
        local Panel = require("fieldkit/ui/panel")
        function ctx:open()
            if self.screen then self.screen:Close(); return end
            if not self:ready("ui") then return false end
            self.executor:cancel("focus")
            local controller = player.components.playercontroller
            local target = G.TheInput:GetWorldEntityUnderMouse()
            local point = G.TheInput:GetWorldPosition()
            if G.TheInput:ControllerAttached() then
                local action = controller:GetActionButtonAction()
                target = action and action.target or controller.controller_target
                point = target and target:GetPosition()
                if not point then
                    local x, _, z = player.Transform:GetWorldPosition()
                    local angle = player.Transform:GetRotation() * math.pi / 180
                    point = G.Vector3(x + math.cos(angle) * 3, 0, z - math.sin(angle) * 3)
                end
            end
            -- Native positions retain the selected point on a moving or rotating platform.
            local intent = { target = target, position = point and G.DynamicPosition(G.Vector3(point.x, 0, point.z)),
                placement = self.placement:context() }
            self.screen = Panel(self, intent)
            G.TheFrontEnd:PushScreen(self.screen)
        end
        function ctx:open_emotes(hold_key)
            if self.screen or not config.gestures or not self:ready("ui") then return false end
            self.executor:cancel("focus")
            self.screen = require("fieldkit/ui/emote_wheel")(self, hold_key)
            G.TheFrontEnd:PushScreen(self.screen)
            return true
        end
        ctx.scope:defer(function() if ctx.screen then ctx.screen:Close(false) end end)

        ctx.overlay = require("fieldkit/ui/hud").new(ctx)
        if config.ground then ctx.ground_dock = require("fieldkit/ui/ground_list").dock(ctx) end
        require("fieldkit/game/map_input").attach(ctx)
        Input.attach(ctx)
        require("fieldkit/game/inventory_input").attach(ctx)
        require("fieldkit/game/pointer_input").attach(ctx)
        require("fieldkit/ui/announcement_controls").attach(ctx)
        ctx.scope:periodic(.25, function()
            if G.ThePlayer ~= player or player.HUD ~= ctx.hud then ctx.scope:close() end
        end)

        local function bind(key, fn, down)
            if key and key ~= 0 then
                local register = down and G.TheInput.AddKeyDownHandler or G.TheInput.AddKeyUpHandler
                local handler = register(G.TheInput, key, function()
                    if not G.TheFrontEnd.textProcessorWidget then fn() end
                end)
                ctx.scope:defer(function() handler:Remove() end)
            end
        end
        bind(config.cancel_key, function()
            ctx.params.auto_repair = false
            ctx.executor:cancel("cancelled")
            ctx.selection:clear()
            if ctx.screen then ctx.screen:Close() end
            if ctx.ground_dock then ctx.ground_dock:release_focus() end
        end)
        bind(config.panel_key, function() ctx:open() end)
        bind(config.gesture_key, function() ctx:open_emotes(config.gesture_key) end, true)
        bind(config.range_key, function()
            if ctx:ready() then
                local ok, reason = ctx.awareness:toggle_pin(G.TheInput:GetWorldEntityUnderMouse())
                if not ok and reason then ctx:notify(reason) end
            end
        end)
        bind(config.range_clear_key, function()
            if ctx:ready() then ctx.awareness:clear_pins() end
        end)
        bind(config.cooking_key, function()
            local _, reason = ctx.cooking:shortcut()
            if reason then ctx:notify(reason) end
        end)
        bind(config.grid_key, function()
            if config.grid and ctx:ready() then
                ctx.placement:swap_shape()
                ctx:notify(ctx:text("放置格型：", "Grid: ") .. Parameters.label(ctx, "grid_shape"))
            end
        end)
        ctx.scope:periodic(.1, function() ctx.executor:tick() end)
    end
    local ok, err = pcall(mount)
    if not ok then
        ctx:report(err)
        ctx.scope:close()
        return nil, err
    end
    return ctx
end

function M.install(env)
    local G, active, store = env.GLOBAL
    local params = Parameters.new()
    local config = Config.read(env.modinfo.configuration_options, env.GetModConfigData,
        G.LOC and G.LOC.GetLocaleCode())
    require("fieldkit/game/information").install(env)
    require("fieldkit/features/placement").install(env, function() return active end)
    require("fieldkit/features/visual").install(env)
    require("fieldkit/features/cooking").install(env, function() return active end)
    require("fieldkit/game/map_input").install(env, function() return active end)
    require("fieldkit/game/map_view").install(env, function() return active end)
    require("fieldkit/game/inventory_input").install(env, function() return active end)
    require("fieldkit/ui/announcement_controls").install(env, function() return active end)
    env.AddClassPostConstruct("screens/redux/pausescreen", function(screen)
        local function add_entry()
            local menu, ctx = screen.menu, active
            if not menu or not ctx or ctx.scope.closed or screen.owner ~= ctx.player or menu._fieldkit then return end
            local count = #menu.items
            if count < 2 then return end
            menu._fieldkit = true
            local button = menu:AddItem("Fieldkit", function()
                screen:unpause()
                if active == ctx and not ctx.scope.closed then
                    ctx:open()
                end
            end)
            button:SetScale(.7)
            -- Fit one additional row inside the original first/last row bounds.
            menu.offset = menu.offset * (count - 1) / count
            for i, item in ipairs(menu.items) do item:SetPosition(0, (i - 1) * menu.offset) end
        end
        local original = screen.BuildMenu
        screen.BuildMenu = function(self, ...)
            local function finish(...)
                add_entry()
                return ...
            end
            return finish(original(self, ...))
        end
        add_entry()
    end)
    env.AddPlayerPostInit(function(player)
        local function activate()
            if player ~= G.ThePlayer or not player.HUD then return end
            if active and active.player == player and active.hud == player.HUD and not active.scope.closed then return end
            if active then active.scope:close() end
            if not store then
                store = Store.new(G.TheSim, require("json"), function(reason)
                    G.print("[Fieldkit] profile " .. reason)
                    if active then active:notify(active:text("本地配置保存或读取失败", "Local profile could not be saved or loaded")) end
                end)
            end
            active = M.attach(G, player, config, store, params)
        end
        player:ListenForEvent("playeractivated", activate)
        -- Covers clients whose post-init runs after activation; no polling loop per remote player.
        player:DoTaskInTime(0, activate)
    end)
end

return M
