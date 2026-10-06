local root, scripts = assert(arg[1]), assert(arg[2])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
local wire = require("dst_server.wire")
local output, replies = {}, {}
local nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
local health = { generation = 7, control_ready = true, profile = "off", faults = wire.object({ r2 = "" }) }
package.loaded.dst_server = { health = function() return health end }
package.loaded["dst_server.rpc"] = { install = function() end }
local control
package.loaded["dst_server.recovery"] = {
    gate = function(callback) control = callback; return function() end end,
}
SpawnPrefabFromSim = function() return 0 end
Ents = {}
TheSim = {
    GetNumLaunches = function() return 7 end,
    GetPersistentString = function(_, _, callback)
        callback(true, json.encode({driver = {nonce = nonce, profile = "off"}}))
    end,
    LuaPrintRemote = function(_, line)
        if line:sub(1, 11) == "DST_DRIVER|" then
            output[#output + 1] = line:sub(1, -2)
        else
            replies[#replies + 1] = line
        end
    end,
}
local bootstrap = require("dst_server.bootstrap")
bootstrap.start()
assert(#output == 0, "valid launch configuration must not publish a bootstrap error")
for _, size in ipairs({ 4095, 4096, 4097, 8192, 32768, 65535, 65536, 65537 }) do
    health.faults.r2 = "边界😀\"\\\n\0"
    bootstrap.ready()
    local extra = size - #output[#output] - 1
    health.faults.r2 = health.faults.r2 .. string.rep("x", extra)
    bootstrap.ready()
    local last = wire.decode(output[#output]:sub(12))
    if size <= 65536 then
        assert(#output[#output] + 1 == size and last.nonce == nonce and last.health.faults.r2 == health.faults.r2)
    else
        assert(last.error == "publication_failed")
    end
    local record = { event = "recovery_failed", error = "边界😀\"\\\n\0" }
    assert(control(record))
    record.error = record.error .. string.rep("x", size - #replies[#replies])
    local before = #replies
    local sent = control(record)
    assert(sent == (size <= 65536))
    if sent then
        assert(#replies[#replies] == size)
        assert(wire.decode(replies[#replies]:sub(13, -2)).error == record.error)
    else
        assert(#replies == before)
    end
end
io.write("ok\n")
