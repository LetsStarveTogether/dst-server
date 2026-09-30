local values = require("dst_server.values")
local wire = require("dst_server.wire")
local console = {}
local maximum_values = 16
local maximum_value_bytes = 512
-- Keep output plus all return values below 64 KiB even with six-byte JSON escapes.
local maximum_output_bytes = 2048

local function pack(...)
    return { n = select("#", ...), ... }
end

function console.evaluate(args)
    local source = values.required_string(args, "source")
    local result = { output = "", values = {}, error = json.null, truncated = false }

    local function text(value, maximum)
        local ok, rendered = pcall(tostring, value)
        if not ok or type(rendered) ~= "string" then
            return "<" .. type(value) .. ": tostring failed>"
        end
        if #rendered > maximum then result.truncated = true end
        rendered = values.text(rendered, maximum)
        if not pcall(wire.encode, rendered) then
            rendered = string.gsub(rendered, "[\128-\255]", function(byte)
                return string.format("\\x%02x", string.byte(byte))
            end)
        end
        return rendered
    end

    local callback, message = loadstring("return " .. source, "@dst-server-console")
    if callback == nil then
        callback, message = loadstring(source, "@dst-server-console")
    end
    if callback == nil then
        result.error = { kind = "compile", message = text(message, 1024) }
        return result
    end

    local output, remaining = {}, maximum_output_bytes
    local function append(value)
        if #value > remaining then
            value = values.text(value, remaining)
            remaining = 0
            result.truncated = true
        else
            remaining = remaining - #value
        end
        if value ~= "" then output[#output + 1] = value end
    end
    local original_print = print
    local capture = function(...)
        for index = 1, select("#", ...) do
            if index > 1 then append("\t") end
            append(tostring(select(index, ...)))
        end
        append("\n")
    end
    -- Cached references must resume native printing after this evaluation.
    print = function(...) return (capture or original_print)(...) end

    local returned = pack(pcall(callback))
    if not returned[1] then
        result.error = { kind = "runtime", message = text(returned[2], 1024) }
    else
        result.truncated = result.truncated or returned.n - 1 > maximum_values
        for index = 2, math.min(returned.n, maximum_values + 1) do
            local value = returned[index]
            result.values[#result.values + 1] = {
                type = type(value), text = text(value, maximum_value_bytes),
            }
        end
    end
    print = original_print
    capture = nil
    result.output = text(string.gsub(table.concat(output), "\n$", ""), maximum_output_bytes)
    return result
end

return console
