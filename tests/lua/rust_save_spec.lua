local root, scripts = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)

-- Execute the native SaveGame body, delaying both world serialization and its final write callback.
local file = assert(io.open(scripts .. "/mainfunctions.lua"))
local source = file:read("*a")
file:close()
assert(loadstring(assert(source:match("(function SaveGame%(isshutdown, cb%).-)\nfunction ProcessJsonMessage"))))()
local snapshot, ended = 10, 0
local serialized, final_callbacks, records = {}, {}, {}
TheNet = {
    GetIsServer = function() return true end,
    GetCurrentSnapshot = function() return snapshot end,
    StartWorldSave = function() end,
    TruncateSnapshots = function() end,
    IncrementSnapshot = function() snapshot = snapshot + 1 end,
    EndWorldSave = function() ended = ended + 1 end,
}
AllPlayers, Ents = {}, {}
TheWorld = {
    meta = { session_identifier = "SESSION" }, worldprefab = "forest", topology = { overrides = {} },
    GetPersistData = function() return {} end,
    net = { GetPersistData = function() return { clock = { cycles = 1 } } end },
    Map = {
        GetStringEncode = function() return "tiles" end,
        GetDataStringEncode = function() return "data" end,
        GetNavStringEncode = function() return "nav" end,
        GetNodeIdTileMapStringEncode = function() return "nodes" end,
        GetSize = function() return 10, 10 end,
    },
}
GetWorldTileMap = function() return {} end
WasSuUsed = function() return false end
ModManager = { GetModRecords = function() return {} end }
DataDumper = function() return "{}" end
deepcopy = function(value) return value end
SerializeWorldSession = function(_, session, callback)
    assert(session == "SESSION")
    serialized[#serialized + 1] = callback
end
UpdateWorldGenOverride = function(_, callback) callback() end
ShardGameIndex = {
    GetGenOptions = function() return {} end,
    GetSlot = function() return 0 end,
    GetShard = function() end,
    Save = function(_, callback) callback() end,
    WriteTimeFile = function(_, callback) final_callbacks[#final_callbacks + 1] = callback end,
}
local function emit(record) records[#records + 1] = record end
require("dst_server.save_observer").install(emit)
local original_callback = 0
SaveGame(true, function()
    original_callback = original_callback + 1
    assert(ended == 1 and records[#records].event == "save_complete")
end)
assert(#records == 1 and records[1].event == "save_started")
serialized[1]()
assert(snapshot == 11 and #records == 1 and original_callback == 0)
final_callbacks[1]()
assert(original_callback == 1 and records[2].snapshot_id == 10 and records[2].shutdown)

-- Two saves started at the same snapshot cannot safely be attributed to different files.
SaveGame(false)
SaveGame(false)
serialized[2]()
serialized[3]()
final_callbacks[3]()
final_callbacks[2]()
assert(records[#records].event == "save_unconfirmed" and records[#records].error == "overlapping_snapshot")
assert(records[#records - 1].event == "save_unconfirmed")

-- Different snapshots can overlap at the final callback; never infer their ID from current - 1.
SaveGame(false)
serialized[4]()
SaveGame(false)
serialized[5]()
final_callbacks[5]()
final_callbacks[4]()
assert(records[#records - 1].event == "save_complete" and records[#records - 1].snapshot_id == 14)
assert(records[#records].event == "save_complete" and records[#records].snapshot_id == 13)

local callback_failure = {}
SaveGame(false, function() error(callback_failure, 0) end)
serialized[6]()
local ok, failure = pcall(final_callbacks[6])
assert(not ok and failure == callback_failure and records[#records].event == "save_complete")
SaveGame(false)
serialized[7]()
TheWorld = { meta = { session_identifier = "OTHER" } }
final_callbacks[7]()
assert(records[#records].event == "save_unconfirmed" and records[#records].error == "session_changed")

-- Reinstallation follows late Mod replacements without resetting IDs or
-- wrapping the same current function twice. Delegation to an old SDK wrapper
-- still emits one final native proof.
local observer = require("dst_server.save_observer")
SaveGame = function(_, callback, ...)
    snapshot = snapshot + 1
    return callback(...)
end
assert(observer.refresh())
local wrapped = SaveGame
assert(not observer.refresh() and SaveGame == wrapped)
local count, previous_id = #records, records[#records].save_id
SaveGame = function(...) return wrapped(...) end
assert(observer.refresh())
local first, middle, last = SaveGame(false, function(...) return ... end, "first", nil, "last")
assert(first == "first" and middle == nil and last == "last")
assert(#records == count + 2 and records[#records].event == "save_complete")
assert(records[#records].save_id == previous_id + 1)

-- A callback may start a separate save; it must not be mistaken for delegation.
count = #records
SaveGame(false, function() SaveGame(false) end)
assert(#records == count + 4 and records[#records].event == "save_complete")
assert(records[#records - 2].event == "save_complete")

-- An in-flight callback remains owned across replacement. A non-persistent
-- mode that only calls its callback produces an immediate unconfirmed result.
local delayed
SaveGame = function(_, callback) delayed = callback; return "queued" end
observer.refresh()
assert(SaveGame(false) == "queued")
local pending_id = records[#records].save_id
snapshot = snapshot + 1
SaveGame = function(_, callback, ...) return callback(...) end
observer.refresh()
assert(SaveGame(false, function(value) return value end, "mode") == "mode")
assert(records[#records].event == "save_unconfirmed")
assert(records[#records].error == "snapshot_unconfirmed")
assert(records[#records].save_id == pending_id + 1)
delayed()
assert(records[#records].event == "save_complete" and records[#records].save_id == pending_id)

-- c_shutdown calls native SaveCurrent directly, outside the SDK dispatcher.
ShardGameIndex.SaveCurrent = function(self, callback, isshutdown)
    assert(self == ShardGameIndex)
    return SaveGame(isshutdown, callback)
end
observer.refresh()
local entry = ShardGameIndex.SaveCurrent
SaveGame = function(_, callback) return callback("shutdown") end
assert(ShardGameIndex:SaveCurrent(function(value) return value end, true) == "shutdown")
assert(ShardGameIndex.SaveCurrent == entry)
assert(records[#records].event == "save_unconfirmed" and records[#records].shutdown)
assert(records[#records].error == "snapshot_unconfirmed")

local native_failure = {}
SaveGame = function() error(native_failure, 0) end
require("dst_server.save_observer").install(emit)
ok, failure = pcall(SaveGame, false)
assert(not ok and failure == native_failure and records[#records].event == "save_failed")
SaveGame = function(_, callback, ...)
    snapshot = snapshot + 1
    return callback(...)
end
require("dst_server.save_observer").install(function() error("lost control pipe", 0) end)
local a, b, c = SaveGame(false, function(...) return ... end, "first", nil, "last")
assert(a == "first" and b == nil and c == "last")
SaveGame = function(_, callback) return callback() end
TheNet.GetCurrentSnapshot = function() error("observer unavailable", 0) end
require("dst_server.save_observer").install(emit)
assert(SaveGame(false, function() return "still saved" end) == "still saved")
assert(records[#records].event == "save_unconfirmed" and records[#records].error == "observation_failed")
io.write("ok\n")
