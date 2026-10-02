return function(root, scripts)
    assert(scripts, "native DST scripts root is required")
    package.path = root .. "/resources/lua/?.lua;" .. scripts .. "/?.lua;" .. package.path
    json = require("json")
    -- Container UI definitions require engine widgets; component behavior does not.
    package.loaded.containers = {}
end
