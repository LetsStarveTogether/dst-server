local function null() end

local function encodable(value)
    local kind = type(value)
    return kind == "string"
        or kind == "boolean"
        or kind == "number"
        or kind == "nil"
        or kind == "table"
        or value == null
end

local function is_array(value)
    local maximum = 0
    for key, item in pairs(value) do
        if type(key) == "number" and key >= 1 and key == math.floor(key) then
            if not encodable(item) then
                return false
            end
            maximum = math.max(maximum, key)
        elseif key == "n" then
            if item ~= #value then
                return false
            end
        elseif encodable(item) then
            return false
        end
    end
    return true, maximum
end

local function encode(value)
    if value == nil or value == null then
        return "null"
    end

    local kind = type(value)
    if kind == "string" then
        -- Match DST: encode_compliant leaves NUL, backspace, and form feed raw.
        local escaped = value:gsub('\\', '\\\\')
        escaped = escaped:gsub('"', '\\"')
        escaped = escaped:gsub('\n', '\\n')
        escaped = escaped:gsub('\t', '\\t')
        escaped = escaped:gsub('\r', '\\r')
        return '"' .. escaped .. '"'
    end
    if kind == "number" then
        return tostring(value)
    end
    if kind == "boolean" then
        return tostring(value)
    end
    if kind ~= "table" then
        error("encode_compliant attempt to encode unsupported type " .. kind)
    end

    local array, length = is_array(value)
    if array then
        local items = {}
        for index = 1, length do
            items[index] = encode(value[index])
        end
        return "[" .. table.concat(items, ",") .. "]"
    end

    local keys = {}
    for key, item in pairs(value) do
        if encodable(key) and encodable(item) then
            keys[#keys + 1] = key
        end
    end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    local fields = {}
    for index, key in ipairs(keys) do
        fields[index] = encode(tostring(key)) .. ":" .. encode(value[key])
    end
    return "{" .. table.concat(fields, ",") .. "}"
end

return {
    null = null,
    decode = function(value) return value end,
    encode_compliant = encode,
}
