local M = { LIMIT = 128 }

-- Session task choices; safety permissions remain in the Workshop configuration.
local choices = {
    search_radius = { default = 20, values = { 12, 20, 30 } },
    inventory_batch = { default = 5, values = { 1, 5, 10 } },
    fish_count = { default = 5, values = { 1, 5, 10, 20 } },
    fish_labels = { default = true, values = { true, false } },
    craft_count = { default = 5, values = { 1, 5, 10, 20 } },
    cooking_count = { default = 5, values = { 1, 5, 10, 20 } },
    cooking_harvest = { default = true, values = { true, false } },
    repeat_count = { default = 1, values = { 1, 5, 10, 20 } },
    clear_stumps = { default = false, values = { false, true } },
    farm_layout = { default = 3, values = { 2, 3, 4 } },
    ground_ignore_full = { default = false, values = { false, true } },
    follow_mode = { default = "trail", values = { "trail", "direct" } },
    follow_distance = { default = 1.5, values = { .5, 1.5, 2.5, 3.5 } },
    follow_keep_distance = { default = false, values = { false, true } },
    travel_cane = { default = false, values = { false, true } },
    travel_fast = { default = true, values = { true, false } },
    travel_avoid = { default = true, values = { true, false } },
    auto_repair = { default = false, values = { false, true } },
    wormhole_marks = { default = true, values = { true, false } },
    timer_hud = { default = true, values = { true, false } },
    grid_shape = { default = "square", values = { "square", "diamond", "hex", "hex45", "hex90", "hex135" } },
    grid_smart_spacing = { default = true, values = { true, false } },
    grid_size = { default = .5, values = { .25, .5, 1, 2, 4 } },
    announcement_channel = { default = "global", values = { "global", "local" } },
}

local names = {
    trail = { "历史轨迹", "Recorded trail" }, direct = { "直接靠近", "Direct approach" },
    square = { "方格", "Square" }, diamond = { "菱形", "Diamond" },
    hex = { "六角", "Hexagonal" }, hex45 = { "六角 45°", "Hexagonal 45°" },
    hex90 = { "六角 90°", "Hexagonal 90°" }, hex135 = { "六角 135°", "Hexagonal 135°" },
    global = { "公聊", "Global" }, ["local"] = { "附近", "Nearby" },
    [true] = { "开启", "On" }, [false] = { "关闭", "Off" },
}

function M.new()
    local params = {}
    for name, choice in pairs(choices) do params[name] = choice.default end
    return params
end

function M.set(params, name, value)
    local choice = choices[name]
    for _, allowed in ipairs(choice and choice.values or {}) do
        if value == allowed then params[name] = value; return true end
    end
    return false
end

function M.cycle(params, name)
    local choice = choices[name]
    if not choice then return false end
    for index, value in ipairs(choice.values) do
        if value == params[name] then return M.set(params, name, choice.values[index % #choice.values + 1]) end
    end
    return M.set(params, name, choice.default)
end

function M.label(ctx, name)
    local value = ctx.params[name]
    if name == "farm_layout" then
        return value == 4 and ctx:text("密排（跨地块连续）", "Dense continuous rows") or value .. "×" .. value
    end
    local text = names[value]
    return text and ctx:text(text[1], text[2]) or tostring(value)
end

return M
