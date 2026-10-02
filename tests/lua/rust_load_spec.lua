local root, scripts = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local file = assert(io.open(scripts .. "/shardindex.lua"))
local source = file:read("*a")
file:close()
local native = assert(source:match("(local function OnLoadSaveDataFile.-)\nfunction ShardIndex:IsMasterShardIndex"))
ShardIndex = {}
assert(loadstring(native, "@scripts/shardindex.lua"))()
local callbacks, records = {}, {}
TheNet = { GetIsClient = function() return false end }
TheSim = { GetPersistentString = function(_, path, callback) callbacks[path] = callback end }
RunInSandbox = function(encoded)
    local chunk = loadstring(encoded)
    if chunk == nil then return false end
    return pcall(chunk)
end
GetTableSize = function(value)
    local count = 0
    for _ in pairs(value) do count = count + 1 end
    return count
end
print = function() end
local emit_failure = false
assert(require("dst_server.load_observer").install(function(record)
    if emit_failure then error("publication failure", 0) end
    records[#records + 1] = record
end))
local index = setmetatable({ GetSession = function() return "SESSION" end }, { __index = ShardIndex })
local called = 0
local function valid(savedata) called = called + 1; assert(savedata.world == "native") end
index:GetSaveDataFile("session/SESSION/0000000001", valid)
assert(#records == 1 and records[1].event == "world_load_started")
callbacks["session/SESSION/0000000001"](true, 'return {world="native"}')
assert(called == 1 and records[#records].event == "world_load_decoded")
for _, case in ipairs({
    { false, "", "read", "read_failed" },
    { true, nil, "read", "encoded_nil" },
    { true, "", "read", "encoded_empty" },
    { true, "not valid Lua", "decode", "parse_failed" },
    { true, "return nil", "decode", "decoded_nil" },
    { true, "return {}", "decode", "decoded_empty" },
}) do
    index:GetSaveDataFile("session/SESSION/0000000002", valid)
    local ok, err = pcall(callbacks["session/SESSION/0000000002"], case[1], case[2])
    assert(not ok and type(err) == "string")
    local record = records[#records]
    assert(record.event == "world_load_failed" and record.session_id == "SESSION")
    assert(record.world_file == "session/SESSION/0000000002")
    assert(record.phase == case[3] and record.reason == case[4])
    assert(record.read_succeeded == case[1] and not record.callback_entered)
    assert(record.decoder_unchanged)
end
-- A Mod replacing a native decoding dependency makes rollback evidence unusable.
local native_sandbox = RunInSandbox
RunInSandbox = function(...) return native_sandbox(...) end
index:GetSaveDataFile("session/SESSION/modified_decoder", valid)
assert(not pcall(callbacks["session/SESSION/modified_decoder"], true, "invalid"))
assert(not records[#records].decoder_unchanged)
RunInSandbox = native_sandbox
-- Errors in world/mod initialization occur after native decoding, not in save parsing.
local original_error = {}
index:GetSaveDataFile("session/SESSION/0000000003", function() error(original_error, 0) end)
local ok, err = pcall(callbacks["session/SESSION/0000000003"], true, 'return {world="native"}')
assert(not ok and err == original_error)
assert(records[#records].phase == "world" and records[#records].callback_entered)
-- Native asynchronous callbacks can complete out of order and preserve their file identities.
index:GetSaveDataFile("session/SESSION/0000000004", valid)
index:GetSaveDataFile("session/SESSION/0000000005", valid)
assert(not pcall(callbacks["session/SESSION/0000000005"], true, "invalid five"))
assert(records[#records].world_file == "session/SESSION/0000000005")
callbacks["session/SESSION/0000000004"](true, 'return {world="native"}')
assert(records[#records].world_file == "session/SESSION/0000000004")
-- Publication failure cannot change native loading or the original exception object.
emit_failure = true
index:GetSaveDataFile("session/SESSION/0000000006", valid)
callbacks["session/SESSION/0000000006"](true, 'return {world="native"}')
index:GetSaveDataFile("session/SESSION/0000000007", function() error(original_error, 0) end)
ok, err = pcall(callbacks["session/SESSION/0000000007"], true, 'return {world="native"}')
assert(not ok and err == original_error)
ShardIndex.GetSaveDataFile = function() end
assert(not require("dst_server.load_observer").install(function() end))
io.write("ok\n")
