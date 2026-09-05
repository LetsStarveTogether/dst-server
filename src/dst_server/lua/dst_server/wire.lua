local json = require("json")
local wire = {}
local object_marker = {}
local prefix = "DST_SERVER_RESULT|"
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

local function quote(value)
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
    return '"' .. string.gsub(value, '[%z\1-\31\\"]', escapes) .. '"'
end

function wire.encode(value)
    local parts, seen = {}, {}

    local function append(text)
        parts[#parts + 1] = text
    end

    local function encode(item)
        if item == nil or item == json.null then
            append("null")
            return
        end
        local kind = type(item)
        if kind == "string" then
            append(quote(item))
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
                append(quote(key))
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

function wire.reply(callback)
    local ok, data = pcall(callback)
    local payload, failure
    if ok then
        ok, data = pcall(wire.encode, data)
        if ok then
            payload = '{"ok":true,"data":' .. data .. "}"
            if #prefix + #payload > maximum_line_bytes then
                failure = "response_too_large"
            end
        else
            failure = data == "invalid_utf8" and "invalid_utf8" or "invalid_json_value"
        end
    else
        failure = "lua_error"
    end
    if failure ~= nil then
        payload = '{"ok":false,"error":"' .. failure .. '"}'
    end
    print(prefix .. payload)
end

return wire
