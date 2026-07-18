local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local player_events = {}

local function listen(player, event_name, callback)
    player:ListenForEvent(event_name, telemetry.guard(callback))
end

local function attach_lifecycle(player)
    listen(player, "ms_becameghost", function(_, data)
        telemetry.emit("dst.player.ghosted", {
            player = values.entity_ref(player),
            corpse = data ~= nil and data.corpse == true,
        })
    end)
    listen(player, "ms_respawnedfromghost", function(_, data)
        telemetry.emit("dst.player.revived", {
            player = values.entity_ref(player),
            corpse = data ~= nil and data.corpse == true,
            reviver = values.entity_ref(data ~= nil and data.reviver or nil),
        })
    end)
    listen(player, "onsink", function(_, data)
        telemetry.emit("dst.player.incident", {
            player = values.entity_ref(player),
            kind = "sink",
            source = values.entity_ref(data ~= nil and data.boat or nil),
            destination = values.position(data ~= nil and data.shore_pt or nil),
        })
    end)
    listen(player, "onfallinvoid", function(_, data)
        telemetry.emit("dst.player.incident", {
            player = values.entity_ref(player),
            kind = "fall_in_void",
            source = json.null,
            destination = values.position(data ~= nil and data.teleport_pt or nil),
        })
    end)
end

local function attach_combat(player)
    listen(player, "onhitother", function(_, data)
        local event = values.combat_data(player, data)
        event.target = values.entity_ref(data.target)
        event.damage_resolved = values.optional_json_number(data.damageresolved)
        event.redirected = values.entity_ref(data.redirected)
        telemetry.emit("dst.player.combat_hit", event)
    end)
    listen(player, "attacked", function(_, data)
        local event = values.combat_data(player, data)
        event.attacker = values.entity_ref(data.attacker)
        event.damage_resolved = values.optional_json_number(data.damageresolved)
        event.original_damage = values.optional_json_number(data.original_damage)
        event.redirected = values.entity_ref(data.redirected)
        telemetry.emit("dst.player.combat_received", event)
    end)
    listen(player, "blocked", function(_, data)
        local event = values.combat_data(player, data)
        event.attacker = values.entity_ref(data.attacker)
        event.original_damage = values.optional_json_number(data.original_damage)
        telemetry.emit("dst.player.combat_blocked", event)
    end)
end

local function attach_conditions(player)
    local function condition(name, active)
        telemetry.emit("dst.player.condition_changed", {
            player = values.entity_ref(player),
            condition = name,
            active = active,
        })
    end
    for _, item in ipairs({
        { "startstarving", "starving", true },
        { "stopstarving", "starving", false },
        { "startfreezing", "freezing", true },
        { "stopfreezing", "freezing", false },
        { "startoverheating", "overheating", true },
        { "stopoverheating", "overheating", false },
        { "startfiredamage", "fire_damage", true },
        { "stopfiredamage", "fire_damage", false },
        { "startlunarburn", "lunar_burn", true },
        { "stoplunarburn", "lunar_burn", false },
    }) do
        local event_name, condition_name, active = item[1], item[2], item[3]
        listen(player, event_name, function()
            condition(condition_name, active)
        end)
    end
    for _, item in ipairs({
        { "gosane", "sane" },
        { "goinsane", "insane" },
        { "goenlightened", "enlightened" },
    }) do
        local event_name, sanity = item[1], item[2]
        listen(player, event_name, function()
            telemetry.emit("dst.player.condition_changed", {
                player = values.entity_ref(player),
                condition = "sanity",
                state = sanity,
            })
        end)
    end
end

local function attach_skills_and_fishing(player)
    local function fished(method, data)
        local event = values.player_data(player)
        event.fish = values.item_ref(data.fish)
        event.method = method
        telemetry.emit("dst.player.fished", event)
    end
    listen(player, "fishingcollect", function(_, data) fished("inland", data) end)
    listen(player, "fishcaught", function(_, data) fished("ocean", data) end)
    listen(player, "onactivateskill_server", function(_, data)
        telemetry.emit("dst.player.skill_changed", {
            player = values.entity_ref(player),
            skill = tostring(data.skill),
            active = true,
        })
    end)
    listen(player, "ondeactivateskill_server", function(_, data)
        telemetry.emit("dst.player.skill_changed", {
            player = values.entity_ref(player),
            skill = tostring(data.skill),
            active = false,
        })
    end)
    listen(player, "houndwarning", function(_, warning_type)
        telemetry.emit("dst.player.hound_warning", {
            player = values.entity_ref(player),
            warning_type = warning_type,
        })
    end)
end

local function attach_inventory(player)
    local function crafted(kind, data)
        local event = values.player_data(player)
        event.item = values.item_ref(data.item)
        event.recipe = tostring(data.recipe.name)
        event.kind = kind
        event.skin = data.skin or json.null
        telemetry.emit("dst.player.crafted", event)
    end
    listen(player, "builditem", function(_, data) crafted("item", data) end)
    listen(player, "buildstructure", function(_, data) crafted("structure", data) end)
    listen(player, "oneat", function(_, data)
        local event = values.player_data(player)
        event.food = values.item_ref(data.food)
        event.feeder = values.entity_ref(data.feeder)
        telemetry.emit("dst.player.ate", event)
    end)
    listen(player, "picksomething", function(_, data)
        local event = values.player_data(player)
        event.source = values.entity_ref(data.object)
        event.loot = values.loot_refs(data.loot)
        telemetry.emit("dst.player.picked", event)
    end)
    listen(player, "harvestsomething", function(_, data)
        local event = values.player_data(player)
        event.source = values.entity_ref(data.object)
        telemetry.emit("dst.player.harvested", event)
    end)
    listen(player, "finishedwork", function(_, data)
        local event = values.player_data(player)
        event.target = values.entity_ref(data.target)
        event.action_id = tostring(data.action.id)
        telemetry.emit("dst.player.finished_work", event)
    end)
    listen(player, "deployitem", function(_, data)
        local event = values.player_data(player)
        event.prefab = tostring(data.prefab)
        telemetry.emit("dst.player.deployed", event)
    end)
    listen(player, "equip", function(_, data)
        local event = values.player_data(player)
        event.item = values.item_ref(data.item)
        event.slot = tostring(data.eslot)
        telemetry.emit("dst.player.equipped", event)
    end)
    listen(player, "unequip", function(_, data)
        local event = values.player_data(player)
        event.item = values.item_ref(data.item)
        event.slot = tostring(data.eslot)
        event.slip = data.slip == true
        telemetry.emit("dst.player.unequipped", event)
    end)
    listen(player, "dropitem", function(_, data)
        local event = values.player_data(player)
        event.item = values.item_ref(data.item)
        telemetry.emit("dst.player.dropped", event)
    end)
end

function player_events.attach(player)
    if player == nil or state.players[player] then
        return
    end
    state.players[player] = true
    attach_lifecycle(player)
    if state.profile == "history" then
        attach_combat(player)
        attach_conditions(player)
        attach_skills_and_fishing(player)
        attach_inventory(player)
    end
end

return player_events
