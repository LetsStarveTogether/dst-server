local root, handlers, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
local scripts = os.getenv("DST_SERVER_TEST_SCRIPTS") or root .. "/dst-scripts/scripts"
package.path = root .. "/src/dst_server/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
json = require("json")
local state = require("dst_server.state")
state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 4
state.requested_profile = profile
state.telemetry_active = profile ~= "off"
TheWorld = { meta = { session_identifier = "SESSION" }, state = { cycles = 2 } }
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local outputs = {}
nolineprint = function(line) outputs[#outputs + 1] = line:sub(10) end

local command = { name = "rollback", hash = 42, vote = true }
package.loaded.usercommands = {
    GetCommandFromHash = function() return command end,
    GetCommandFromName = function() return command end,
}
STRINGS = { UI = {
    BUILTINCOMMANDS = {},
    NOTIFICATION = {
        JOINEDGAME = "%s joined", KICKEDFROMGAME = "%s kicked", BANNEDFROMGAME = "%s banned",
        DICEROLLED = "%s rolled %s out of %d", DEFAULTVOTENAMEFMT = "Rollback %s",
        DEFAULTVOTEPASSEDFMT = "%s passed", DEFAULTVOTEFAILEDFMT = "%s failed",
    },
} }
local displayed = 0
ChatHistory = {
    OnSay = function() displayed = displayed + 1 end,
    OnAnnouncement = function() displayed = displayed + 1 end,
    OnSkinAnnouncement = function() displayed = displayed + 1 end,
    OnSystemMessage = function() displayed = displayed + 1 end,
    GetDisplayName = function(_, name) return name end,
}
Ents = {}
TheNet = { GetNetIdForUser = function() return "NET_A" end }
MAX_CHAT_INPUT_LENGTH = 150
string.utf8len = function(value)
    local _, count = value:gsub("[^\128-\191]", "")
    return count
end
dofile(handlers)
local native_system = Networking_SystemMessage
Networking_SystemMessage = function(...)
    native_system(...)
    return nil, "preserved", nil, false
end
if profile ~= "off" then require("dst_server.message_events").install() end
Networking_Say(1, "KU_A", "玩家", "wilson", "你好", nil, false, false)
Networking_Say(1, "KU_A", "玩家", "wilson", "秘密", nil, true, false)
Networking_Say(1, "KU_A", "玩家", "wilson", "wave", nil, false, true)
Networking_Say(1, "KU_A", "玩家", "wilson", nil, nil, false, false)
Networking_Say(1, "KU_A", "玩家", "wilson", string.rep("界", 151), nil, false, false)
Networking_Announcement(nil)
Networking_Announcement("", nil, "")
Networking_Announcement("Maintenance in 60 seconds")
Networking_Announcement("Wet season", nil, "mod_custom_湿季")
Networking_JoinAnnouncement("玩家")
Networking_KickAnnouncement("玩家")
Networking_BanAnnouncement("玩家")
Networking_SkinAnnouncement("玩家", nil, "wilson_rose")
Networking_SkinAnnouncement("玩家", {1, 1, 1, 1}, "wilson_rose")
local function pack(...) return { n = select("#", ...), ... } end
local result = pack(Networking_SystemMessage("Hello\nworld"))
assert(result.n == 4 and result[1] == nil and result[2] == "preserved" and result[3] == nil and result[4] == false)
Networking_RollAnnouncement("KU_A", "玩家", "wilson", nil, {1, 4, 6}, 6)
-- Administrators also use this native callback; it must not create a real vote result.
assert(Networking_VoteAnnouncement(42, "", true) == "rollback")
assert(displayed == 13)

local failure = {}
ChatHistory.OnAnnouncement = function() error(failure, 0) end
local before = #outputs
local ok, err = pcall(Networking_Announcement, "failed")
assert(not ok and err == failure and #outputs == before)

-- The game callback still succeeds if the optional telemetry encoder fails.
require("dst_server.values").entity_ref = function() error("capture failed") end
assert(Networking_Say(1, "KU_A", "玩家", "wilson", "still delivered", nil, false, false) == nil)
assert(displayed == 14)
assert(state.errors == (profile == "off" and 0 or 1))
for _, line in ipairs(outputs) do io.write(line, "\n") end
