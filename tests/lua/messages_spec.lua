local root, scripts, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
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
Ents = {}
MAX_CHAT_INPUT_LENGTH = 150
string.utf8len = function(value)
    local _, count = value:gsub("[^\128-\191]", "")
    return count
end
local calls, failure = 0, {}
local fail = false
local function original(...)
    calls = calls + 1
    if fail then error(failure, 0) end
    return nil, "preserved", nil, false
end
Networking_Say, Networking_Announcement = original, original
Networking_SkinAnnouncement, Networking_SystemMessage, Networking_RollAnnouncement = original, original, original
if profile ~= "off" then require("dst_server.message_events").install() end
local function invoke(callback, ...)
    local before = calls
    local results = require("dst_server.telemetry").pack(callback(...))
    assert(calls == before + 1)
    assert(results.n == 4 and results[1] == nil and results[2] == "preserved" and results[3] == nil and results[4] == false)
end
invoke(Networking_Say, 1, "KU_A", "玩家", "wilson", "你好", nil, false, false)
invoke(Networking_Say, 1, "KU_A", "玩家", "wilson", "秘密", nil, true, false)
invoke(Networking_Say, 1, "KU_A", "玩家", "wilson", "wave", nil, false, true)
invoke(Networking_Say, 1, "KU_A", "玩家", "wilson", nil, nil, false, false)
invoke(Networking_Say, 1, "KU_A", "玩家", "wilson", string.rep("界", 151), nil, false, false)
invoke(Networking_Announcement, nil)
invoke(Networking_Announcement, "", nil, "")
invoke(Networking_Announcement, "Maintenance in 60 seconds")
invoke(Networking_Announcement, "Wet season", nil, "mod_custom_湿季")
invoke(Networking_SkinAnnouncement, "玩家", nil, "wilson_rose")
invoke(Networking_SkinAnnouncement, "玩家", {1, 1, 1, 1}, "wilson_rose")
invoke(Networking_SystemMessage, "Hello\nworld")
invoke(Networking_RollAnnouncement, "KU_A", "玩家", "wilson", nil, {1, 4, 6}, 6)
local before = #outputs
fail = true
local ok, err = pcall(Networking_Announcement, "failed")
assert(not ok and err == failure and #outputs == before)
fail = false
require("dst_server.values").entity_ref = function() error("capture failed") end
invoke(Networking_Say, 1, "KU_A", "玩家", "wilson", "still delivered", nil, false, false)
assert(state.errors == (profile == "off" and 0 or 1))
for _, line in ipairs(outputs) do io.write(line, "\n") end
