local root, scripts, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local state = require("dst_server.state")
state.nonce, state.generation = "01ARZ3NDEKTSV4RRFFQ69G5FAV", 4
state.requested_profile, state.telemetry_active = profile, profile ~= "off"
TheWorld = { meta = { session_identifier = "SESSION" }, state = { cycles = 2 } }
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
MAX_CHAT_INPUT_LENGTH = 150
string.utf8len = function(value)
    local _, count = value:gsub("[^\128-\191]", "")
    return count
end

local calls, emitted, failure = 0, {}, {}
local function native(...)
    calls = calls + 1
    if select(1, ...) == failure then error(failure, 0) end
    return nil, "native", nil, false
end
Networking_Say, Networking_RollAnnouncement, Networking_Announcement = native, native, native
local messages = require("dst_server.message_events")
if profile ~= "off" then messages.install_public_chat() end
if profile == "play" then messages.install_dice() end
assert((Networking_Say == native) == (profile == "off"))
assert((Networking_RollAnnouncement == native) == (profile ~= "play"))
assert(Networking_Announcement == native)

local wire = require("dst_server.wire")
local pack = require("dst_server.telemetry").pack
dofile(root .. "/tests/lua/capture_remote.lua")(function(line)
    emitted[#emitted + 1] = wire.decode(line:sub(10))
    io.write(line:sub(10), "\n")
end)
local function invoke(callback, ...)
    local before, count = calls, #emitted
    local result = pack(callback(...))
    assert(calls == before + 1)
    assert(result.n == 4 and result[1] == nil and result[2] == "native" and result[3] == nil and result[4] == false)
    if #emitted > count then return emitted[#emitted] end
end
-- A known native sender or a whisper has no reason to look up an entity.
Ents = nil
assert(invoke(Networking_Say, 1, "", "Name", "wilson", "private", nil, true, false) == nil)
assert(invoke(Networking_Say, 1, "KU_A", "Name", "", string.rep("界", 151), nil, false, false) == nil)
local text = string.rep("界", 150)
local chat = invoke(Networking_Say, 1, "KU_A", "Name", "wilson", text, nil, false, true)
Ents = { [1] = { userid = "KU_ENTITY", IsValid = function() return true end,
    HasTag = function(_, tag) return tag == "player" end } }
local fallback = invoke(Networking_Say, 1, "", nil, "", "hello", nil, false, false)
local unknown = invoke(Networking_Say, 2, nil, nil, "", "hello", nil, false, false)
local dice = invoke(Networking_RollAnnouncement, "KU_A", "Name", "wilson", nil, { 1, 6 }, 6)
assert(invoke(Networking_Announcement, "formatted dice") == nil)
if profile ~= "off" then
    assert(chat.event == "dst.player.public_chat" and chat.data.userid == "KU_A" and chat.data.message == text and chat.data.emote)
    assert(chat.data.player == nil and chat.data.whisper == nil)
    assert(fallback.data.userid == "KU_ENTITY" and fallback.data.prefab == json.null)
    assert(unknown.data.userid == json.null)
end
if profile == "play" then assert(dice.event == "dst.player.dice_rolled" and dice.data.rolls[2] == 6) end
assert(#emitted == (profile == "off" and 0 or profile == "play" and 4 or 3))
local ok, err = pcall(Networking_Say, failure, "KU_A", nil, nil, "native failed")
assert(not ok and err == failure)
Ents[1].IsValid = function() error("mod entity is gone") end
assert(invoke(Networking_Say, 1, "", nil, nil, "unknown sender") == nil)
assert(state.errors == (profile == "off" and 0 or 1))
