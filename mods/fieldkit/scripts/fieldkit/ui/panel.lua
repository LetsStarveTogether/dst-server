local Parameters = require("fieldkit/core/parameters")
local Screen = require("widgets/screen")
local Widget = require("widgets/widget")
local Text = require("widgets/text")
local Image = require("widgets/image")
local TextButton = require("widgets/textbutton")
local Query = require("fieldkit/game/query")
local Metrics = require("fieldkit/game/metrics")
local AnnouncementData = require("fieldkit/game/announcement_data")

local Panel = Class(Screen, function(self, ctx, intent)
    Screen._ctor(self, "FieldkitPanel")
    self.ctx, self.intent = ctx, intent or {}
    self.page, self.section = 1, "tools"
    self.root = self:AddChild(Widget("fieldkit_panel"))
    self.root:SetHAnchor(ANCHOR_MIDDLE)
    self.root:SetVAnchor(ANCHOR_MIDDLE)
    self.root:SetScaleMode(SCALEMODE_PROPORTIONAL)
    self.background = self.root:AddChild(Image("images/global.xml", "square.tex"))
    self.background:SetSize(710, 560)
    self.background:SetTint(.055, .065, .075, .97)
    self.title = self.root:AddChild(Text(UIFONT, 32))
    self.title:SetPosition(0, 245)
    self.hint = self.root:AddChild(Text(BODYTEXTFONT, 20))
    self.hint:SetPosition(0, -247)
    self.content = self.root:AddChild(Widget("fieldkit_content"))
    self:Rebuild()
end)

function Panel:OnDestroy()
    self.closed = true
    if self.ctx.screen == self then self.ctx.screen = nil end
    Panel._base.OnDestroy(self)
end

function Panel:Close()
    if self.closed then return end
    self.closed = true
    if self.ctx.screen == self then self.ctx.screen = nil end
    TheFrontEnd:PopScreen(self)
end

function Panel:Execute(fn)
    self:Close()
    if self.ctx.scope.closed then return end
    local ok, success, reason = pcall(fn)
    if not ok then
        self.ctx:report(success)
        self.ctx:notify(self.ctx:text("操作失败，详见日志", "Operation failed; see client log"))
    elseif reason then
        self.ctx:notify(reason)
    end
end

function Panel:Button(label, x, y, fn)
    local button = self.content:AddChild(TextButton())
    button:SetFont(BODYTEXTFONT)
    button:SetTextSize(23 * self.ctx.config.text_scale)
    button:SetText(label)
    button.text:SetTruncatedString(label, 650, nil, true)
    button.image:SetSize(button.text:GetRegionSize())
    button:SetPosition(x, y)
    button:SetOnClick(fn)
    self.buttons[#self.buttons + 1] = button
    return button
end

local sections = {}

function Panel:Add(label, fn)
    self.entries[#self.entries + 1] = { label = label, fn = fn }
end

function Panel:Action(zh, en, fn)
    self:Add(self.ctx:text(zh, en), function() self:Execute(fn) end)
end

function Panel:PointAction(zh, en, fn)
    self:Action(zh, en, function()
        local point = self.intent.position and self.intent.position:GetPosition()
        if not point then return false, self.ctx:text("所选位置或平台已不可用", "Selected position or platform is no longer available") end
        return fn(point)
    end)
end

function Panel:Parameter(name, zh, en)
    self:Add(self.ctx:text(zh, en) .. ": " .. Parameters.label(self.ctx, name) .. " ›", function()
        if self.closed or self.ctx.scope.closed then return end
        if name == "grid_shape" then self.ctx.placement:cycle()
        else Parameters.cycle(self.ctx.params, name) end
        self.focus_parameter = name
        self:Rebuild()
    end)
    self.entries[#self.entries].parameter = name
end

function Panel:Section(id, zh, en)
    self:Add(self.ctx:text(zh, en), function()
        self.section, self.page = id, 1
        self:Rebuild()
    end)
end

function Panel:CustomRange(group)
    local ctx, target = self.ctx, self.intent.target or self.ctx.player
    self:Execute(function()
        local dialog
        local function close()
            if ctx.screen == dialog then ctx.screen = nil; dialog:Close() end
        end
        local function submit()
            if ctx.screen ~= dialog then return end
            local radius = tonumber(dialog:GetActualString())
            local method = group and "custom_group" or "custom"
            close()
            self:Execute(function()
                local ok, reason = ctx.awareness[method](ctx.awareness, radius, target)
                return ok, reason or not ok and ctx:text("请输入有效半径并选择可见目标", "Enter a valid radius and select a visible target") or nil
            end)
        end
        dialog = require("screens/redux/inputdialog")(
            ctx:text("自定义范围半径（0.1–128 米）", "Custom radius (0.1–128 metres)"), {
                { text = ctx:text("显示", "Show"), cb = submit },
                { text = ctx:text("取消", "Cancel"), cb = close },
            }, true)
        dialog:SetValidChars("0123456789.")
        dialog:OverrideText("4")
        dialog.edit_text:SetTextLengthLimit(8)
        local destroy = dialog.OnDestroy
        dialog.OnDestroy = function(screen, ...)
            if ctx.screen == screen then ctx.screen = nil end
            return destroy(screen, ...)
        end
        ctx.screen = dialog
        TheFrontEnd:PushScreen(dialog)
        return true
    end)
end

sections.tools = function(self)
    local ctx = self.ctx
    if ctx.config.work then self:Section("work", "交互与选择", "Interaction and selection") end
    if ctx.config.work then self:Section("construction", "施工、农田与制作", "Construction, farming and crafting") end
    if ctx.config.work then self:Section("drying", "晾晒收成与补挂", "Drying: harvest and refill") end
    if ctx.config.work then self:Section("fishing", "自动海钓", "Ocean fishing") end
    if ctx.config.work then self:Section("repair", "自动维修与补充燃料", "Automatic repair and refuelling") end
    if ctx.config.travel then self:Section("travel", "移动、路线与跟随", "Movement, routes and following") end
    if ctx.config.cooking then self:Section("cooking", "配料与多锅烹饪", "Ingredients and batch cooking") end
    if ctx.ground_dock then
        self:Action("控制附近物品列表", "Control nearby item list", function() return ctx.ground_dock:focus() end)
        self:Action("展开 / 收起附近物品列表", "Show / hide nearby item list", function() return ctx.ground_dock:toggle() end)
    end
    if ctx.config.grid then self:Section("placement", "几何放置设置", "Geometry settings") end
    if ctx.config.announcements or ctx.config.range_hints or ctx.minimap or ctx.journal or ctx.presentation then
        self:Section("information", "信息与显示", "Information and display")
    end
    if ctx.config.gestures then
        self:Action("表情轮盘", "Emote wheel", function() return ctx:open_emotes() end)
        self:Section("emote_favourites", "设置八格常用表情", "Set eight favourite emotes")
    end
    if ctx.config.inventory_scroll or ctx.config.inventory_middle then
        self:Parameter("inventory_batch", "库存批量操作数量", "Inventory batch amount")
    end
end

sections.emote_favourites = function(self)
    local ctx = self.ctx
    self:Add(ctx:text("轮盘优先显示常用表情：", "Show favourites first: ")
        .. (ctx.emotes.enabled and ctx:text("开启", "On") or ctx:text("关闭", "Off")), function()
        ctx.emotes:toggle_favourites(); self:Rebuild()
    end)
    for i = 1, 8 do
        local index = i
        self:Add(index .. " · " .. ctx.emotes:favourite_label(index), function()
            self.emote_slot, self.section, self.page = index, "emote_slot", 1
            self:Rebuild()
        end)
    end
end

sections.emote_slot = function(self)
    local ctx = self.ctx
    local function assign(name)
        ctx.emotes:set_favourite(self.emote_slot, name)
        self.section, self.page = "emote_favourites", 1
        self:Rebuild()
    end
    self:Add(ctx:text("清空此格", "Clear this slot"), function() assign(nil) end)
    for _, entry in ipairs(ctx.emotes:entries()) do
        local name = entry.name
        self:Add(entry.label, function() assign(name) end)
    end
end

sections.work = function(self)
    local ctx = self.ctx
    self:Action("对所指目标及同类执行主动作", "Primary action on target and nearby matches", function() return ctx.work:start(self.intent.target) end)
    self:Action("切换所指目标的左键选择", "Toggle target's left-click selection", function() return ctx.selection:toggle(self.intent.target, "left") end)
    self:Action("切换所指目标的右键选择", "Toggle target's right-click selection", function() return ctx.selection:toggle(self.intent.target, "right") end)
    self:PointAction("记录区域起点", "Mark area start", function(point) return ctx.selection:mark_start(point) end)
    self:PointAction("记录区域终点", "Mark area end", function(point) return ctx.selection:mark_end(point) end)
    self:Action("执行当前选择", "Execute selection", function() return ctx.selection:commit() end)
    self:Action("清空选择", "Clear selection", function() ctx.selection:clear(); return true end)
    self:Parameter("search_radius", "附近任务范围（米）", "Nearby task radius (m)")
    self:Parameter("fish_count", "每池塘钓获上限", "Catches per pond")
    self:Parameter("repeat_count", "每个目标最多投入次数", "Maximum applications per target")
    self:Parameter("clear_stumps", "砍树完成后挖树根", "Dig stumps after chopping")
end

sections.fishing = function(self)
    local ctx = self.ctx
    self:PointAction("在所指海面连续海钓", "Fish repeatedly at selected water", function(point) return ctx.fishing:start(point) end)
    self:Parameter("fish_count", "本次海钓钓获上限", "Catch limit for this fishing session")
    self:Parameter("fish_labels", "季节鱼名称与季节颜色", "Seasonal fish names and colours")
end

sections.repair = function(self)
    local ctx = self.ctx
    self:Parameter("auto_repair", "本次游戏自动维修（停止键可关闭）", "Auto repair this session (Stop disables)")
    for _, item in ipairs(ctx.repair:targets()) do
        local target = item
        local percent = ctx.repair:rule(item.prefab).percent
        self:Add(string.format("%s · %d%%", item:GetDisplayName(), percent), function()
            self.repair_target, self.section, self.page = target, "repair_item", 1
            self:Rebuild()
        end)
    end
end

sections.repair_item = function(self)
    local ctx, item = self.ctx, self.repair_target
    if not item or not item:IsValid() then return end
    local rule = ctx.repair:rule(item.prefab)
    self:Add(item:GetDisplayName() .. ctx:text(" · 维修阈值：", " · Repair below: ") .. rule.percent .. "% ›", function()
        local choices, next_value = { 0, 10, 25, 50, 75 }, 0
        for _, value in ipairs(choices) do if value > rule.percent then next_value = value; break end end
        ctx.repair:set_percent(item.prefab, next_value)
        self:Rebuild()
    end)
    for _, material in ipairs(ctx.repair:materials(item)) do
        local selected = material
        self:Add((material.enabled and "✓ " or "○ ") .. material.name, function()
            ctx.repair:toggle_material(item.prefab, selected.prefab)
            self:Rebuild()
        end)
    end
    self:Section("repair", "选择其他物品（0% 关闭；勾选允许消耗的材料）", "Other items (0% disables; tick allowed materials)")
end

sections.construction = function(self)
    local ctx = self.ctx
    local function region(kind) return ctx.construction:area(ctx.selection:area(), kind, self.intent.placement) end
    self:PointAction("记录施工区域起点", "Mark construction area start", function(point) return ctx.selection:mark_start(point, "right") end)
    self:PointAction("记录施工区域终点", "Mark construction area end", function(point) return ctx.selection:mark_end(point, "right") end)
    self:Action("在区域内部署手中物品", "Deploy held item across area", function() return region("deploy") end)
    self:Action("在区域内耕地", "Till selected area", function() return region("till") end)
    self:Action("在区域内挖地皮", "Terraform selected area", function() return region("terraform") end)
    self:Action("在每格丢下一件手中物品", "Drop one held item per grid point", function() return region("drop") end)
    self:Action("按地块浇水", "Water selected farm tiles", function() return region("water") end)
    self:Action("按地块施肥", "Fertilize selected farm tiles", function() return region("fertilize") end)
    self:Action("在区域内建造当前预览建筑", "Build previewed structure across area", function() return region("build") end)
    self:Action("重复最近制作配方 ×" .. ctx.params.craft_count, "Repeat last recipe ×" .. ctx.params.craft_count,
        function() return ctx.construction:repeat_recipe(ctx.params.craft_count) end)
    self:Parameter("craft_count", "批量制作次数", "Crafting repetitions")
    self:Parameter("repeat_count", "每地块水肥次数", "Water / fertilizer applications per tile")
    self:Parameter("farm_layout", "耕地排列", "Tilling layout")
end

sections.placement = function(self)
    local ctx = self.ctx
    self:Action("开关几何吸附", "Toggle snapping", function() ctx.placement:toggle(); return true end)
    self:Parameter("grid_shape", "放置格型", "Grid shape")
    self:Parameter("grid_smart_spacing", "按物品自动选择格距", "Use native item spacing")
    self:Parameter("grid_size", "手动格距（米）", "Manual grid spacing (m)")
    self:Action("网格旋转 15°", "Rotate grid 15°", function() ctx.placement:rotate(15); return true end)
    self:PointAction("将所指位置设为原点", "Set origin at selected point", function(point) return ctx.placement:set_origin(point) end)
    self:Action("恢复默认原点", "Reset grid origin", function() ctx.placement:reset_origin(); return true end)
end

sections.cooking = function(self)
    local ctx = self.ctx
    self:Action("记住当前锅中的完整配料", "Remember this pot's ingredients", function() return ctx.cooking:remember() end)
    self:Action("记录物品栏前格的配方", "Remember recipe from first inventory slots",
        function() return ctx.cooking:remember_inventory("inventory", 1) end)
    self:Action("记录背包前格的配方", "Remember recipe from first backpack slots",
        function() return ctx.cooking:remember_inventory("backpack", 1) end)
    self:Action("快速重复所指或已打开的锅", "Quick repeat selected or open pot",
        function() return ctx.cooking:quick_repeat(self.intent.target) end)
    self:Action("填入一份记忆配料", "Fill one remembered recipe", function() return ctx.cooking:fill() end)
    self:Action("填料并烹饪一份", "Fill and cook once", function() return ctx.cooking:repeat_once() end)
    self:Action("预览当前锅的候选菜品", "Preview this pot's possible dishes", function() return ctx.cooking:preview() end)
    self:Action("开始烹饪当前锅", "Cook this pot", function() return ctx.cooking:cook() end)
    self:Action("附近多锅烹饪 ×" .. ctx.params.cooking_count, "Cook nearby pots ×" .. ctx.params.cooking_count,
        function() return ctx.cooking:batch(self.intent.target, ctx.params.cooking_count) end)
    self:Action("收获附近已完成的锅", "Harvest finished nearby pots", function() return ctx.cooking:harvest() end)
    self:Parameter("cooking_count", "多锅烹饪份数", "Batch cooking amount")
    self:Parameter("cooking_harvest", "烹饪后收获（关闭时仅开火）", "Harvest cooked dishes (off: start cooking only)")
    self:Parameter("search_radius", "附近任务范围（米）", "Nearby task radius (m)")
end

sections.drying = function(self)
    for _, scope in ipairs({
        { "target", "所指晾晒架", "Selected rack" },
        { "area", "选区内的晾晒架", "Racks in selected area" },
        { "nearby", "附近晾晒架", "Nearby racks" },
    }) do
        local selected = scope[1]
        self:Add(self.ctx:text(scope[2], scope[3]) .. " ›", function()
            self.drying_scope, self.section, self.page = selected, "drying_food", 1
            self:Rebuild()
        end)
    end
    self:Parameter("search_radius", "附近任务范围（米）", "Nearby task radius (m)")
end

sections.drying_food = function(self)
    local ctx = self.ctx
    local function start(prefab)
        if self.drying_scope == "target" then return ctx.drying:start({ self.intent.target }, prefab) end
        if self.drying_scope == "area" then return ctx.drying:area(ctx.selection:area(), prefab) end
        return ctx.drying:nearby(prefab)
    end
    self:Action("只收成，保留未晾好的原料", "Harvest only; keep unfinished food", function() return start() end)
    for _, ingredient in ipairs(ctx.drying:ingredients()) do
        local prefab = ingredient.prefab
        self:Add(ctx:text("收成并补挂：", "Harvest and refill: ") .. ingredient.item:GetDisplayName()
            .. " ×" .. ingredient.count, function() self:Execute(function() return start(prefab) end) end)
    end
    self:Section("drying", "重新选择晾晒架范围", "Choose rack scope again")
end

sections.travel = function(self)
    local ctx = self.ctx
    self:PointAction("走向打开面板前所指位置", "Walk to selected point", function(point) return ctx.travel:walk(point) end)
    self:PointAction("将所指位置加入路线", "Add selected point to route", function(point) return ctx.travel:add_waypoint(point) end)
    self:Action("执行已记录路线", "Follow recorded waypoints", function() return ctx.travel:navigate() end)
    self:Action("清空路线点", "Clear waypoints", function() ctx.travel:clear_waypoints(); return true end)
    if ctx.travel:can_follow(self.intent.target) then
        self:Action("跟随所指目标 · " .. Parameters.label(ctx, "follow_mode"),
            "Follow selected target · " .. Parameters.label(ctx, "follow_mode"),
            function() return ctx.travel:follow(self.intent.target) end)
    end
    self:Section("players", "跟随 → 选择附近玩家", "Follow → choose nearby player")
    self:Section("creatures", "跟随 → 选择附近生物", "Follow → choose nearby creature")
    if ctx.travel:can_push(self.intent.target) then
        self:Action("推挤所指目标（最多 30 秒）", "Push selected target (up to 30 seconds)", function() return ctx.travel:push(self.intent.target) end)
    end
    self:Parameter("follow_mode", "跟随方式", "Following mode")
    self:Parameter("follow_distance", "跟随碰撞边缘间距（米）", "Following collision-edge gap (m)")
    self:Parameter("follow_keep_distance", "目标接近时主动后退", "Back away when the target approaches")
    self:Parameter("travel_cane", "移动任务借用手杖，到达后恢复", "Borrow a cane while travelling; restore on arrival")
    self:Parameter("travel_fast", "优先道路和加速地皮", "Prefer roads and faster terrain")
    self:Parameter("travel_avoid", "绕开已观察到的危险区域", "Avoid observed dangers")
    self:Parameter("search_radius", "附近任务范围（米）", "Nearby task radius (m)")
end

sections.players = function(self)
    local ctx = self.ctx
    local players = self.section == "players"
    local candidates = Query.nearby(ctx, ctx.params.search_radius, players and { "player" } or nil,
        not players and { "INLIMBO", "NOCLICK", "player", "FX", "DECOR" } or nil,
        not players and { "locomotor", "balloon" } or nil,
        function(entity) return ctx.travel:can_follow(entity) end)
    for _, entity in ipairs(candidates) do
        local target = entity
        self:Add(entity:GetDisplayName(), function() self:Execute(function() return ctx.travel:follow(target) end) end)
    end
end

sections.creatures = sections.players

sections.information = function(self)
    local ctx = self.ctx
    if ctx.presentation then self:Parameter("fish_labels", "季节鱼名称与季节颜色", "Seasonal fish names and colours") end
    if ctx.timers then self:Section("timers", "效果、天气与 Boss 计时记录", "Effects, weather and boss records") end
    if ctx.journal then self:Section("rides", "骑乘记录与驯化估算", "Riding history and domestication estimates") end
    if ctx.wormholes then
        self:Parameter("wormhole_marks", "已穿越虫洞的配对编号", "Visited wormhole pair numbers")
        self:Action("清除本世界虫洞记录", "Clear wormhole records for this world", function() ctx.wormholes:clear(); return true end)
    end
    if ctx.config.announcements then
        self:Parameter("announcement_channel", "宣告频道（Ctrl 临时切换）", "Chat channel (Ctrl toggles)")
        self:Action("宣告自己的状态", "Announce my status", function() return ctx.announcements:status() end)
        self:Action("宣告体温", "Announce body temperature", function() return ctx.announcements:metric("temperature") end)
        self:Action("宣告潮湿", "Announce moisture", function() return ctx.announcements:metric("moisture") end)
        self:Action("宣告梦魇阶段", "Announce nightmare phase", function() return ctx.announcements:nightmare() end)
        self:Action("宣告骑乘状态", "Announce riding status", function() return ctx.announcements:ride() end)
        self:Action("宣告自己的位置", "Announce my position", function() return ctx.announcements:position() end)
        self:Action("宣告所指目标", "Announce selected target", function() return ctx.announcements:target(self.intent.target) end)
        self:Action("宣告季节与世界时间", "Announce season and world time", function() return ctx.announcements:world() end)
        self:Action("宣告当前手持物品", "Announce equipped item", function() return ctx.announcements:equipment() end)
        self:Section("metrics", "宣告角色、伙伴与船只状态", "Announce character, companion and boat status")
        if #AnnouncementData.circuits(ctx) > 0 then
            self:Section("circuits", "宣告已安装的芯片", "Announce an installed circuit")
        end
    end
    if ctx.config.range_hints then
        self:Action("固定 / 取消所指目标的范围", "Pin / unpin selected target's range", function() return ctx.awareness:toggle_pin(self.intent.target) end)
        self:Action("固定 / 取消附近同类范围", "Pin / unpin nearby matching ranges", function() return ctx.awareness:toggle_group(self.intent.target) end)
        self:Add(ctx:text("自定义所指目标的范围半径", "Custom range on selected target"), function() self:CustomRange(false) end)
        self:Add(ctx:text("自定义附近同类的范围半径", "Custom range on nearby matching targets"), function() self:CustomRange(true) end)
        self:Action("清除固定范围", "Clear pinned ranges", function() ctx.awareness:clear_pins(); return true end)
    end
    if ctx.minimap then
        self:Action("展开小地图", "Expand minimap", function() return ctx.minimap:set_open(true) end)
        self:Action("折叠小地图", "Collapse minimap", function() return ctx.minimap:set_open(false) end)
        self:Action("小地图回到角色位置", "Recenter minimap", function() return ctx.minimap:recenter() end)
        self:Action("控制小地图", "Control minimap", function() return ctx.minimap:focus() end)
    end
end

sections.rides = function(self)
    local ctx = self.ctx
    self:Add(ctx:text("驯化值按上牛时刻估算，服主修改规则会影响结果", "Estimates refer to mount time; server rules may differ"), function() end)
    local records = ctx.journal:records("ride")
    for i = #records, 1, -1 do
        local record = records[i]
        local value = record.low and record.high and string.format("≈%.0f–%.0f%%", record.low * 100, record.high * 100)
            or ctx:text("无法估算", "Unknown")
        local reason = record.state == "dismounted" and ctx:text(" · 主动下牛", " · Dismounted")
            or record.state == "interrupted" and ctx:text(" · 观察中断", " · Interrupted") or ""
        self:Add(ctx:text("第 ", "Day ") .. string.format("%.1f", record.at / ctx.G.TUNING.TOTAL_DAY_TIME + 1)
            .. " · " .. (record.name or record.prefab or "?") .. " · " .. string.format("%.1fs", record.duration or 0)
            .. " · " .. value .. reason, function() end)
    end
    self:Action("清除本世界骑乘记录", "Clear riding history for this world", function()
        ctx.journal:clear("ride"); ctx.journal:flush(); return true
    end)
end

sections.timers = function(self)
    local ctx = self.ctx
    self:Parameter("timer_hud", "HUD 效果与天气倒计时", "Effects and weather on HUD")
    self:Add(ctx.timers:weather_label(), function() self:Rebuild() end)
    local snapshot = ctx.timers:snapshot()
    for _, buff in ipairs(snapshot.buffs) do
        self:Add(buff.label .. " ≈" .. ctx.timers:duration(buff.remaining) .. " · " .. (buff.source or ""), function() self:Rebuild() end)
    end
    for _, boss in ipairs(snapshot.bosses) do
        local description
        if boss.state == "seen" then description = ctx:text("已再次观察到", "Observed again")
        elseif boss.high <= 0 then description = ctx:text("预计已可刷新，待确认", "May be available; unconfirmed")
        else description = "≈" .. ctx.timers:duration(boss.low)
            .. (boss.high > boss.low and "–" .. ctx.timers:duration(boss.high) or "") end
        self:Add(boss.label .. " · " .. description
            .. (boss.state == "winter" and ctx:text("（冬季）", " (winter)") or ""), function() self:Rebuild() end)
    end
    self:Section("weather_history", "天气变化记录", "Weather history")
    self:Action("清除本世界 Boss 计时记录", "Clear boss records for this world", function()
        ctx.journal:clear("boss"); ctx.journal:flush(); return true
    end)
end

sections.weather_history = function(self)
    local ctx = self.ctx
    local names = { none = { "降水结束", "Precipitation ended" }, rain = { "开始降雨", "Rain began" },
        snow = { "开始降雪", "Snow began" }, acidrain = { "开始酸雨", "Acid rain began" }, lunarhail = { "开始月雹", "Lunar hail began" } }
    local records = ctx.journal:records("weather")
    for i = #records, 1, -1 do
        local record = records[i]
        local name = names[record.state] or { "天气变化", "Weather changed" }
        self:Add(ctx:text("第 ", "Day ") .. string.format("%.1f", record.at / ctx.G.TUNING.TOTAL_DAY_TIME + 1)
            .. " · " .. ctx:text(name[1], name[2]), function() end)
    end
end

sections.metrics = function(self)
    local ctx, own = self.ctx, self.ctx.information:snapshot().own
    for _, key in ipairs(Metrics.order) do
        local metric, state = key, own[key]
        if key ~= "health" and key ~= "hunger" and key ~= "sanity" and key ~= "moisture"
            and key ~= "temperature" and state and state.quality == "known" then
            self:Add(ctx:text(state.label[1], state.label[2]), function()
                self:Execute(function() return ctx.announcements:metric(metric) end)
            end)
        end
    end
end

sections.circuits = function(self)
    local ctx = self.ctx
    for _, chip in ipairs(AnnouncementData.circuits(ctx)) do
        local target = { bar = chip.bar, index = chip.index, netid = chip.netid }
        self:Add(ctx:text("电路 ", "Circuit ") .. (chip.bar + 1) .. " · " .. chip.name, function()
            self:Execute(function() return ctx.announcements:circuit(target) end)
        end)
    end
end

function Panel:Rows()
    self.entries = {}
    if sections[self.section] then sections[self.section](self) end
    return self.entries
end

function Panel:Rebuild()
    self.content:KillAllChildren()
    self.buttons = {}
    local ctx = self.ctx
    local rows = self:Rows()
    local pages = math.max(1, math.ceil(#rows / 8))
    self.page = math.max(1, math.min(self.page, pages))
    self.title:SetString("Fieldkit · " .. self.page .. "/" .. pages)
    local parameter_focus
    for i = 1, 8 do
        local entry = rows[(self.page - 1) * 8 + i]
        if entry then
            local x, y = 0, 192 - (i - 1) * 47
            local button = self:Button(entry.label, x, y, entry.fn)
            if entry.parameter and entry.parameter == self.focus_parameter then parameter_focus = button end
        end
    end
    if #rows == 0 then
        local empty = self.content:AddChild(Text(BODYTEXTFONT, 24))
        empty:SetString(ctx:text("目前没有可用项目", "No available entries"))
    end
    if self.page > 1 then
        self:Button(ctx:text("上一页", "Previous"), -235, -210, function() self.page = self.page - 1; self:Rebuild() end)
    end
    self:Button(ctx:text("关闭", "Close"), 0, -210, function() self:Close() end)
    if self.page < pages then
        self:Button(ctx:text("下一页", "Next"), 235, -210, function() self.page = self.page + 1; self:Rebuild() end)
    end
    if self.section ~= "tools" then
        self:Button(ctx:text("工具箱", "Toolkit"), 0, -175, function()
            self.section, self.page = "tools", 1; self:Rebuild()
        end)
    end
    self.hint:SetString(ctx:text("方向键 / 手柄选择 · › 调整下次任务 · 返回关闭", "Arrows / D-pad to select · › changes next task · Cancel to close"))
    for i, button in ipairs(self.buttons) do
        local previous = self.buttons[(i - 2) % #self.buttons + 1]
        local next_button = self.buttons[i % #self.buttons + 1]
        button:SetFocusChangeDir(MOVE_UP, previous)
        button:SetFocusChangeDir(MOVE_LEFT, previous)
        button:SetFocusChangeDir(MOVE_DOWN, next_button)
        button:SetFocusChangeDir(MOVE_RIGHT, next_button)
    end
    self.default_focus, self.focus_parameter = parameter_focus or self.buttons[1], nil
    if self.default_focus then self.default_focus:SetFocus() end
end

function Panel:OnControl(control, down)
    if control == CONTROL_CANCEL then
        if not down then self:Close() end
        return true
    end
    if Panel._base.OnControl(self, control, down) then return true end
    -- This is an active screen; unhandled controls must not reach the character.
    return true
end

function Panel:OnRawKey(key, down)
    if key == KEY_ESCAPE then
        if not down then self:Close() end
        return true
    end
    return Panel._base.OnRawKey(self, key, down)
end

return Panel
