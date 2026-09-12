return function(root, scripts)
    assert(scripts, "native DST scripts root is required")
    package.path = root .. "/src/dst_server/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
    json = require("json")
end
