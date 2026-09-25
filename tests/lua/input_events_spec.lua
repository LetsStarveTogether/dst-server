local root, scripts, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
require("class")
Entity = {}
require("entityscript")
require("util")
for _, name in ipairs({"screens/redux/inputdialog", "screens/redux/popupdialog", "widgets/widget",
    "widgets/imagebutton", "widgets/text", "screens/redux/connectingtogamepopup"}) do
    package.loaded[name] = {}
end
FRAMES = 1 / 30
COMMAND_PERMISSION = {USER = 1, MODERATOR = 2, ADMIN = 3}
COMMAND_RESULT = {DISABLED = 0, DENY = 1, INVALID = 2, ALLOW = 3, VOTE = 4}
smallhash = function(value) return value end
dumptablequiet = function() end
IsConsole = function() return false end
STRINGS = {UI = {BUILTINCOMMANDS = {}, USERCOMMANDS = {DISABLEDFMT = "%s", NOTALLOWEDFMT = "%s"}}}
local prints = {}
print = function(...) prints[#prints + 1] = {...} end
local commands = require("usercommands")
require("emote_items")
require("emotes")
require("builtinusercommands")
require("networking")

local player = setmetatable({prefab = "wilson", GUID = 2, userid = "KU_PLAYER", name = "玩家", components = {}}, {__index = EntityScript})
local client = {userid = player.userid, name = player.name}
AllPlayers = {player}
Ents = {[player.GUID] = player}
TheWorld = {ismastersim = true, meta = {session_identifier = "SESSION"}, state = {cycles = 2}}
TheNet = {
    GetServerIsClientHosted = function() return true end,
    GetClientTable = function() return {client} end,
    GetClientTableForUser = function(_, userid) return userid == client.userid and client or nil end,
}
local owned = false
TheInventory = {CheckClientOwnership = function(_, _, skin) return owned and skin ~= "body_unowned" end}
local rescues, emotes = 0, 0
player.PutBackOnGround = function() rescues = rescues + 1 end
player:ListenForEvent("emote", function() emotes = emotes + 1 end)

local outputs = {}
nolineprint = function(line) outputs[#outputs + 1] = line:sub(10) end
GetTick = function() return 10 end
GetTimeReal = function() return 20 end
local state = require("dst_server.state")
state.nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
state.generation = 4
state.requested_profile = profile
state.telemetry_active = profile ~= "off"
local capture = require("dst_server.input_events")
local common, validate = GetCommonEmotes, ValidateSpawnPrefabRequest
GetCommonEmotes, ValidateSpawnPrefabRequest = nil, nil
assert(capture.install_commands() == false and capture.install_appearance() == false)
GetCommonEmotes, ValidateSpawnPrefabRequest = common, validate
local rescue_command = commands.GetCommandFromName("rescue")
local native_rescue = rescue_command.serverfn
rescue_command.serverfn = function(...)
    native_rescue(...)
    return nil, "preserved", nil, false
end
if profile ~= "off" then
    capture.install_commands()
    capture.install_appearance()
end

-- Slash aliases resolve to canonical native commands and run only when the queue executes.
Networking_SlashCmd(player.GUID, player.userid, "hi")
assert(#outputs == 0 and emotes == 0)
HandleUserCmdQueue()
commands.RunUserCommand("toast", {}, player, true)
HandleUserCmdQueue()
assert(emotes == 2)
local before = #outputs
Networking_SlashCmd(player.GUID, player.userid, "chicken")
HandleUserCmdQueue()
assert(emotes == 2 and #outputs == before, "unowned emotes must not be captured as accepted")
owned = true
Networking_SlashCmd(player.GUID, player.userid, "chicken")
HandleUserCmdQueue()
assert(emotes == 3)
Networking_SlashCmd(player.GUID, player.userid, "rescue")
HandleUserCmdQueue()
commands.RunUserCommand("rescue", {}, player, true)
HandleUserCmdQueue()
assert(rescues == 2, "slash and menu requests reach the same native callback")

-- An accepted command can have only a client identity, with no spawned player or animation.
AllPlayers = {}
Networking_SlashCmd(999, player.userid, "wave")
HandleUserCmdQueue()
assert(emotes == 3)
AllPlayers = {player}
-- Native per-tick limits reject the eleventh request; the wrapper cannot bypass them.
before = #outputs
for _ = 1, 11 do Networking_SlashCmd(player.GUID, player.userid, "dance") end
HandleUserCmdQueue()
assert(emotes == 13 and #outputs == before + (profile == "off" and 0 or 10))

MODCHARACTERLIST, MODCHARACTEREXCEPTIONS_DST, SEAMLESSSWAP_CHARACTERLIST = {}, {}, {"wonkey"}
DST_CHARACTERLIST = {"wilson"}
PREFAB_SKINS = {wilson = {"wilson_rose"}}
IsClothingItem = function(value) return value == "body_owned" or value == "hand_owned" end
local telemetry = require("dst_server.telemetry")
local result = telemetry.pack(ValidateSpawnPrefabRequest(player.userid, "wilson", "wilson_rose", "body_unowned", "hand_owned", "", ""))
assert(result.n == 6 and result[1] == "wilson" and result[2] == "wilson_rose" and result[3] == nil and result[4] == "hand_owned" and result[5] == nil and result[6] == nil)
result = telemetry.pack(ValidateSpawnPrefabRequest(player.userid, "wonkey", "wonkey_none", "", "", "", "", false))
assert(result.n == 6 and result[1] == "wilson" and result[2] == nil and result[6] == nil)
local failure = {}
local check_ownership = TheInventory.CheckClientOwnership
TheInventory.CheckClientOwnership = function() error(failure, 0) end
before = #outputs
local ok, err = pcall(ValidateSpawnPrefabRequest, player.userid, "wilson", "wilson_rose", "", "", "", "")
assert(not ok and err == failure and #outputs == before, "failed validation cannot report a validated appearance")
TheInventory.CheckClientOwnership = check_ownership

-- Original multiple returns and errors survive; a failed command remains an accepted request.
result = telemetry.pack(rescue_command.serverfn({}, player))
assert(result.n == 4 and result[1] == nil and result[2] == "preserved" and result[3] == nil and result[4] == false)
player.PutBackOnGround = function() error(failure, 0) end
ok, err = pcall(rescue_command.serverfn, {}, player)
assert(not ok and err == failure)
player.PutBackOnGround = function() rescues = rescues + 1 end
local entity_ref = require("dst_server.values").entity_ref
require("dst_server.values").entity_ref = function() error("capture failed") end
result = telemetry.pack(rescue_command.serverfn({}, player))
assert(rescues == 4 and result.n == 4 and result[2] == "preserved")
result = telemetry.pack(ValidateSpawnPrefabRequest(player.userid, "wilson", "wilson_rose", "", "", "", ""))
assert(result.n == 6 and result[1] == "wilson" and result[2] == "wilson_rose" and result[6] == nil)
require("dst_server.values").entity_ref = entity_ref
assert(state.errors == (profile == "off" and 0 or 2))
for _, line in ipairs(outputs) do io.write(line, "\n") end
