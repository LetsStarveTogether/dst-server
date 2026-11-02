local M = {}
M.keys = { "cancel_key", "panel_key", "gesture_key", "range_key", "range_clear_key", "cooking_key", "grid_key" }

function M.resolve_keys(values)
    local effective, conflicts, used = {}, {}, {}
    for _, name in ipairs(M.keys) do
        local key = values[name] or 0
        effective[name] = key
        if key ~= 0 then
            if used[key] then effective[name], conflicts[name] = 0, used[key]
            else used[key] = name end
        end
    end
    return effective, conflicts
end

-- The Workshop options are the schema: no second table of drifting defaults.
function M.read(options, get, locale)
    local config = { key_defaults = {}, key_options = {} }
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
    for _, name in ipairs(M.keys) do
        config.key_defaults[name] = config[name] or 0
        for _, option in ipairs(options) do
            if option.name == name then config.key_options[name] = option; break end
        end
    end
    for name, value in pairs(M.resolve_keys(config.key_defaults)) do config[name] = value end
    return config
end

return M
