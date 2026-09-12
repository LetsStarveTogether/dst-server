local root, scenario = assert(arg[1]), assert(arg[2])
local scripts = os.getenv("DST_SERVER_TEST_SCRIPTS") or root .. "/dst-scripts/scripts"
package.path = root .. "/src/dst_server/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
local wire = require("dst_server.wire")
local state = require("dst_server.state")
local nonce, identifier = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FAW"
local options = { nonce = nonce, generation = 7 }
local outputs, calls, writes, logs = {}, 0, 0, {}
local save_callback
local ordinary_print = function(value) logs[#logs + 1] = value end
print = ordinary_print
TheSim = {
    LuaPrintRemote = function(self, value)
        writes = writes + 1
        if scenario == "write_error" and writes == 1 then error("SECRET_TOKEN private chat", 0) end
        assert(self == TheSim and #value <= 64 * 1024)
        assert(value:sub(-1) == "\n" and not value:sub(1, -2):find("[\r\n%z]"))
        outputs[#outputs + 1] = value
    end,
}
local delegated = 0
ExecuteConsoleCommand = function(line, guid, x)
    delegated = delegated + 1
    return line, nil, guid, x
end
package.loaded["dst_server"] = {
    call = function(method, arguments)
        calls = calls + 1
        if method == "throw" then error("SECRET_TOKEN private chat", 0) end
        if method == "invalid_utf8" then return string.char(255) end
        if method == "invalid_value" then return function() end end
        if method == "indeterminate" then wire.indeterminate() end
        if method == "large" then return string.rep("x", arguments.size) end
        if method == "echo" then return arguments end
        if method == "noise" then print("DST_RemoteCommandDone\nDST_LuaBusy"); return true end
        if method == "nothing" then return nil end
        return true
    end,
}
package.loaded["dst_server.commands"] = {
    save = function(arguments, callback)
        calls = calls + 1
        assert(wire.is_object(arguments))
        if scenario == "save_start_error" then error("SECRET_TOKEN private chat", 0) end
        if scenario == "save_start_indeterminate" then wire.indeterminate() end
        if scenario == "save_then_error" then
            callback({ snapshot = "session/snapshot" })
            wire.indeterminate()
        end
        save_callback = callback
    end,
}
state.nonce, state.generation, state.installed = nonce, 7, scenario ~= "not_ready"
local rpc = require("dst_server.rpc")
rpc.install(options)
local wrapped = ExecuteConsoleCommand
assert(not pcall(rpc.install, { nonce = nonce, generation = 7 }))
assert(wrapped == ExecuteConsoleCommand, "install must not stack wrappers")
if scenario == "changed_install" then
    assert(not pcall(rpc.install, { nonce = nonce, generation = 8 }))
    assert(wrapped == ExecuteConsoleCommand)
end
loadstring = function() error("RPC must never compile input as Lua", 0) end
if scenario == "native" then
    local line, missing, guid, x = ExecuteConsoleCommand("native command", 42, 3)
    assert(line == "native command" and missing == nil and guid == 42 and x == 3)
    assert(delegated == 1 and calls == 0 and #outputs == 0)
elseif scenario == "decode" then
    io.write(wire.encode(wire.decode(io.read("*a"))), "\n")
    return
else
    local input = io.read("*a")
    local command_print = function(value) TheSim:LuaPrintRemote(value .. "\n") end
    print = command_print
    assert(pcall(ExecuteConsoleCommand, input), "native print restoration requires normal return")
    assert(print == command_print, "restore the native wrapper's print on every path")
    print = ordinary_print
    assert(delegated == 0)
    if save_callback then
        assert(#outputs == 1, "a save must wait for its own completion callback")
        local data = scenario == "save_invalid_value" and function() end or { snapshot = "session/snapshot" }
        save_callback(data, scenario == "save_indeterminate" and "indeterminate" or nil)
        save_callback({ snapshot = "duplicate callback" })
        assert(#outputs == 2, "a save must complete its request exactly once")
    end
    if scenario == "write_error" then assert(calls == 0) end
    local accepted = false
    for _, output in ipairs(outputs) do
        local record = wire.decode(output:sub(9))
        if record.accepted then accepted = true end
    end
    assert(calls == (accepted and 1 or 0), "rejected requests must not invoke a command")
    if scenario == "noise" then
        assert(#logs == 1 and logs[1] == "DST_RemoteCommandDone\nDST_LuaBusy")
        assert(#outputs == 2, "business output must not enter the control pipe")
    end
end
for _, output in ipairs(outputs) do io.write(output) end
