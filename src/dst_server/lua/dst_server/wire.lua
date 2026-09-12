local json = require("json")
local wire = {}
local object_marker = {}
local indeterminate = {}
local success_prefix = '{"ok":true,"data":'
local maximum_line_bytes = 64 * 1024
local escapes = { ['"'] = '\\"', ["\\"] = "\\\\" }

for byte = 0, 31 do
    escapes[string.char(byte)] = string.format("\\u%04x", byte)
end
escapes["\b"] = "\\b"
escapes["\f"] = "\\f"
escapes["\n"] = "\\n"
escapes["\r"] = "\\r"
escapes["\t"] = "\\t"

function wire.object(value)
    return setmetatable(value, object_marker)
end

function wire.is_object(value)
    return type(value) == "table" and getmetatable(value) == object_marker
end

function wire.indeterminate()
    error(indeterminate, 0)
end

local function quote(value, remaining)
    local size = #value + 2
    if size > remaining then error("response_too_large", 0) end
    local index = 1
    while true do
        index = string.find(value, "[\128-\255]", index)
        if index == nil then
            break
        end
        local byte = string.byte(value, index)
        local width
        if byte >= 194 and byte <= 223 then
            width = 1
        elseif byte >= 224 and byte <= 239 then
            width = 2
        elseif byte >= 240 and byte <= 244 then
            width = 3
        else
            error("invalid_utf8", 0)
        end
        if index + width > #value then
            error("invalid_utf8", 0)
        end
        local second = string.byte(value, index + 1)
        if (byte == 224 and second < 160)
            or (byte == 237 and second > 159)
            or (byte == 240 and second < 144)
            or (byte == 244 and second > 143) then
            error("invalid_utf8", 0)
        end
        for offset = 1, width do
            local continuation = string.byte(value, index + offset)
            if continuation < 128 or continuation > 191 then
                error("invalid_utf8", 0)
            end
        end
        index = index + width + 1
    end
    return '"' .. string.gsub(value, '[%z\1-\31\\"]', function(character)
        local escaped = escapes[character]
        size = size + #escaped - 1
        if size > remaining then error("response_too_large", 0) end
        return escaped
    end) .. '"'
end

function wire.encode(value, limit)
    local parts, seen = {}, {}
    local remaining = limit or maximum_line_bytes

    local function append(text)
        remaining = remaining - #text
        if remaining < 0 then error("response_too_large", 0) end
        parts[#parts + 1] = text
    end

    local function encode(item)
        if item == nil or item == json.null then
            append("null")
            return
        end
        local kind = type(item)
        if kind == "string" then
            append(quote(item, remaining))
            return
        elseif kind == "boolean" then
            append(item and "true" or "false")
            return
        elseif kind == "number" then
            if item ~= item or item == math.huge or item == -math.huge then
                error("invalid_json_value", 0)
            end
            append(string.format("%.17g", item))
            return
        elseif kind ~= "table" or seen[item] then
            error("invalid_json_value", 0)
        end

        local marker = getmetatable(item)
        if marker ~= nil and marker ~= object_marker then
            error("invalid_json_value", 0)
        end
        seen[item] = true
        local object, count, maximum = marker == object_marker, 0, 0
        for key in next, item do
            local key_kind = type(key)
            if key_kind == "string" then
                if maximum > 0 then
                    error("invalid_json_value", 0)
                end
                object = true
            elseif key_kind == "number" and key >= 1 and key < math.huge
                and key == math.floor(key) then
                if object then
                    error("invalid_json_value", 0)
                end
                maximum = math.max(maximum, key)
            else
                error("invalid_json_value", 0)
            end
            count = count + 1
        end
        if not object and count ~= maximum then
            error("invalid_json_value", 0)
        end

        if object then
            append("{")
            local first = true
            for key, child in next, item do
                if not first then
                    append(",")
                end
                first = false
                append(quote(key, remaining))
                append(":")
                encode(child)
            end
            append("}")
        else
            append("[")
            for index = 1, count do
                if index > 1 then
                    append(",")
                end
                encode(rawget(item, index))
            end
            append("]")
        end
        seen[item] = nil
    end

    encode(value)
    return table.concat(parts)
end

-- The native JSON decoder uses loadstring, accepts Lua expressions and loses
-- nulls and object/array identity. RPC input needs actual JSON instead.
function wire.decode(source)
    if type(source) ~= "string" or #source > maximum_line_bytes then error("invalid_request", 0) end
    local position, length = 1, #source
    local parse
    local function invalid() error("invalid_request", 0) end
    local function whitespace()
        position = string.find(source, "[^ \t\r\n]", position) or length + 1
    end
    local function unicode(code)
        if code < 128 then return string.char(code) end
        if code < 2048 then return string.char(192 + math.floor(code / 64), 128 + code % 64) end
        if code < 65536 then
            return string.char(224 + math.floor(code / 4096), 128 + math.floor(code / 64) % 64, 128 + code % 64)
        end
        return string.char(240 + math.floor(code / 262144), 128 + math.floor(code / 4096) % 64,
            128 + math.floor(code / 64) % 64, 128 + code % 64)
    end
    local function hex()
        local digits = source:sub(position, position + 3)
        if #digits ~= 4 or not digits:match("^%x%x%x%x$") then invalid() end
        position = position + 4
        return tonumber(digits, 16)
    end
    local decoded_escapes = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }
    local function string_value()
        if source:sub(position, position) ~= '"' then invalid() end
        position = position + 1
        local parts = {}
        while true do
            local stop = source:find('["\\%z\1-\31]', position)
            if stop == nil then invalid() end
            parts[#parts + 1] = source:sub(position, stop - 1)
            local character = source:sub(stop, stop)
            position = stop + 1
            if character == '"' then
                local value = table.concat(parts)
                quote(value, math.huge) -- Validate raw UTF-8 as well as escaped Unicode.
                return value
            end
            if character ~= "\\" then invalid() end
            local escaped = source:sub(position, position)
            position = position + 1
            if escaped == "u" then
                local code = hex()
                if code >= 55296 and code <= 56319 then
                    if source:sub(position, position + 1) ~= "\\u" then invalid() end
                    position = position + 2
                    local low = hex()
                    if low < 56320 or low > 57343 then invalid() end
                    code = 65536 + (code - 55296) * 1024 + low - 56320
                elseif code >= 56320 and code <= 57343 then invalid() end
                parts[#parts + 1] = unicode(code)
            else
                if decoded_escapes[escaped] == nil then invalid() end
                parts[#parts + 1] = decoded_escapes[escaped]
            end
        end
    end
    local function digits()
        local first, last = source:find("^%d+", position)
        if first == nil then invalid() end
        position = last + 1
    end
    parse = function(depth)
        if depth > 64 then invalid() end
        whitespace()
        local character = source:sub(position, position)
        if character == '"' then return string_value() end
        if character == "{" or character == "[" then
            local object = character == "{"
            local value = object and wire.object({}) or {}
            local closing = object and "}" or "]"
            position = position + 1
            whitespace()
            if source:sub(position, position) ~= closing then
                repeat
                    whitespace()
                    local key = object and string_value() or #value + 1
                    if object then
                        if value[key] ~= nil then invalid() end
                        whitespace()
                        if source:sub(position, position) ~= ":" then invalid() end
                        position = position + 1
                    end
                    value[key] = parse(depth + 1)
                    whitespace()
                    character = source:sub(position, position)
                    if character == closing then break end
                    if character ~= "," then invalid() end
                    position = position + 1
                until false
            end
            position = position + 1
            return value
        end
        for token, value in pairs({ ["true"] = true, ["false"] = false, ["null"] = json.null }) do
            if source:sub(position, position + #token - 1) == token then
                position = position + #token
                return value
            end
        end
        local first = position
        if character == "-" then position = position + 1 end
        if source:sub(position, position) == "0" then position = position + 1 else digits() end
        if source:sub(position, position) == "." then position = position + 1; digits() end
        character = source:sub(position, position)
        if character == "e" or character == "E" then
            position = position + 1
            character = source:sub(position, position)
            if character == "+" or character == "-" then position = position + 1 end
            digits()
        end
        local value = tonumber(source:sub(first, position - 1))
        if value == nil or value ~= value or value == math.huge or value == -math.huge then invalid() end
        return value
    end
    local value = parse(0)
    whitespace()
    if position <= length then invalid() end
    return value
end

function wire.response(callback, limit)
    limit = limit or maximum_line_bytes
    local ok, data = pcall(callback)
    local payload, failure
    if ok then
        ok, data = pcall(wire.encode, data, limit - #success_prefix - 1)
        if ok then
            payload = success_prefix .. data .. "}"
            if #payload > limit then
                failure = "response_too_large"
            end
        else
            failure = (data == "invalid_utf8" or data == "response_too_large")
                and data or "invalid_json_value"
        end
    else
        failure = data == indeterminate and "indeterminate" or "lua_error"
    end
    if failure ~= nil then
        payload = '{"ok":false,"error":"' .. failure .. '"}'
    end
    if #payload > limit then error("response_too_large", 0) end
    return payload
end

return wire
