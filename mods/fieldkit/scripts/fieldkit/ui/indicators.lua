local M = {}

function M.new(ctx)
    local G, circles, labels, arrows, glows = ctx.G, {}, {}, {}, {}
    local Widget = require("widgets/widget")
    local Text = require("widgets/text")
    local Image = require("widgets/image")
    local root = (ctx.hud.under_root or ctx.hud):AddChild(Widget("fieldkit_indicators"))
    root:SetClickable(false)
    local self, ground, ground_arrow = {}, nil, nil

    local function entity(kind)
        local inst = G.CreateEntity()
        inst.entity:AddTransform()
        inst.entity:SetCanSleep(false)
        inst:AddTag("CLASSIFIED")
        inst:AddTag("NOCLICK")
        inst:AddTag("NOBLOCK")
        inst.persists = false
        if kind == "label" then
            inst.entity:AddLabel()
            inst.Label:SetFont(G.DEFAULTFONT)
            inst.Label:SetFontSize(20 * (ctx.config.text_scale or 1))
            inst.Label:SetWorldOffset(0, 1.5, 0)
            inst.Label:SetColour(1, .85, .4)
        elseif kind == "glow" then
            inst.entity:AddAnimState()
            inst.AnimState:SetBank("lavaarena_item_pickup_fx")
            inst.AnimState:SetBuild("lavaarena_item_pickup_fx")
            inst.AnimState:PlayAnimation("loop", true)
            inst.AnimState:SetBloomEffectHandle("shaders/anim.ksh")
            inst.AnimState:SetLightOverride(1)
            inst.AnimState:SetFinalOffset(1)
            inst.Transform:SetScale(.6, .6, .6)
            inst:AddTag("FX")
        else
            inst.entity:AddAnimState()
            inst.AnimState:SetBank("firefighter_placement")
            inst.AnimState:SetBuild("firefighter_placement")
            inst.AnimState:PlayAnimation("idle", true)
            inst.AnimState:SetOrientation(G.ANIM_ORIENTATION.OnGround)
            inst.AnimState:SetLayer(G.LAYER_BACKGROUND)
            inst.AnimState:SetSortOrder(1)
            inst.AnimState:SetLightOverride(1)
        end
        return inst
    end

    local function label(key, point, text, present)
        local inst = labels[key]
        if not inst then inst = entity("label"); labels[key] = inst end
        inst.Transform:SetPosition(point.x, 0, point.z)
        if inst._text ~= text then inst.Label:SetText(text); inst._text = text end
        inst.Label:Enable(true)
        present[key] = true
    end

    local function prune(pool, present, widget)
        for key, inst in pairs(pool) do
            if not present[key] then
                if widget then inst:Kill() elseif inst:IsValid() then inst:Remove() end
                pool[key] = nil
            end
        end
    end

    local function direction(widget, record)
        if not widget then
            widget = root:AddChild(Widget("fieldkit_direction"))
            widget:SetClickable(false)
            widget.arrow = widget:AddChild(Image("images/ui.xml", "scroll_arrow.tex"))
            widget.arrow:SetScale(.45)
            widget.text = widget:AddChild(Text(G.BODYTEXTFONT, 20 * (ctx.config.text_scale or 1)))
            widget.text:SetPosition(0, 28)
        end
        local x, y, angle = G.GetIndicatorLocationAndAngle(ctx.player, record.point.x, record.point.z, 100, 50)
        widget:SetPosition(x, y)
        widget.arrow:SetRotation(angle + 45)
        widget.text:SetString(record.label)
        return widget
    end

    function self:locate(record)
        if not record then
            if ground then
                for _, inst in pairs(ground) do if inst:IsValid() then inst:Remove() end end
                ground = nil
            end
            if ground_arrow then ground_arrow:Kill(); ground_arrow = nil end
            return
        end
        if not ground then
            ground = { circle = entity("circle"), label = entity("label") }
            ground.circle.Transform:SetScale(.1, .1, .1)
            ground.circle.AnimState:SetMultColour(1, .85, .4, .85)
        end
        for _, inst in pairs(ground) do inst.Transform:SetPosition(record.point.x, 0, record.point.z) end
        ground.label.Label:SetText(record.label)
        ground.label.Label:Enable(true)
        local x, y = G.TheSim:GetScreenPos(record.point.x, 0, record.point.z)
        local width, height = G.TheSim:GetScreenSize()
        if not x or not y or x < 0 or y < 0 or x > width or y > height then
            ground_arrow = direction(ground_arrow, record)
        elseif ground_arrow then ground_arrow:Kill(); ground_arrow = nil end
    end

    function self:render(frame)
        local current_circles, current_labels, current_arrows, current_glows = {}, {}, {}, {}
        for _, record in ipairs(frame.ranges or {}) do
            label(record.key, record.point, record.label, current_labels)
            for i, rule in ipairs(record.rules) do
                local key = record.key .. ":" .. i
                local inst = circles[key]
                if not inst then inst = entity("circle"); circles[key] = inst end
                -- Native firesuppressor's helper uses scale 1.55 for its 15-unit detector.
                local scale = rule.radius * 1.55 / 15
                inst.Transform:SetScale(scale, scale, scale)
                local center = record.point
                inst.Transform:SetPosition(center.x, 0, center.z)
                inst.AnimState:SetMultColour(unpack(rule.color or { .35, .8, 1, .6 }))
                current_circles[key] = true
            end
        end
        for _, record in ipairs(frame.loot or {}) do
            label(record.key, record.point, record.label, current_labels)
            if record.glow then
                local inst = glows[record.key]
                if not inst then inst = entity("glow"); glows[record.key] = inst end
                inst.Transform:SetPosition(record.point.x, 0, record.point.z)
                current_glows[record.key] = true
            end
        end
        for _, record in ipairs(frame.bosses or {}) do
            arrows[record.key] = direction(arrows[record.key], record)
            current_arrows[record.key] = true
        end
        prune(circles, current_circles)
        prune(labels, current_labels)
        prune(arrows, current_arrows, true)
        prune(glows, current_glows)
    end

    function self:close()
        self:locate(nil)
        prune(circles, {})
        prune(labels, {})
        prune(glows, {})
        arrows = {}
        root:Kill()
    end
    return self
end

return M
