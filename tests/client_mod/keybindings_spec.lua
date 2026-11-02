local T = require("tests.client_mod.support")
local Config = require("fieldkit/core/config")
local Bindings = require("fieldkit/game/keybindings")
local schema = {}
setfenv(assert(loadfile("mods/fieldkit/modinfo.lua")), schema)()

local function fixture(saved, duplicate)
    local handlers, player = T.handlers(), T.entity()
    local state = { hits = {}, saved = saved, screen = {} }
    local store = { ready = false, get = function() return state.saved end,
        set = function(_, _, value) state.saved = value; return true end,
        on_ready = function(_, fn)
            state.loaded = fn
            return function() state.loaded = nil end
        end }
    local ctx = { player = player, store = store, config = Config.read(schema.configuration_options, function(name)
        if duplicate and name == "panel_key" then return 288 end
    end, "en"), scope = require("fieldkit/core/scope").new(player, error) }
    function ctx:text(_, en) return en end
    local input = {
        AddKeyDownHandler = function(_, key, fn) return handlers:add("down", key, fn) end,
        AddKeyUpHandler = function(_, key, fn) return handlers:add("up", key, fn) end,
        onkey = { HandleEvent = function() end },
        onkeydown = { HandleEvent = function(_, key) handlers:dispatch("down", key) end },
        onkeyup = { HandleEvent = function(_, key) handlers:dispatch("up", key) end },
    }
    local raw_key = T.eval("local Input = {}\n" .. assert(T.source("input"):match("function Input:OnRawKey%b().-\nend"))
        .. "\nreturn Input.OnRawKey")
    ctx.G = { TheInput = input, TheFrontEnd = { GetFocusWidget = function() return state.focus end,
        GetActiveScreen = function() return state.screen end } }
    local commands = {}
    for _, name in ipairs(Config.keys) do
        local id = name
        commands[name] = { down = name == "gesture_key", fn = function() state.hits[#state.hits + 1] = id end }
    end
    ctx.bindings = Bindings.new(ctx, commands)
    function state:key(key, down) raw_key(input, key, down) end
    function state:press(key) self:key(key, true); self:key(key, false) end
    function state:load() store.ready = true; self.loaded() end
    return ctx, state, handlers
end

T.case("profile load validates bindings and explains conflicts", function()
    local ctx, s = fixture({ panel_key = 288, gesture_key = "code", range_key = 999, injected = 287 })
    assert(not ctx.bindings:set("panel_key", 293) and s.saved.panel_key == 288)
    s:load()
    assert(ctx.config.panel_key == 0 and ctx.bindings.conflicts.panel_key == "cancel_key")
    assert(ctx.config.gesture_key == 289 and ctx.config.range_key == 290)
    s:press(288); assert(#s.hits == 1 and s.hits[1] == "cancel_key")
    local ok, reason = ctx.bindings:set("panel_key", 290)
    assert(not ok and reason:find(ctx.bindings:label("range_key"), 1, true))
    assert(ctx.bindings:set("panel_key", 293) and ctx.config.panel_key == 293)
    s:press(293); assert(s.hits[2] == "panel_key")
    assert(s.saved.panel_key == 293 and s.saved.injected == nil)
    assert(ctx.bindings:set("cancel_key", 0) and ctx.config.cancel_key == 0)
    s:press(288); assert(#s.hits == 2)
    assert(ctx.bindings:reset() and ctx.config.cancel_key == 288 and ctx.config.panel_key == 287 and next(s.saved) == nil)
    ctx.scope:close()
end)

T.case("rebinding cannot reuse held keys or stale callbacks", function()
    local ctx, s, handlers = fixture()
    s:load()
    s:key(287, true)
    local stale = handlers.list[#handlers.list - 1].fn
    assert(ctx.bindings:set("panel_key", 293))
    s:key(287, false); s:key(293, false); stale()
    assert(#s.hits == 0)
    s:press(293); assert(#s.hits == 1)
    s:key(293, true); s.screen = {}; s:key(293, false)
    assert(#s.hits == 1, "focus changes invalidate a key held on another screen")
    s.focus = { editing = true }; s:press(293); s:press(289)
    assert(#s.hits == 1)
    s.focus = nil; s:key(289, true)
    assert(s.hits[2] == "gesture_key")
    s:key(289, false); assert(#s.hits == 2)
    ctx.scope:close(); s:press(293)
    for _, handler in ipairs(handlers.list) do assert(handler.removed) end
    assert(#s.hits == 2 and s.loaded == nil)
end)

T.case("closing before profile load removes the callback", function()
    local ctx, s = fixture(nil, true)
    assert(ctx.bindings.conflicts.panel_key == "cancel_key")
    ctx.scope:close()
    assert(s.loaded == nil)
end)

T.case("panel changes keys and keeps conflicts visible", function()
    local ctx, s = fixture()
    s:load()
    local Base = require("tests.client_mod.support.widgets")
    Base.install()
    package.loaded["widgets/screen"] = Base
    ANCHOR_MIDDLE, SCALEMODE_PROPORTIONAL = 1, 1
    MOVE_UP, MOVE_DOWN, MOVE_LEFT, MOVE_RIGHT = 1, 2, 3, 4
    CONTROL_CANCEL, KEY_ESCAPE = 10, 27
    TheFrontEnd = { PopScreen = function(_, screen) screen:OnDestroy() end }
    for name, value in pairs(ctx.config) do if type(value) == "boolean" then ctx.config[name] = false end end
    ctx.params = require("fieldkit/core/parameters").new()
    local panel = require("fieldkit/ui/panel")(ctx)
    ctx.screen = panel
    panel.section, panel.binding = "binding_keys", "panel_key"
    local function choose(key)
        local choices = ctx.config.key_options.panel_key.options
        local rows = panel:Rows()
        for i, choice in ipairs(choices) do if choice.data == key then rows[i].fn(); return end end
        error("missing key choice")
    end
    choose(288)
    assert(panel.section == "binding_keys" and ctx.config.panel_key == 287)
    assert(panel.hint.text:find("Shortcut already used", 1, true))
    choose(293)
    assert(panel.section == "bindings" and ctx.config.panel_key == 293 and not panel.closed)
    assert(panel.default_focus.focus and s.saved.panel_key == 293)
    panel:OnControl(CONTROL_CANCEL, false)
    assert(panel.closed and ctx.screen == nil)
    ctx.scope:close()
end)

T.done()
