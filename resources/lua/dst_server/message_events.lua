local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local message_events = {}

function message_events.install_public_chat()
    local say = Networking_Say
    assert(type(say) == "function", "Networking_Say is unavailable")
    local capture_chat = telemetry.guard("player.public_chat", function(guid, userid, name, prefab, message, _, _, emote)
        if type(message) ~= "string" or #message > MAX_CHAT_INPUT_LENGTH and message:utf8len() > MAX_CHAT_INPUT_LENGTH then
            return
        end
        local sender = values.userid(userid)
        if sender == nil then
            local player = Ents[guid]
            if player ~= nil and player:IsValid() and player:HasTag("player") then
                sender = values.entity_userid(player)
            end
        end
        telemetry.emit("dst.player.public_chat", {
            userid = sender or json.null,
            name = name or json.null,
            prefab = prefab ~= "" and prefab or json.null,
            message = message,
            emote = not not emote,
        })
    end)
    Networking_Say = function(...)
        if select(7, ...) then return say(...) end
        local results = telemetry.pack(say(...))
        capture_chat(...)
        return unpack(results, 1, results.n)
    end
end

function message_events.install_dice()
    local roll = Networking_RollAnnouncement
    assert(type(roll) == "function", "Networking_RollAnnouncement is unavailable")
    local capture_roll = telemetry.guard("player.dice_rolled", function(userid, name, prefab, _, rolls, maximum)
        telemetry.emit("dst.player.dice_rolled", {
            userid = values.userid(userid) or json.null,
            name = name or json.null,
            prefab = prefab ~= "" and prefab or json.null,
            rolls = rolls,
            max = maximum,
        })
    end)
    Networking_RollAnnouncement = function(...)
        local results = telemetry.pack(roll(...))
        capture_roll(...)
        return unpack(results, 1, results.n)
    end
end

return message_events
