local Config = require("fieldkit/core/config")
local M = {}

function M.new(ctx, commands)
    local self = { values = {}, conflicts = {} }
    local handlers, epoch = {}, 0
    local input, front, config, store = ctx.G.TheInput, ctx.G.TheFrontEnd, ctx.config, ctx.store
    local function valid(name, key)
        local option = config.key_options[name]
        for _, choice in ipairs(option and option.options or {}) do
            if choice.data == key then return true end
        end
        return false
    end
    local function clear()
        epoch = epoch + 1
        for _, handler in ipairs(handlers) do handler:Remove() end
        handlers = {}
    end
    local function editing()
        local focus = front:GetFocusWidget()
        return front.textProcessorWidget or focus and focus.editing
    end
    local function refresh()
        clear()
        local effective
        effective, self.conflicts = Config.resolve_keys(self.values)
        local generation = epoch
        for _, name in ipairs(Config.keys) do
            local key, command = effective[name], commands[name]
            config[name] = key
            if key ~= 0 and command then
                local armed
                handlers[#handlers + 1] = input:AddKeyDownHandler(key, function()
                    armed = nil
                    if generation ~= epoch or ctx.scope.closed or editing() then return end
                    armed = { screen = front:GetActiveScreen() }
                    if command.down then command.fn() end
                end)
                handlers[#handlers + 1] = input:AddKeyUpHandler(key, function()
                    local intent = armed
                    armed = nil
                    if generation ~= epoch or ctx.scope.closed or not intent or editing()
                        or intent.screen ~= front:GetActiveScreen() then return end
                    if not command.down then command.fn() end
                end)
            end
        end
    end
    function self:label(name)
        return config.key_options[name].label
    end
    function self:key_label(name)
        for _, choice in ipairs(config.key_options[name].options) do
            if choice.data == self.values[name] then return choice.description end
        end
    end
    function self:set(name, key)
        if not store.ready then return false, ctx:text("本地配置正在读取，请稍候", "Local profile is loading; wait a moment") end
        if not valid(name, key) then return false, ctx:text("无效快捷键", "Invalid shortcut") end
        if key ~= 0 then
            for _, other in ipairs(Config.keys) do
                if other ~= name and self.values[other] == key then
                    return false, ctx:text("快捷键已用于：", "Shortcut already used by: ") .. self:label(other)
                end
            end
        end
        self.values[name] = key
        local overrides = {}
        for _, id in ipairs(Config.keys) do
            if self.values[id] ~= config.key_defaults[id] then overrides[id] = self.values[id] end
        end
        store:set("bindings", overrides)
        refresh()
        return true
    end
    function self:reset()
        if not store.ready then return false, ctx:text("本地配置正在读取，请稍候", "Local profile is loading; wait a moment") end
        store:set("bindings", {})
        for _, name in ipairs(Config.keys) do self.values[name] = config.key_defaults[name] end
        refresh()
        return true
    end
    for _, name in ipairs(Config.keys) do self.values[name] = config.key_defaults[name] end
    refresh()
    ctx.scope:defer(clear)
    ctx.scope:defer(store:on_ready(function()
        local saved = store:get("bindings")
        if type(saved) == "table" then
            for _, name in ipairs(Config.keys) do
                if valid(name, saved[name]) then self.values[name] = saved[name] end
            end
        end
        refresh()
    end))
    return self
end

return M
