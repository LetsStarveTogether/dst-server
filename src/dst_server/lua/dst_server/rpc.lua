local json = require("json")
local state = require("dst_server.state")
local wire = require("dst_server.wire")
local rpc = {}
local prefix = "DST_RPC|"
-- Native FD3 reads a block, not a line. Keep a request within Linux PIPE_BUF
-- so it reaches that reader in one atomic write.
local maximum_request_bytes = 4096
local maximum_line_bytes = 64 * 1024
local installed
local fields = { v = true, nonce = true, id = true, generation = true, method = true, arguments = true }

local function identifier(value)
    return type(value) == "string" and #value == 26 and value:match("^[0-7][0-9A-HJKMNP-TV-Z]+$") ~= nil
end

local function integer(value)
    return type(value) == "number" and value >= 0 and value <= 9007199254740991 and value == math.floor(value)
end

function rpc.install(options)
    assert(installed == nil, "RPC is already installed")
    local original = ExecuteConsoleCommand
    local ordinary_print = print
    local remote_print = TheSim.LuaPrintRemote
    local sim = TheSim
    assert(type(original) == "function" and type(remote_print) == "function")
    assert(identifier(options.nonce) and integer(options.generation))
    installed = { nonce = options.nonce, generation = options.generation }

    local function send(record)
        remote_print(sim, prefix .. wire.encode(record, maximum_line_bytes - #prefix - 1) .. "\n")
    end

    ExecuteConsoleCommand = function(line, ...)
        if type(line) ~= "string" or line:sub(1, #prefix) ~= prefix then
            return original(line, ...)
        end
        local header = { v = 1, nonce = installed.nonce, id = json.null, generation = installed.generation }
        local completed = false
        local function reject(reason)
            if completed then return end
            completed = true
            header.result = { ok = false, error = reason }
            send(header)
        end
        -- The cloudserver temporarily redirects print to FD4. Business output
        -- belongs in the normal log; only our explicit replies use that pipe.
        local command_print = print
        print = ordinary_print
        -- Native WrapRemoteCommandPrint restores print only when this function
        -- returns normally. Never let malformed input or an output error escape.
        local ok = pcall(function()
            line = line:gsub("\n$", "")
            if #line + 1 > maximum_request_bytes or line:find("[\r\n]") then
                reject("invalid_request")
                return
            end
            local decoded, request = pcall(wire.decode, line:sub(#prefix + 1))
            if not decoded or not wire.is_object(request) then reject("invalid_request"); return end
            if identifier(request.id) then header.id = request.id end
            local valid = request.v == 1 and request.nonce == installed.nonce and identifier(request.id)
                and integer(request.generation) and type(request.method) == "string"
                and request.method:match("^[a-z_][a-z0-9_]*$") ~= nil and wire.is_object(request.arguments)
            for name in pairs(request) do if not fields[name] then valid = false end end
            if not valid then reject("invalid_request"); return end
            if request.generation ~= installed.generation then reject("stale_generation"); return end
            if not state.installed or state.nonce ~= installed.nonce or state.generation ~= installed.generation then
                reject("not_ready")
                return
            end
            header.accepted = true
            send(header)
            header.accepted = nil
            local beginning = prefix .. wire.encode(header):sub(1, -2) .. ',"result":'
            local function respond(callback)
                if completed then return end
                completed = true
                local encoded, response = pcall(wire.response, callback, maximum_line_bytes - #beginning - 2)
                if not encoded then response = '{"ok":false,"error":"lua_error"}' end
                pcall(remote_print, sim, beginning .. response .. "}\n")
            end
            if request.method == "save" then
                -- Saving finishes asynchronously, after native command dispatch.
                -- Keep the request ID until this particular save's callback.
                local saved, failure = pcall(require("dst_server.commands").save, request.arguments, function(data, failure)
                    respond(function()
                        if failure == "indeterminate" then wire.indeterminate() end
                        assert(failure == nil)
                        return data
                    end)
                end)
                if not saved then respond(function() error(failure, 0) end) end
            else
                respond(function()
                    return require("dst_server").call(request.method, request.arguments)
                end)
            end
        end)
        print = command_print
        if not ok then
            header.accepted = nil
            pcall(reject, "lua_error")
        end
    end
end

return rpc
