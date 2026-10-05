local M = {}

-- The Workshop options are the schema: no second table of drifting defaults.
function M.read(options, get, locale)
    local config = {}
    for _, option in ipairs(options) do
        local value = get(option.name)
        local accepted = false
        for _, choice in ipairs(option.options) do
            if value == choice.data then accepted = true; break end
        end
        if accepted then config[option.name] = value else config[option.name] = option.default end
    end
    if config.language == "auto" then
        config.language = locale and locale:sub(1, 2) == "zh" and "zh" or "en"
    end
    -- Duplicate bindings would otherwise execute two different intents on one press.
    local used = {}
    for _, name in ipairs({ "cancel_key", "panel_key", "gesture_key", "range_key", "range_clear_key", "cooking_key", "grid_key" }) do
        local key = config[name]
        if key and key ~= 0 then
            if used[key] then config[name] = 0 else used[key] = true end
        end
    end
    return config
end

return M
