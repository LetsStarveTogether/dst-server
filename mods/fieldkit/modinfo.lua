name = "Fieldkit · 随行工具箱"
description = "One client toolkit for interaction, construction, travel and readable information.\n统一的交互、施工、移动与信息工具箱。F6 打开，F7 停止；Alt 拖选。"
author = "Fieldkit contributors"
version = "0.15.0"
api_version = 10
dst_compatible = true
client_only_mod = true
all_clients_require_mod = false

local function text(zh, en)
    return (locale == "zh" or locale == "zhr" or locale == "zht") and zh or en
end

local switches = {
    { description = text("开启", "On"), data = true },
    { description = text("关闭", "Off"), data = false },
}
local keys = {
    { description = text("不绑定", "Unbound"), data = 0 },
    { description = "V", data = 118 },
    { description = "F5", data = 286 },
    { description = "F6", data = 287 },
    { description = "F7", data = 288 },
    { description = "F8", data = 289 },
    { description = "F9", data = 290 },
    { description = "F10", data = 291 },
    { description = "F11", data = 292 },
    { description = "F12", data = 293 },
}

configuration_options = {
    { name = "language", label = text("语言", "Language"), default = "auto", options = {
        { description = text("跟随游戏", "Game language"), data = "auto" },
        { description = "简体中文", data = "zh" },
        { description = "English", data = "en" },
    } },
    { name = "panel_key", label = text("打开工具箱", "Open toolkit"), default = 287, options = keys },
    { name = "cancel_key", label = text("停止当前操作", "Cancel operation"), default = 288, options = keys },
    { name = "gesture_key", label = text("按住表情轮盘", "Hold emote wheel"), default = 289, options = keys },
    { name = "range_key", label = text("固定目标范围", "Pin target range"), default = 290, options = keys },
    { name = "range_clear_key", label = text("清除固定范围", "Clear pinned ranges"), default = 286, options = keys },
    { name = "cooking_key", label = text("快速重复烹饪", "Quick repeat cooking"), default = 291, options = keys },
    { name = "grid_key", label = text("切换最近两种格型", "Swap recent grid shapes"), default = 118, options = keys },
    { name = "status_numbers", label = text("原生状态数字", "Native status numbers"), default = true, options = {
        { description = text("常驻当前值", "Always show current"), data = true },
        { description = text("当前 / 最大值", "Current / maximum"), data = "detailed" },
        { description = text("原生悬停显示", "Native hover"), data = false },
    } },
    { name = "information_hud", label = text("世界与角色信息", "World and player information"), default = true, options = switches },
    { name = "nightmare_indicator", label = text("梦魇阶段", "Nightmare phase"), default = "always", options = {
        { description = text("显示公开阶段", "Show public phase"), data = "always" },
        { description = text("持有铥矿徽章时", "When carrying Thulecite Medallion"), data = "medallion" },
        { description = text("关闭", "Off"), data = "off" },
    } },
    { name = "minimap", label = text("小地图", "Minimap"), default = true, options = switches },
    { name = "range_hints", label = text("范围提示", "Range hints"), default = true, options = switches },
    { name = "range_double_click", label = text("范围双击间隔", "Range double-click interval"), default = .3, options = {
        { description = "0.3s", data = .3 }, { description = "0.4s", data = .4 },
        { description = "0.5s", data = .5 }, { description = "0.6s", data = .6 }, { description = "0.7s", data = .7 },
    } },
    { name = "range_modifier", label = text("中键范围入口", "Middle-click ranges"), default = "none", options = {
        { description = text("关闭", "Off"), data = "off" },
        { description = text("无需修饰键", "None"), data = "none" },
        { description = "Ctrl", data = "ctrl" }, { description = "Alt", data = "alt" },
        { description = "Shift", data = "shift" },
    } },
    { name = "range_hover_modifier", label = text("世界悬停范围入口", "World hover ranges"), default = "none", options = {
        { description = text("关闭", "Off"), data = "off" },
        { description = text("无需修饰键", "None"), data = "none" },
        { description = "Ctrl", data = "ctrl" }, { description = "Alt", data = "alt" },
        { description = "Shift", data = "shift" },
    } },
    { name = "range_inventory", label = text("悬停库存物品显示范围", "Inventory hover ranges"), default = true, options = switches },
    { name = "range_preview", label = text("放置预览显示范围", "Placement preview ranges"), default = true, options = switches },
    { name = "boss_indicators", label = text("已观察 Boss 方向", "Observed boss directions"), default = true, options = switches },
    { name = "loot_hints", label = text("贵重掉落与蓝图提示", "Valuable loot and blueprint hints"), default = true, options = switches },
    { name = "loot_sound", label = text("贵重物品提示音", "Valuable item sound"), default = false, options = switches },
    { name = "inventory_scroll", label = text("库存滚轮操作", "Inventory wheel actions"), default = true, options = switches },
    { name = "inventory_middle", label = text("库存中键操作", "Inventory middle-click actions"), default = true, options = switches },
    { name = "inventory_reverse", label = text("反转库存滚轮", "Reverse inventory wheel"), default = false, options = switches },
    { name = "names", label = text("附近玩家名字", "Nearby player names"), default = true, options = switches },
    { name = "ride_timer", label = text("骑乘时间估计", "Estimated ride time"), default = true, options = switches },
    { name = "announcements", label = text("手动宣告", "Manual announcements"), default = true, options = switches },
    { name = "gestures", label = text("表情轮盘", "Emote wheel"), default = true, options = switches },
    { name = "ground", label = text("附近地面物品", "Nearby ground items"), default = true, options = switches },
    { name = "cooking", label = text("配料与烹饪提示", "Cooking assistance"), default = true, options = switches },
    { name = "work", label = text("交互与施工队列", "Interaction and construction queues"), default = true, options = switches },
    { name = "travel", label = text("移动与跟随", "Walking and following"), default = true, options = switches },
    { name = "grid", label = text("地面放置对齐", "Ground placement grid"), default = true, options = switches },
    { name = "reduce_shake", label = text("减弱镜头震动", "Reduce camera shake"), default = false, options = switches },
    { name = "reduce_distortion", label = text("减弱画面扭曲", "Reduce screen distortion"), default = false, options = switches },
    { name = "colour_filter", label = text("颜色滤镜", "Colour filter"), default = "native", options = {
        { description = text("原生", "Native"), data = "native" },
        { description = text("中和理智滤镜", "Neutral sanity colours"), data = "neutral_effects" },
        { description = text("中和全部颜色滤镜", "Neutral all colour layers"), data = "neutral_all" },
    } },
    { name = "snow_overlay", label = text("地面积雪上限", "Ground snow limit"), default = 1, options = {
        { description = text("原生", "Native"), data = 1 },
        { description = text("上限 60%", "Limit to 60%"), data = 0.6 },
        { description = text("隐藏", "Hidden"), data = 0 },
    } },
    { name = "sand_overlay", label = text("风沙遮罩", "Sandstorm overlay"), default = 1, options = {
        { description = text("原生", "Native"), data = 1 },
        { description = text("淡化至 35%", "Fade to 35%"), data = 0.35 },
        { description = text("隐藏", "Hidden"), data = 0 },
    } },
    { name = "text_scale", label = text("辅助信息文字尺度", "Assistant text scale"), default = 1, options = {
        { description = "100%", data = 1 }, { description = "115%", data = 1.15 },
        { description = "130%", data = 1.3 },
    } },
    { name = "selection_modifier", label = text("框选修饰键", "Selection modifier"), default = 400, options = {
        { description = "Alt", data = 400 }, { description = text("关闭鼠标框选", "Disable mouse selection"), data = 0 },
    } },
    { name = "selection_auto_execute", label = text("框选后立即执行", "Run after selection"), default = true, options = switches },
    { name = "queue_tools", label = text("自动使用库存工具", "Use tools from inventory"), default = true, options = switches },
    { name = "queue_craft_tools", label = text("缺工具时制作基础工具", "Craft missing basic tools"), default = false, options = switches },
    { name = "queue_collect", label = text("拾取其他工作掉落（鱼自动拾取）", "Collect work drops (fish always collected)"), default = false, options = switches },
    { name = "map_modifier", label = text("地图导航修饰键", "Map navigation modifier"), default = 400, options = {
        { description = "Alt", data = 400 }, { description = text("关闭地图快捷键", "Disable map shortcut"), data = 0 },
    } },
}
