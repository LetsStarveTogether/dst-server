local Widget = require("widgets/widget")
local Image = require("widgets/image")
local Text = require("widgets/text")
local TextButton = require("widgets/textbutton")
local MapInput = require("fieldkit/game/map_input")
local RouteOverlay = require("fieldkit/ui/map_overlay")
local M = {}

function M.new(ctx, view)
    if not view then return nil end
    local G = ctx.G
    local width, height = 240, 160
    local self = { view = view, focused = false, consumed = {}, buttons = {} }
    local root = ctx.hud.root:AddChild(Widget("fieldkit_minimap"))
    self.root = root
    root:SetHAnchor(G.ANCHOR_LEFT)
    root:SetVAnchor(G.ANCHOR_TOP)
    root:SetScaleMode(G.SCALEMODE_PROPORTIONAL)
    local hud_scale = G.TheFrontEnd:GetHUDScale()
    root:SetScale(hud_scale)
    function self:layout(top)
        self.top = top
        root:SetPosition(width * hud_scale / 2 + 20, -top - height * hud_scale / 2)
    end
    self:layout(190)
    ctx.scope:listen("refreshhudsize", function(_, scale)
        hud_scale = scale
        root:SetScale(scale)
        self:layout(self.top)
    end, ctx.hud.inst)
    local paper = root:AddChild(Image("images/hud.xml", "map.tex"))
    paper:SetSize(width, height)
    paper.inst.ImageWidget:SetBlendMode(G.BLENDMODE.Premultiplied)
    local crop = root:AddChild(Widget("fieldkit_map_crop"))
    crop:SetScissor(-width / 2, -height / 2, width, height)
    local image = crop:AddChild(Image())
    image:SetClickable(false)
    image.inst.ImageWidget:SetBlendMode(G.BLENDMODE.Additive)
    local route = RouteOverlay.new(ctx, crop, view.renderer, function()
        local image_width, image_height = image:GetSize()
        return image_width, image_height, width, height
    end)
    self.route = route
    local reticle = crop:AddChild(Text(G.BODYTEXTFONT, 24, "+"))
    reticle:SetClickable(false)
    local help = root:AddChild(Text(G.BODYTEXTFONT, 17))
    help:SetPosition(0, -height / 2 - 50)
    help:SetRegionSize(width + 60, 40)
    help:SetClickable(false)

    local function active() return not ctx.scope.closed and view:active() end
    local function clear_focus()
        self.focused, self.drag = false, nil
        ctx.travel:resume_route(self)
        ctx.hud:SetModFocus("fieldkit", "minimap", false)
        root:ClearFocus()
        reticle:Hide()
    end
    local function button(label, x, y, fn)
        local item = root:AddChild(TextButton())
        item:SetFont(G.BODYTEXTFONT)
        item:SetTextSize(20)
        item:SetText(label)
        item:SetPosition(x, y)
        item:SetOnClick(fn)
        self.buttons[#self.buttons + 1] = item
        return item
    end

    function self:set_open(open)
        if not open then clear_focus() end
        view:set_open(open)
        return true
    end

    function self:recenter() view:recenter(); return true end

    function self:focus()
        if not active() then return false end
        view:set_open(true)
        if not ctx.travel:pause_route(self) then ctx.executor:cancel("focus") end
        self.focused = true
        ctx.hud:SetModFocus("fieldkit", "minimap", true)
        root:SetFocus()
        reticle:Show()
        return true
    end

    local function full_map(point)
        if not active() then return false end
        clear_focus()
        ctx.hud.controls:ShowMap(point)
        return true
    end
    local toggle = button("", 0, height / 2 + 16, function() self:set_open(not view.open) end)
    local zoom_in = button("+", -width * .4, -height / 2 - 18, function() view:zoom(-1) end)
    local zoom_out = button("−", -width * .2, -height / 2 - 18, function() view:zoom(1) end)
    local center = button(ctx:text("回中", "Center"), width * .08, -height / 2 - 18, function() self:recenter() end)
    local expand = button(ctx:text("地图", "Map"), width * .36, -height / 2 - 18, full_map)
    local content = { paper, crop, zoom_in, zoom_out, center, expand, help }

    local function local_point(x, y)
        local origin, scale = root:GetWorldPosition(), root:GetWorldScale()
        return (x - origin.x) / scale.x, (y - origin.y) / scale.y
    end

    local function under_pointer()
        local pos = G.TheInput:GetScreenPosition()
        return local_point(pos.x, pos.y)
    end

    function self:consumes_pointer()
        if not active() then return false end
        local x, y = under_pointer()
        local bottom = view.open and -height / 2 - 34 or height / 2 + 2
        return math.abs(x) <= width / 2 and y >= bottom and y <= height / 2 + 30
    end

    local function over_map(x, y)
        return active() and view.open and math.abs(x) <= width / 2 and math.abs(y) <= height / 2
    end

    function self:world_position(x, y)
        local image_width, image_height = image:GetSize()
        local wx, wz = view.renderer:MapPosToWorldPos(2 * x / image_width, 2 * y / image_height, 0)
        return G.Vector3(wx, 0, wz)
    end

    local function navigate(point, append)
        local _, native_right = ctx.player.components.playercontroller:UpdateActionsToMapActions(point, nil)
        if native_right then full_map(point); return true end
        if not append then clear_focus() end
        local ok, reason = MapInput.request(ctx, point, append)
        if reason then ctx:notify(reason) end
        return ok
    end

    function self:render(visible, open)
        if self.rendered_visible == visible and self.rendered_open == open then return end
        self.rendered_visible, self.rendered_open = visible, open
        if not visible then
            clear_focus()
            route:update(0, false)
            root:Hide()
            return
        end
        root:Show()
        toggle:SetText(ctx:text(open and "收起小地图" or "展开小地图", open and "Collapse map" or "Expand map"))
        for _, widget in ipairs(content) do if open then widget:Show() else widget:Hide() end end
        if self.focused and open then reticle:Show() else reticle:Hide() end
    end

    function self:handle_control(control, down)
        if not down and self.consumed[control] then
            local generation = self.consumed[control]
            self.consumed[control] = nil
            -- All controls sharing the physical button must finish their down phase before focus moves.
            if generation == ctx.executor.generation and self.focused and active() then
                if control == G.CONTROL_CANCEL then clear_focus()
                elseif control == G.CONTROL_MAP then full_map()
                elseif control == G.CONTROL_MENU_MISC_2 then navigate(self:world_position(0, 0), true)
                elseif control == G.CONTROL_MENU_MISC_3 or control == G.CONTROL_ACCEPT
                    or control == G.CONTROL_CONTROLLER_ACTION then navigate(self:world_position(0, 0), false) end
            end
            return true
        end
        if not active() then return false end
        if self.focused then
            if down then
                self.consumed[control] = ctx.executor.generation
                if control == G.CONTROL_MAP_ZOOM_IN then view:zoom(-1)
                elseif control == G.CONTROL_MAP_ZOOM_OUT then view:zoom(1)
                elseif control == G.CONTROL_MENU_MISC_1 then self:recenter() end
            end
            return true
        end
        local x, y = under_pointer()
        local pointer_input = not G.TheInput:ControllerAttached() or G.TheFrontEnd.tracking_mouse
        if pointer_input and over_map(x, y) and (control == G.CONTROL_PRIMARY or control == G.CONTROL_ACCEPT
            or control == G.CONTROL_SECONDARY) then
            if down then self.consumed[control] = ctx.executor.generation end
            return true
        elseif pointer_input and over_map(x, y)
            and (control == G.CONTROL_MAP_ZOOM_IN or control == G.CONTROL_MAP_ZOOM_OUT) then
            if down then
                view:zoom(control == G.CONTROL_MAP_ZOOM_IN and -1 or 1)
                self.consumed[control] = ctx.executor.generation
            end
            return true
        end
        return false
    end

    ctx.scope:wrap(ctx.player.components.playercontroller, "DoCameraControl", function(original)
        return function(controller, ...)
            local x, y = under_pointer()
            local pointer_input = not G.TheInput:ControllerAttached() or G.TheFrontEnd.tracking_mouse
            if self.focused or pointer_input and over_map(x, y) then return end
            return original(controller, ...)
        end
    end)

    root.OnControl = function(widget, control, down)
        if self:handle_control(control, down) then return true end
        return Widget.OnControl(widget, control, down)
    end
    root.OnMouseButton = function(widget, which, down, x, y)
        if Widget.OnMouseButton(widget, which, down, x, y) then return true end
        local lx, ly = local_point(x, y)
        if which == G.MOUSEBUTTON_LEFT and not down and self.drag then
            self.drag = nil
            ctx.travel:resume_route(self)
            return true
        end
        if not over_map(lx, ly) then return false end
        if down and self.focused then clear_focus() end
        if which == G.MOUSEBUTTON_LEFT then
            if down then
                if not ctx.travel:pause_route(self) then ctx.executor:cancel("focus") end
                self.drag = { x = x, y = y }
            end
            return true
        elseif which == G.MOUSEBUTTON_RIGHT then
            if down then navigate(self:world_position(lx, ly), G.TheInput:IsKeyDown(G.KEY_SHIFT)) end
            return true
        end
        return false
    end
    root.OnUpdate = function(_, dt)
        view:refresh()
        if not active() or not view.open then route:update(0, false); return end
        local screen_width, screen_height = G.TheSim:GetScreenSize()
        local scale = root:GetWorldScale()
        -- Keep the full native projection; the panel clips it without resizing the renderer.
        local image_width, image_height = screen_width / scale.x, screen_height / scale.y
        local handle = view.renderer:GetTextureHandle()
        if self.handle ~= handle then
            image.inst.ImageWidget:SetTextureHandle(handle)
            self.handle, self.image_width = handle, nil
        end
        if self.image_width ~= image_width or self.image_height ~= image_height then
            image:SetSize(image_width, image_height)
            self.image_width, self.image_height = image_width, image_height
        end
        if self.drag then
            if G.TheInput:IsControlPressed(G.CONTROL_PRIMARY) then
                local pos = G.TheInput:GetScreenPosition()
                view:pan((pos.x - self.drag.x) * 2 / 9, (pos.y - self.drag.y) * 2 / 9)
                self.drag.x, self.drag.y = pos.x, pos.y
            else
                self.drag = nil
                ctx.travel:resume_route(self)
            end
        end
        if self.focused then
            local dx = G.TheInput:GetAnalogControlValue(G.CONTROL_MOVE_RIGHT) - G.TheInput:GetAnalogControlValue(G.CONTROL_MOVE_LEFT)
            local dy = G.TheInput:GetAnalogControlValue(G.CONTROL_MOVE_UP) - G.TheInput:GetAnalogControlValue(G.CONTROL_MOVE_DOWN)
            local deadzone = G.TUNING.CONTROLLER_DEADZONE_RADIUS
            if dx * dx + dy * dy >= deadzone * deadzone then view:pan(-100 * dt * dx, -100 * dt * dy) end
        end
        route:update(dt, true)
        if self.help_focus ~= self.focused then
            self.help_focus = self.focused
            local id = G.TheInput:GetControllerID()
            if self.focused then
                help:SetString(G.TheInput:GetLocalizedControl(id, G.CONTROL_MENU_MISC_2) .. ctx:text(" 加点", " Add") .. "  "
                    .. G.TheInput:GetLocalizedControl(id, G.CONTROL_MENU_MISC_3) .. ctx:text(" 导航", " Go") .. "  "
                    .. G.TheInput:GetLocalizedControl(id, G.CONTROL_CANCEL) .. ctx:text(" 返回", " Back"))
            else help:SetString(ctx:text("拖动平移 · 右键导航 · Shift 加点", "Drag to pan · Right click to go · Shift adds")) end
        end
    end
    ctx.scope:defer(function() clear_focus(); route:close(); root:Kill() end)
    view:attach(self)
    root:OnUpdate(0)
    root:StartUpdating()
    return self
end

return M
