local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local message_events = {}

function message_events.install()
    local say, announcement = Networking_Say, Networking_Announcement
    local skin, system, roll = Networking_SkinAnnouncement, Networking_SystemMessage, Networking_RollAnnouncement
    assert(type(say) == "function", "Networking_Say is unavailable")
    assert(type(announcement) == "function", "Networking_Announcement is unavailable")
    assert(type(skin) == "function", "Networking_SkinAnnouncement is unavailable")
    assert(type(system) == "function", "Networking_SystemMessage is unavailable")
    assert(type(roll) == "function", "Networking_RollAnnouncement is unavailable")

    local capture_chat = telemetry.guard("player.chat", function(guid, userid, name, prefab, message, colour, whisper, emote)
        -- Match native acceptance, without the client HUD's whisper-distance filter.
        if message == nil or message:utf8len() > MAX_CHAT_INPUT_LENGTH then return end
        telemetry.emit("dst.player.chat", {
            userid = userid or json.null,
            name = name or json.null,
            prefab = (prefab ~= "" and prefab) or json.null,
            message = message,
            whisper = not not whisper,
            emote = not not emote,
            player = values.entity_ref(Ents[guid]),
        })
    end)
    Networking_Say = function(...)
        local results = telemetry.pack(say(...))
        capture_chat(...)
        return telemetry.unpack(results)
    end

    local capture_announcement = telemetry.guard("server.announcement", function(message, colour, kind)
        if not message then return end
        telemetry.emit("dst.server.announcement", {
            kind = (kind and kind ~= "") and kind or "default",
            message = message,
        })
    end)
    Networking_Announcement = function(...)
        local results = telemetry.pack(announcement(...))
        capture_announcement(...)
        return telemetry.unpack(results)
    end

    local capture_skin = telemetry.guard("player.skin_received", function(name, colour, skin_name)
        if not (name and colour and skin_name) then return end
        -- The native callback supplies a display name, not an account identity.
        telemetry.emit("dst.player.skin_received", { name = name, skin = skin_name })
    end)
    Networking_SkinAnnouncement = function(...)
        local results = telemetry.pack(skin(...))
        capture_skin(...)
        return telemetry.unpack(results)
    end

    local capture_system = telemetry.guard("server.system_message", function(message)
        if not message then return end
        telemetry.emit("dst.server.system_message", { message = message })
    end)
    Networking_SystemMessage = function(...)
        local results = telemetry.pack(system(...))
        capture_system(...)
        return telemetry.unpack(results)
    end

    local capture_roll = telemetry.guard("player.dice_rolled", function(userid, name, prefab, colour, rolls, maximum)
        telemetry.emit("dst.player.dice_rolled", {
            userid = userid or json.null,
            name = name or json.null,
            prefab = (prefab ~= "" and prefab) or json.null,
            rolls = rolls,
            max = maximum,
        })
    end)
    Networking_RollAnnouncement = function(...)
        local results = telemetry.pack(roll(...))
        -- The announcement preserves displayed text; this fact preserves native dice values.
        capture_roll(...)
        return telemetry.unpack(results)
    end
end

return message_events
