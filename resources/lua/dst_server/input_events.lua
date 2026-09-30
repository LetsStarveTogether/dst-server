local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local input_events = {}

function input_events.install_commands()
    if type(GetCommonEmotes) ~= "function" or type(EMOTE_ITEMS) ~= "table" then return false end
    local commands = require("usercommands")
    local function wrap(name, event_name)
        local command = assert(commands.GetCommandFromName(name), "user command is unavailable: " .. name)
        local original = command.serverfn
        assert(type(original) == "function", "user command serverfn is unavailable: " .. name)
        local capture = telemetry.guard("player." .. event_name, function(_, caller)
            local data = {
                userid = caller.userid,
                player = values.entity_ref(UserToPlayer(caller.userid)),
            }
            if event_name == "emote_requested" then data.emote = command.name end
            telemetry.emit("dst.player." .. event_name, data)
        end)
        command.serverfn = function(...)
            -- Native permission checks and queue limits have already accepted the request.
            -- This does not assert that the stategraph played an animation or moved a player.
            capture(...)
            return original(...)
        end
    end
    for name in pairs(GetCommonEmotes()) do wrap(name, "emote_requested") end
    for _, item in pairs(EMOTE_ITEMS) do wrap(item.cmd_name, "emote_requested") end
    wrap("rescue", "rescue_requested")
end

local function appearance(prefab, skin, body, hand, legs, feet)
    local function optional(value) return value ~= nil and value ~= "" and value or json.null end
    return {
        prefab = prefab,
        skin_base = optional(skin),
        clothing_body = optional(body),
        clothing_hand = optional(hand),
        clothing_legs = optional(legs),
        clothing_feet = optional(feet),
    }
end

function input_events.install_appearance()
    local original = ValidateSpawnPrefabRequest
    if type(original) ~= "function" then return false end
    local capture = telemetry.guard("player.appearance_requested", function(validated, userid, ...)
        telemetry.emit("dst.player.appearance_requested", {
            userid = userid,
            player = values.entity_ref(UserToPlayer(userid)),
            requested = appearance(...),
            validated = appearance(telemetry.unpack(validated)),
        })
    end)
    ValidateSpawnPrefabRequest = function(...)
        local results = telemetry.pack(original(...))
        -- Validation may replace an unowned skin or an invalid character; preserve both facts.
        capture(results, ...)
        return telemetry.unpack(results)
    end
end

return input_events
