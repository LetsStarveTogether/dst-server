local lua_root = assert(arg[1], "driver Lua root is required")
local native_root = assert(arg[3], "native DST scripts root is required")
package.path = lua_root .. "/?.lua;" .. native_root .. "/?.lua;" .. package.path

-- Exercise the game's actual console command, scheduler, and entity task cleanup.
require("class")
require("util")
require("scheduler")
Entity = {}
require("entityscript")
require("consolecommands")
local commands = require("dst_server.commands")
local tick = 0
local static_tick = 0
GetTick = function() return tick end
GetTime = function() return tick end
GetTickTime = function() return 1 end
GetStaticTick = function() return static_tick end
GetStaticTime = function() return static_tick end
toarrayornil = function(...) return select("#", ...) > 0 and { ... } or nil end

local messages = {}
TheNet = {
    Announce = function(_, message)
        messages[#messages + 1] = { message, tick }
    end,
}
TheWorld = setmetatable({ GUID = 1 }, { __index = EntityScript })
local function advance(seconds)
    for _ = 1, seconds do
        tick = tick + 1
        scheduler:OnTick(tick)
    end
end

assert(commands.announce({ message = "once" }) == true)
assert(#messages == 1 and messages[1][1] == "once" and messages[1][2] == 0)
assert(TheWorld.__announcementtask == nil)

commands.announce({ message = "repeat", count = 3, interval = 2 })
local first = TheWorld.__announcementtask
assert(first.limit == 3 and #messages == 1, "native repetition starts next simulation tick")
advance(6)
assert(#messages == 4)
for index, expected_tick in ipairs({ 1, 3, 5 }) do
    assert(messages[index + 1][1] == "repeat" and messages[index + 1][2] == expected_tick)
end
assert(first.fn == nil and first.list == nil and TheWorld.pendingtasks[first] == nil)
advance(10)
assert(#messages == 4, "finite repetition must stop without an SDK cancellation")

commands.announce({ message = "old", count = 3, interval = 2 })
local old = TheWorld.__announcementtask
commands.announce({ message = "replacement", count = 2, interval = 3 })
local replacement = TheWorld.__announcementtask
assert(old.fn == nil and TheWorld.pendingtasks[old] == nil)
commands.announce({ message = "interjection", count = 1, interval = 2 })
assert(TheWorld.__announcementtask == replacement and replacement.limit == 2)
advance(4)
assert(#messages == 7)
assert(messages[5][1] == "interjection")
assert(messages[6][1] == "replacement" and messages[7][1] == "replacement")
assert(replacement.fn == nil)

commands.announce({ message = "default interval", count = 2 })
local defaults = TheWorld.__announcementtask
assert(defaults.period == 30)
for _ = 1, 100 do
    static_tick = static_tick + 1
    staticScheduler:OnTick(static_tick)
end
assert(#messages == 7 and defaults.limit == 2, "native repeats pause with simulation time")

-- Validation must happen before a bad request can replace an active announcement.
for _, args in ipairs({
    { message = "", count = 2 },
    { message = "invalid", count = 0 },
    { message = "invalid", count = -1 },
    { message = "invalid", count = 1.5 },
    { message = "invalid", count = true },
    { message = "invalid", count = "2" },
    { message = "invalid", count = math.huge },
    { message = "invalid", count = 0 / 0 },
    { message = "invalid", count = 9007199254740992 },
    { message = "invalid", count = 1e100 },
    { message = "invalid", interval = 0 },
    { message = "invalid", interval = -1 },
    { message = "invalid", interval = true },
    { message = "invalid", interval = "2" },
    { message = "invalid", interval = math.huge },
    { message = "invalid", interval = 0 / 0 },
}) do
    assert(not pcall(commands.announce, args))
    assert(TheWorld.__announcementtask == defaults and defaults.limit == 2)
    assert(#messages == 7)
end

TheWorld:CancelAllPendingTasks()
advance(100)
assert(#messages == 7 and defaults.fn == nil, "world cleanup must cancel remaining repeats")
commands.announce({ message = "maximum count", count = 9007199254740991 })
assert(TheWorld.__announcementtask.limit == 9007199254740991)
TheWorld:CancelAllPendingTasks()
