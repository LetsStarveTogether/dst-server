local root, scripts = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)

local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local wire = require("dst_server.wire")
state.nonce, state.generation = "01ARZ3NDEKTSV4RRFFQ69G5FAV", 1
state.requested_profile, state.telemetry_active = "play", true
TheWorld = { state = { cycles = 2 }, meta = { session_identifier = "SESSION" } }
GetTick = function() return 1 end
GetTimeReal = function() return 100 end

local line, writes = nil, 0
dofile(root .. "/tests/lua/capture_remote.lua")(function(frame)
    line, writes = frame, writes + 1
end)
local name, data = "dst.world.state_changed", { name = "phase", value = "边界😀" }
telemetry.emit(name, data)
local overhead = #line + 1
data.value = data.value .. string.rep("x", 65536 - overhead)
telemetry.emit(name, data)
assert(#line + 1 == 65536 and wire.decode(line:sub(10)).data.value == data.value)
data.value = data.value .. "x"
telemetry.emit(name, data)
assert(writes == 2 and state.sequence == 2 and state.last_error.message == "event_too_large")

-- A failed send consumes its number. A fresh event must never replay it.
local remote = TheSim.LuaPrintRemote
local attempts = 0
TheSim.LuaPrintRemote = function()
    attempts = attempts + 1
    error("write failure")
end
data.value = "dusk"
telemetry.emit(name, data)
assert(attempts == 1 and state.sequence == 3 and state.last_error.message == "write_failed")
TheSim.LuaPrintRemote = remote
telemetry.emit(name, data)
assert(writes == 3 and wire.decode(line:sub(10)).seq == 4)

local tick = GetTick
GetTick = function() return nil end
telemetry.emit(name, data)
assert(writes == 3 and state.sequence == 4 and state.faults["emit." .. name] == "encoding_failed")
GetTick = tick

local ok, failures = true, state.errors
local capture = telemetry.guard("test.capture", function()
    if not ok then error("private exception details") end
end)
for _ = 1, 100 do
    ok = false
    capture()
    ok = true
    capture()
end
assert(state.errors == failures + 100 and writes == 3, "intermittent capture errors must not write to FD4")
io.write("ok\n")
