local Widget = require("widgets/widget")
local Image = require("widgets/image")
local Text = require("widgets/text")
local M = {}
local MAX_LINES, MAX_MARKERS = 256, 64
local planned, pending = { .25, .85, 1, .95 }, { 1, .8, .25, .9 }
local colours = { { 1, .5, .5 }, { .5, 1, .55 }, { .5, .8, 1 }, { 1, .8, .4 }, { .85, .55, 1 }, { .4, 1, 1 } }

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

local function clip(x1, y1, x2, y2, width, height)
    local dx, dy, low, high = x2 - x1, y2 - y1, 0, 1
    local function boundary(p, q)
        if p == 0 then return q >= 0 end
        local t = q / p
        if p < 0 then low = math.max(low, t) else high = math.min(high, t) end
        return low <= high
    end
    if boundary(-dx, x1 + width / 2) and boundary(dx, width / 2 - x1)
        and boundary(-dy, y1 + height / 2) and boundary(dy, height / 2 - y1) then
        return x1 + low * dx, y1 + low * dy, x1 + high * dx, y1 + high * dy
    end
end

-- Viewport supplies full texture dimensions followed by the visible crop, in parent units.
function M.new(ctx, parent, renderer, viewport)
    local self = { lines = {}, markers = {}, holes = {}, elapsed = .1 }
    local root = parent:AddChild(Widget("fieldkit_map_annotations"))
    self.root = root
    root:SetClickable(false)
    local legend = root:AddChild(Text(ctx.G.BODYTEXTFONT, 15))
    legend:SetClickable(false)
    self.legend = legend

    function self:close()
        if self.closed then return end
        self.closed = true
        root:Kill()
    end

    function self:update(dt, visible)
        if self.closed then return end
        if not visible then
            root:Hide()
            self.elapsed = .1
            return
        end
        self.elapsed = self.elapsed + dt
        if self.elapsed < .1 then return end
        self.elapsed = 0
        if ctx.config.travel ~= false then ctx.travel:update_preview() end
        local snapshot = ctx.config.travel ~= false and ctx.travel:route_snapshot() or { path = {}, points = {} }
        local holes = ctx.wormholes and ctx.wormholes:markers() or {}
        if #snapshot.points == 0 and #holes == 0 then root:Hide(); return end
        local width, height, crop_width, crop_height = viewport()
        root:Show()
        root:SetScissor(-crop_width / 2, -crop_height / 2, crop_width, crop_height)
        local function project(point)
            if not point or not finite(point.x) or not finite(point.z) then return end
            -- MiniMap uses world x/z as its first two arguments, returning normalized texture coordinates.
            local x, y = renderer:WorldPosToMapPos(point.x, point.z, 0)
            if finite(x) and finite(y) then return x * width / 2, y * height / 2 end
        end
        local used, limited = 0, snapshot.truncated == true
        local function line(x1, y1, x2, y2, color, thickness)
            if used == MAX_LINES then limited = true; return end
            local dx, dy = x2 - x1, y2 - y1
            local length = math.sqrt(dx * dx + dy * dy)
            if length < .5 then return end
            used = used + 1
            local item = self.lines[used]
            if not item then
                item = root:AddChild(Image("images/global.xml", "square.tex"))
                item:SetClickable(false)
                self.lines[used] = item
            end
            item:SetSize(length, thickness)
            item:SetPosition((x1 + x2) / 2, (y1 + y2) / 2)
            item:SetRotation(-math.atan2(dy, dx) * 180 / math.pi)
            item:SetTint(unpack(color))
            item:Show()
        end
        local function segment(a, b, dashed)
            local x1, y1 = project(a)
            local x2, y2 = project(b)
            if not x1 or not x2 then return end
            x1, y1, x2, y2 = clip(x1, y1, x2, y2, crop_width, crop_height)
            if not x1 then return end
            if not dashed then line(x1, y1, x2, y2, planned, 3); return end
            local dx, dy = x2 - x1, y2 - y1
            local pieces = math.min(16, math.max(1, math.ceil(math.sqrt(dx * dx + dy * dy) / 12)))
            for i = 0, pieces - 1 do
                line(x1 + dx * i / pieces, y1 + dy * i / pieces,
                    x1 + dx * (i + .55) / pieces, y1 + dy * (i + .55) / pieces, pending, 2)
                if used == MAX_LINES then limited = limited or i < pieces - 1; break end
            end
        end
        local previous = snapshot.origin
        for _, point in ipairs(snapshot.path) do
            segment(previous, point, false)
            previous = point
        end
        previous = #snapshot.path == 0 and snapshot.origin or nil
        for i, point in ipairs(snapshot.points) do
            if i > (snapshot.preview_legs or 0) then segment(previous, point, true) end
            previous = point
        end
        for i = used + 1, #self.lines do self.lines[i]:Hide() end
        local markers = 0
        for index, point in ipairs(snapshot.points) do
            if index > MAX_MARKERS then break end
            local x, y = project(point)
            if x and math.abs(x) <= crop_width / 2 and math.abs(y) <= crop_height / 2 then
                markers = markers + 1
                local item = self.markers[markers]
                if not item then
                    item = root:AddChild(Text(ctx.G.BODYTEXTFONT, 19))
                    item:SetClickable(false)
                    self.markers[markers] = item
                end
                local last = index == #snapshot.points
                item:SetString((last and ctx:text("终 ", "End ") or "") .. (snapshot.completed + index))
                item:SetPosition(x, y + 12)
                item:SetColour(unpack(last and pending or planned))
                item:Show()
                item:MoveToFront()
            end
        end
        for i = markers + 1, #self.markers do self.markers[i]:Hide() end
        local hole_count = 0
        for _, point in ipairs(holes) do
            local x, y = project(point)
            if x and math.abs(x) <= crop_width / 2 and math.abs(y) <= crop_height / 2 then
                hole_count = hole_count + 1
                local label = self.holes[hole_count]
                if not label then
                    label = root:AddChild(Text(ctx.G.BODYTEXTFONT, 18))
                    label:SetClickable(false)
                    self.holes[hole_count] = label
                end
                label:SetString(ctx:text("洞 ", "Hole ") .. point.number)
                label:SetPosition(x, y - 13)
                label:SetColour(unpack(colours[(point.number - 1) % #colours + 1]))
                label:Show()
            end
        end
        for i = hole_count + 1, #self.holes do self.holes[i]:Hide() end
        legend:SetString(ctx:text("实线已寻路 · 虚线待规划", "Solid: path · Dashed: unplanned")
            .. (snapshot.reason and ctx:text("\n未找到路线，请调整航点", "\nNo route found; adjust waypoints") or "")
            .. (limited and ctx:text("\n仅显示部分路线", "\nRoute partly displayed") or ""))
        legend:SetRegionSize(crop_width - 12, 38)
        legend:SetPosition(0, -crop_height / 2 + 23)
        legend:MoveToFront()
        if #snapshot.points == 0 then legend:Hide() else legend:Show() end
        self.used_lines, self.used_markers = used, markers
    end
    root:Hide()
    return self
end

return M
