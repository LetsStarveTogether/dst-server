local Parameters = require("fieldkit/core/parameters")
local Query = require("fieldkit/game/query")
local Geometry = require("fieldkit/rules/geometry")
local M = {}

local function point(x, y, z)
    if not Geometry.finite(x) or not Geometry.finite(z) then return end
    return { x = x, y = y or 0, z = z }
end

function M.new(ctx)
    local self = { entries = {}, selected = {}, markers = {}, owned = {}, preview = {} }
    local G, input = ctx.G, ctx.G.TheInput
    local controller = ctx.player.components.playercontroller
    local rectangle
    local limit = Parameters.LIMIT
    local function limit_message()
        return ctx:text("目标上限为 ", "Target limit: ") .. limit
            .. ctx:text("；请缩小选择范围", "; narrow the selection")
    end

    local function available()
        if ctx.scope.closed then return false end
        if ctx.config.work == false then return false, ctx:text("交互辅助已关闭", "Interaction assistance is disabled") end
        return ctx:ready()
    end

    local function action(target, button)
        if not Query.visible(ctx, target) or target == ctx.player or target:HasTag("player") then return end
        if ctx.player:GetDistanceSqToInst(target) > ctx.params.search_radius ^ 2 then return end
        return ctx.work:preview(target, button)
    end

    local function hide_rectangle()
        if rectangle then rectangle:Hide() end
    end

    local function draw_rectangle()
        local drag = self.drag
        if not drag or not drag.screen then hide_rectangle(); return end
        if not rectangle then
            local Widget, Image = require("widgets/widget"), require("widgets/image")
            rectangle = ctx.hud:AddChild(Widget("fieldkit_selection"))
            rectangle:SetHAnchor(G.ANCHOR_LEFT)
            rectangle:SetVAnchor(G.ANCHOR_BOTTOM)
            rectangle:SetScaleMode(G.SCALEMODE_NONE)
            rectangle:SetClickable(false)
            rectangle.fill = rectangle:AddChild(Image("images/global.xml", "square.tex"))
            rectangle.fill:SetClickable(false)
            rectangle.edges = {}
            for i = 1, 4 do
                local edge = rectangle:AddChild(Image("images/global.xml", "square.tex"))
                edge:SetClickable(false)
                rectangle.edges[i] = edge
            end
        end
        local a, b = drag.screen, drag.current
        local width, height = math.abs(a.x - b.x), math.abs(a.y - b.y)
        if width < 5 and height < 5 then hide_rectangle(); return end
        local r, g, blue = drag.remove and 1 or .25, drag.remove and .35 or .85, 1
        rectangle:SetPosition((a.x + b.x) * .5, (a.y + b.y) * .5)
        rectangle.fill:SetSize(math.max(width, 1), math.max(height, 1))
        rectangle.fill:SetTint(r, g, blue, .12)
        for i, edge in ipairs(rectangle.edges) do
            local horizontal = i <= 2
            edge:SetSize(horizontal and math.max(width, 1) or 2, horizontal and 2 or math.max(height, 1))
            edge:SetPosition(horizontal and 0 or (i == 3 and -.5 or .5) * width,
                horizontal and (i == 1 and -.5 or .5) * height or 0)
            edge:SetTint(r, g, blue, .85)
        end
        rectangle:Show()
    end

    local function render()
        local wanted = {}
        for _, entry in ipairs(self.entries) do
            if Query.visible(ctx, entry.target) then wanted[entry.target] = entry.button end
        end
        for _, entry in ipairs(self.preview) do wanted[entry.target] = self.drag and self.drag.remove and "remove" or entry.button end
        for target, marker in pairs(self.markers) do
            if not wanted[target] then
                if marker:IsValid() then marker:Remove() end
                self.markers[target] = nil
            end
        end
        for target, button in pairs(wanted) do
            local marker = self.markers[target]
            if not marker and G.SpawnPrefab then
                -- Native non-networked prefab; target colours/highlight components remain untouched.
                marker = G.SpawnPrefab("axisalignedplacement_outline")
                self.markers[target] = marker
                if marker then marker.AnimState:PlayAnimation("unit") end
            end
            if marker and marker:IsValid() then
                local p = target:GetPosition()
                marker.Transform:SetPosition(p.x, p.y, p.z)
                local radius = target.GetPhysicsRadius and target:GetPhysicsRadius(0) or .5
                local scale = math.max(.75, math.min(4, radius * 2 + .5))
                marker.AnimState:SetScale(scale, scale)
                if button == "remove" then marker.AnimState:SetMultColour(1, .25, .25, .9)
                elseif button == "right" then marker.AnimState:SetMultColour(1, .65, .2, .9)
                else marker.AnimState:SetMultColour(.25, .85, 1, .9) end
                if available() then marker:Show() else marker:Hide() end
            end
        end
    end

    local function add(target, button)
        local entry = self.selected[target]
        if not entry and #self.entries >= limit then return false, limit_message() end
        -- Queued intents belong to work; update the display only after it accepts a revision.
        local accepted, intent = ctx.work:revise(target, button)
        if not accepted then return false, intent end
        button = intent and intent.button or button
        if entry then entry.button = button; return true end
        entry = { target = target, button = button }
        self.selected[target] = entry
        self.entries[#self.entries + 1] = entry
        return true
    end

    function self:forget(target)
        self.selected[target] = nil
        for i = #self.entries, 1, -1 do
            if self.entries[i].target == target then table.remove(self.entries, i) end
        end
        local marker = self.markers[target]
        if marker and marker:IsValid() then marker:Remove() end
        self.markers[target] = nil
    end

    function self:remove(target)
        self:forget(target)
        if ctx.work.remove then ctx.work:remove(target) end
    end

    function self:toggle(target, button)
        if not available() then return false end
        button = button == "right" and "right" or "left"
        if self.selected[target] and self.selected[target].button == button then
            self:remove(target)
        elseif action(target, button) then
            local added, reason = add(target, button)
            if not added then return false, reason end
        else return false end
        render()
        return true
    end

    function self:select_same(target, button)
        if not available() then return false end
        button = button == "right" and "right" or "left"
        local expected = action(target, button)
        if not expected then return false end
        local added, reason = add(target, button)
        if not added then return false, reason end
        local function matching(candidate)
            if candidate.prefab == target.prefab then
                local candidate_action = action(candidate, button)
                return candidate_action and candidate_action.action.id == expected.action.id
            end
        end
        for _, candidate in ipairs(Query.nearby(ctx, ctx.params.search_radius, nil, nil, nil, matching, limit + 1)) do
            local accepted, reason = add(candidate, button)
            if not accepted then ctx:notify(reason); break end
        end
        render()
        return true
    end

    local function save_area(corners, button)
        if #corners ~= 4 or not Geometry.contains(corners, corners[1].x, corners[1].z) then return end
        local map, platform = G.TheWorld.Map, nil
        for i, p in ipairs(corners) do
            local current = map:GetPlatformAtPoint(p.x, p.z)
            if i == 1 then platform = current elseif current ~= platform then return end
        end
        local area = { corners = corners, button = button, platform = platform }
        if platform then
            area.local_corners = {}
            for i, p in ipairs(corners) do
                area.local_corners[i] = point(platform.entity:WorldToLocalSpace(p.x, p.y, p.z))
            end
        end
        return area
    end

    function self:area()
        local area = self.region
        if area and area.platform then
            if not area.platform:IsValid() then self.region = nil; return end
            for i, p in ipairs(area.local_corners) do
                area.corners[i] = point(area.platform.entity:LocalToWorldSpace(p.x, p.y, p.z))
            end
        end
        return area
    end

    local function screen_area(drag)
        local a, b = drag.screen, drag.current
        local corners = {}
        for _, p in ipairs({ { a.x, a.y }, { b.x, a.y }, { b.x, b.y }, { a.x, b.y } }) do
            local projected = point(G.TheSim:ProjectScreenPos(p[1], p[2]))
            if not projected then return end
            corners[#corners + 1] = projected
        end
        return save_area(corners, drag.button)
    end

    local function candidates(corners, button, remove)
        local entries = {}
        local function matching(target)
            local p = target:GetPosition()
            return Geometry.contains(corners, p.x, p.z)
                and ((remove and self.selected[target]) or (not remove and action(target, button)))
        end
        for _, target in ipairs(Query.nearby(ctx, ctx.params.search_radius, nil, nil, nil, matching, limit + 1)) do
            entries[#entries + 1] = { target = target, button = button }
        end
        return entries
    end

    local function apply(entries, remove)
        local failure
        for _, entry in ipairs(entries) do
            if remove then self:remove(entry.target)
            else
                local accepted, reason = add(entry.target, entry.button)
                if not accepted then failure = failure or reason end
            end
        end
        if failure then ctx:notify(failure) end
        render()
    end

    function self:commit()
        local ready, reason = available()
        if not ready then return false, reason end
        if #self.entries == 0 then return false, ctx:text("请先选择目标", "Select targets first") end
        local entries = {}
        for _, entry in ipairs(self.entries) do
            entries[#entries + 1] = { target = entry.target, button = entry.button }
        end
        return ctx.work:enqueue(entries)
    end

    function self:mark_start(p, button)
        if not available() or not p then return false end
        self.first = point(p.x, p.y, p.z)
        self.first_button = button == "right" and "right" or "left"
        self.first_platform = G.TheWorld.Map:GetPlatformAtPoint(p.x, p.z)
        self.first_local = self.first_platform
            and point(self.first_platform.entity:WorldToLocalSpace(p.x, p.y or 0, p.z)) or nil
        self.region = nil
        return self.first ~= nil
    end

    function self:mark_end(p, button)
        if not available() or not self.first or not p then return false end
        local a, b = self.first, point(p.x, p.y, p.z)
        if self.first_platform then
            if not self.first_platform:IsValid() then self.first = nil; return false end
            local local_point = self.first_local
            a = point(self.first_platform.entity:LocalToWorldSpace(local_point.x, local_point.y, local_point.z))
        end
        if not b then return false end
        local area = save_area({ a, { x = b.x, y = a.y, z = a.z }, b,
            { x = a.x, y = b.y, z = b.z } }, button or self.first_button)
        if not area then
            return false, ctx:text("选区需要有面积，且位于同一地面或平台", "Select an area on one surface or platform")
        end
        self.first, self.first_platform, self.first_local, self.region = nil, nil, nil, area
        apply(candidates(area.corners, area.button))
        return true
    end

    function self:cancel_drag()
        self.drag, self.preview = nil, {}
        hide_rectangle()
    end

    function self:clear()
        self:cancel_drag()
        self.entries, self.selected, self.region, self.first = {}, {}, nil, nil
        self.first_platform, self.first_local, self.last_click = nil, nil, nil
        render()
    end

    function self:status()
        if self.first then return ctx:text("选区：已标记起点", "Selection: first corner marked") end
        if #self.entries > 0 then return ctx:text("已选目标：", "Selected: ") .. #self.entries end
        if self.region then return ctx:text("已选区域", "Area selected") end
        return ""
    end

    local function modified()
        local key = ctx.config.selection_modifier
        if key == nil then key = G.KEY_ALT end
        return key and key ~= 0 and input:IsKeyDown(key)
    end

    function self:handle_control(_, control, down)
        local button = control == G.CONTROL_PRIMARY and "left"
            or control == G.CONTROL_SECONDARY and "right" or nil
        if not button then
            if down then self:cancel_drag() end
            return false
        end
        if not down then
            if not self.owned[control] then return false end
            self.owned[control] = nil
            local drag = self.drag
            if not drag or drag.control ~= control then return true end
            self:cancel_drag()
            if not available() or input:GetHUDEntityUnderMouse() then return true end
            drag.current = input:GetScreenPosition()
            local dx, dy = drag.current.x - drag.screen.x, drag.current.y - drag.screen.y
            local changed, selected_area, failure
            if dx * dx + dy * dy >= 36 then
                local area = screen_area(drag)
                if area then
                    self.region, selected_area = area, area
                    local entries = candidates(area.corners, button, drag.remove)
                    apply(entries, drag.remove)
                    changed = #entries > 0
                else
                    ctx:notify(ctx:text("选区需要有面积，且位于同一地面或平台", "Select an area on one surface or platform"))
                end
            elseif drag.target then
                local now, last = G.GetTime(), self.last_click
                if drag.remove then self:remove(drag.target)
                elseif last and last.target == drag.target and last.button == button and now - last.time <= .3 then
                    changed, failure = self:select_same(drag.target, button)
                    self.last_click = nil
                else
                    changed, failure = self:toggle(drag.target, button)
                    self.last_click = { target = drag.target, button = button, time = now }
                end
            end
            if failure then ctx:notify(failure) end
            if not drag.remove and ctx.config.selection_auto_execute ~= false then
                local _, reason
                if changed then _, reason = self:commit()
                elseif selected_area and ctx.construction then _, reason = ctx.construction:area(selected_area) end
                if reason then ctx:notify(reason) end
            end
            return true
        end
        if self.owned[control] == "released" then
            self.owned[control] = nil
            if self.drag and self.drag.control == control then self:cancel_drag() end
        end
        if self.owned[control] then return true end
        if not modified() or not available() or input:ControllerAttached()
            or input:GetHUDEntityUnderMouse() or controller.draggingonground
            or (controller.IsAOETargeting and controller:IsAOETargeting()) then return false end
        self.owned[control] = true
        self.region = nil
        -- Like a native new click, retire a previous held action before owning this gesture.
        if controller.ClearActionHold then controller:ClearActionHold() end
        -- Only selection-owned press/release pairs are consumed; no synthetic native release.
        self.drag = { control = control, button = button, screen = input:GetScreenPosition(),
            current = input:GetScreenPosition(), target = input:GetWorldEntityUnderMouse(),
            remove = G.KEY_CTRL and ctx.config.selection_modifier ~= G.KEY_CTRL and input:IsKeyDown(G.KEY_CTRL) }
        return true
    end

    ctx.scope:wrap(controller, "IsControlPressed", function(original)
        return function(object, control, ...)
            if self.owned[control] then return false end
            return original(object, control, ...)
        end
    end)
    local move = input:AddMoveHandler(function(x, y)
        if self.drag then self.drag.current = { x = x, y = y }; draw_rectangle() end
    end)
    local mouse = input:AddMouseButtonHandler(function(button, down)
        if down then return end
        local control = button == G.MOUSEBUTTON_LEFT and G.CONTROL_PRIMARY
            or button == G.MOUSEBUTTON_RIGHT and G.CONTROL_SECONDARY or nil
        -- GUI can consume control-up, but raw mouse-up still arrives. Retain ownership
        -- until control-up or the next press: raw/control callbacks may arrive in either order.
        if control and self.owned[control] then self.owned[control] = "released" end
    end)
    ctx.scope:defer(function()
        move:Remove()
        mouse:Remove()
        self:clear()
        self.owned = {}
        if rectangle then rectangle:Kill() end
    end)
    ctx.scope:periodic(.1, function()
        if not available() then self:cancel_drag() end
        if self.drag then
            local area = screen_area(self.drag)
            self.preview = area and candidates(area.corners, self.drag.button, self.drag.remove) or {}
        end
        for i = #self.entries, 1, -1 do
            local target = self.entries[i].target
            if not target:IsValid() or target:HasTag("INLIMBO") then self:forget(target) end
        end
        render()
    end)
    return self
end

return M
