local T = require("tests.client_mod.support")
local Base = {}

function Base._ctor(self, name)
    self.name, self.children, self.flow = name, {}, {}
    self.position = { x = 0, y = 0, z = 0 }
    self.shown = true
    self.inst = { entity = {}, IsValid = function() return not self.killed end }
end
function Base:AddChild(child)
    self.children[#self.children + 1], child.parent = child, self
    return child
end
function Base:RemoveChild(child)
    for i, value in ipairs(self.children) do
        if value == child then table.remove(self.children, i); child.parent = nil; return end
    end
end
function Base:KillAllChildren()
    for _, child in ipairs(self.children) do child:Kill() end
    self.children = {}
end
function Base:Kill() self.killed = true end
function Base:OnDestroy() self:Kill() end
function Base:Show() self.shown = true end
function Base:Hide() self.shown = false end
function Base:SetPosition(x, y, z)
    if type(x) == "table" then x, y, z = x.x, x.y, x.z end
    self.position = { x = x, y = y or 0, z = z or 0 }
end
function Base:GetWorldPosition() return self.position end
function Base:GetPosition() return self.position end
function Base:SetScale(x, y, z) self.scale = type(x) == "table" and x or { x = x, y = y or x, z = z or x } end
function Base:GetScale() return self.scale or { x = 1, y = 1, z = 1 } end
function Base:GetWorldScale() return self.test_scale and { x = self.test_scale, y = self.test_scale } or self:GetScale() end
function Base:SetSize(w, h) self.width, self.height = w, h end
function Base:GetSize() return self.width or 1024, self.height or 768 end
function Base:GetRegionSize() return self.width or 100, self.height or 30 end
function Base:SetText(value)
    self.value = value
    if type(self.text) == "table" then self.text:SetString(value) else self.text = value end
end
Base.SetString = Base.SetText
function Base:SetTruncatedString(value, width) self:SetString(value); self.width = width end
function Base:SetTexture(atlas, texture) self.atlas, self.texture = atlas, texture end
function Base:SetTint(...) self.tint = { ... } end
function Base:SetColour(...) self.colour = { ... } end
function Base:SetRotation(value) self.rotation = value end
function Base:SetClickable(value) self.clickable = value end
function Base:SetFocus() self.focus = true end
function Base:ClearFocus() self.focus = false end
function Base:OnGainFocus() self.focus = true end
function Base:OnLoseFocus() self.focus = false end
function Base:SetFocusChangeDir(direction, other) self.flow[direction] = other end
function Base:SetOnClick(fn) self.onclick = fn end
function Base:OnControl() return false end
function Base:OnRawKey() return false end
function Base:OnMouseButton() return false end
function Base:GetDeepestFocus() end
for _, name in ipairs({ "SetHAnchor", "SetVAnchor", "SetScaleMode", "SetFont", "SetTextSize", "SetHAlign",
    "SetRegionSize", "SetHRegPoint", "SetVRegPoint", "SetScissor", "SetMaxPropUpscale", "SetOffset",
    "StartUpdating", "StopUpdating", "OnBecomeActive", "OnBecomeInactive", "MoveToFront" }) do
    Base[name] = function() end
end

local Widget = T.Class(Base, Base._ctor)
function Widget.button()
    local button = Widget("button")
    button.text, button.image = Widget("text"), Widget("image")
    return button
end
function Widget.install()
    _G.Class = T.Class
    for _, name in ipairs({ "widget", "image", "text" }) do package.loaded["widgets/" .. name] = Widget end
    package.loaded["widgets/textbutton"] = Widget.button
end
return Widget
