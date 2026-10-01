local root, scripts = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local recovery = require("dst_server.recovery")
local function path(id) return "save/session/SESSION/" .. string.format("%010d", id) end
local latest, truncations, shutdowns = path(3), {}, 0
local catalog = {
    { snapshot_id = 3, world_file = path(3) },
    { snapshot_id = 2, world_file = path(2) },
    { snapshot_id = 1, world_file = path(1) },
}
TheNet = {
    GetWorldSessionFile = function(_, session) assert(session == "SESSION"); return latest end,
    IsOnlineMode = function() return true end,
    ListSnapshots = function(_, session, online, count)
        assert(session == "SESSION" and online and count == 100)
        return catalog, false
    end,
    TruncateSnapshots = function(_, session, target)
        assert(session == "SESSION" and target == 2)
        truncations[#truncations + 1] = target
        latest = path(target)
    end,
}
Shutdown = function() shutdowns = shutdowns + 1 end
local index = { GetSession = function() return "SESSION" end }
local records = {}
local function emit(record) records[#records + 1] = record end
assert(recovery.run(index, { mode = "catalog" }, emit))
assert(records[1].event == "recovery_catalog" and records[1].latest_world_file == path(3))
assert(#truncations == 0 and shutdowns == 1 and records[1].snapshots[2].snapshot_id == 2)
local request = { mode = "apply", session_id = "SESSION", snapshot_id = 2, world_file = path(2) }
assert(recovery.run(index, request, emit))
assert(#truncations == 1 and records[#records].changed and latest == path(2))
assert(recovery.run(index, request, emit))
assert(#truncations == 1 and not records[#records].changed)
latest = path(1)
assert(not recovery.run(index, request, emit))
assert(records[#records].error == "target_is_newer" and #truncations == 1)
latest = path(3)
assert(not recovery.run(index, request, emit, function() end))
assert(records[#records].error == "target_not_selected" and #truncations == 1)
request.world_file = "save/session/OTHER/0000000002"
assert(not recovery.run(index, request, emit))
assert(records[#records].error == "target_path_mismatch" and #truncations == 1)
request.world_file, request.session_id = path(2), "OTHER"
assert(not recovery.run(index, request, emit))
assert(records[#records].error == "session_changed" and #truncations == 1)
request.session_id = "SESSION"
catalog[2] = { snapshot_id = 0 }
assert(not recovery.run(index, request, emit))
assert(records[#records].error == "target_not_found")
TheWorld = {}
assert(not recovery.run(index, { mode = "catalog" }, emit))
assert(records[#records].error == "world_already_loaded")
TheWorld = nil
assert(recovery.run(index, { mode = "catalog" }, function() error("closed control pipe", 0) end))
assert(shutdowns == 10, "lost replies must still stop the recovery process")

-- Index loading can finish before asynchronous driver configuration; its callback must wait.
local function native_load(_, callback, value) return callback(value, nil, "last") end
ShardIndex = { Load = native_load }
local starts = 0
local function start_server(...)
    starts = starts + 1
    return ...
end
StartDedicatedServer = start_server
local resume = assert(recovery.gate(emit))
local continued = false
StartDedicatedServer("early authentication")
ShardIndex.Load(index, function() continued = true end, "first")
assert(not continued and starts == 0)
resume({ mode = "catalog" })
assert(not continued and records[#records].event == "recovery_catalog" and shutdowns == 11)
StartDedicatedServer("late authentication")
ShardIndex.Load(index, function() continued = true end, "again")
assert(starts == 0 and not continued and shutdowns == 11, "recovery must not launch another Lua instance")
ShardIndex.Load = native_load
StartDedicatedServer = start_server
resume = assert(recovery.gate(emit))
StartDedicatedServer("ordinary startup")
ShardIndex.Load(index, function(a, b, c)
    continued = true
    assert(a == "first" and b == nil and c == "last")
end, "first")
resume(nil)
assert(continued and shutdowns == 11 and starts == 1)
local a, b, c = ShardIndex.Load(index, function(...) return ... end, "again")
assert(a == "again" and b == nil and c == "last")
a, b, c = StartDedicatedServer("again", nil, "last")
assert(a == "again" and b == nil and c == "last" and starts == 2)
io.write("ok\n")
