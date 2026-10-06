local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")
local values = require("dst_server.values")
local player_events = {}
local pending_revivers = setmetatable({}, { __mode = "k" })

local function listen(player, name, callback)
    player:ListenForEvent(name, telemetry.guard("player." .. name, callback))
end

local function emit(event, player, data)
    data.player = values.entity_ref(player)
    telemetry.emit(event, data)
end

local function revived(player, method, reviver)
    pending_revivers[player] = nil
    emit("dst.player.revived", player, {
        age_seconds = values.player_age(player), method = method, reviver = reviver or json.null,
    })
end

local function ghost_reviver(data)
    if not data then return end
    local reviver, source = data.user, data.source
    if not reviver and data.from_haunt == false and source and source.prefab == "pocketwatch_revive" then
        local item = source.components and source.components.inventoryitem
        reviver = item and item:GetGrandOwner()
    end
    if values.player_userid(reviver) then return values.entity_ref(reviver) end
end

local function attach_lifecycle(player)
    listen(player, "setowner", function()
        -- A seamless character swap clears the old entity's userid before departure.
        state.player_userids[player] = values.entity_userid(player) or state.player_userids[player]
    end)
    listen(player, "ms_skilltreeinitialized", function()
        emit("dst.player.loaded", player, {
            is_ghost = player:HasTag("playerghost"), age_seconds = values.player_age(player),
        })
    end)
    listen(player, "ms_becameghost", function(_, data)
        pending_revivers[player] = nil
        emit("dst.player.ghosted", player, { corpse = data ~= nil and data.corpse == true })
    end)
    listen(player, "attuned", function(_, data)
        if data and not data.isloading
            and (data.tag == "remoteresurrector" or data.tag == "gravestoneresurrector") then
            emit("dst.player.revival_bound", player, { source = data.prefab })
        end
    end)
    listen(player, "respawnfromghost", function(_, data)
        if not player:HasTag("playerghost") then return end
        -- A second request may race the first one's scheduled revival.
        pending_revivers[player] = pending_revivers[player] == nil and ghost_reviver(data) or false
    end)
    listen(player, "ms_respawnedfromghost", function(_, data)
        if data and data.corpse then
            revived(player, "corpse", values.entity_ref(data.reviver))
        else
            revived(player, "ghost", pending_revivers[player])
        end
    end)
end

local function attach_conditions(player)
    for _, condition in ipairs({
        { "startstarving", "stopstarving", "starving" },
        { "startfreezing", "stopfreezing", "freezing" },
        { "startoverheating", "stopoverheating", "overheating" },
    }) do
        local name = condition[3]
        listen(player, condition[1], function()
            emit("dst.player.condition_changed", player, { condition = name, active = true })
        end)
        listen(player, condition[2], function()
            emit("dst.player.condition_changed", player, { condition = name, active = false })
        end)
    end
    local fire_damage = false
    local function fire_changed(active)
        if fire_damage ~= active then
            fire_damage = active
            emit("dst.player.condition_changed", player, { condition = "fire_damage", active = active })
        end
    end
    listen(player, "firedamage", function() fire_changed(true) end)
    listen(player, "stopfiredamage", function() fire_changed(false) end)

    local has_burn_damage = require("prefabs/wagboss_util").HasLunarBurnDamage
    local health = player.components.health
    local flags = health and health:GetLunarBurnFlags() or 0
    local burning = flags ~= 0 and has_burn_damage(flags)
    local function lunar_burn_changed(_, flags)
        -- Klei also sends startlunarburn when a nearby supernova is blocked by cover.
        local active = flags ~= nil and has_burn_damage(flags)
        if burning ~= active then
            burning = active
            emit("dst.player.condition_changed", player, { condition = "lunar_burn", active = active })
        end
    end
    listen(player, "startlunarburn", lunar_burn_changed)
    listen(player, "stoplunarburn", lunar_burn_changed)
    for native, name in pairs({ gosane = "sane", goinsane = "insane", goenlightened = "enlightened" }) do
        listen(player, native, function()
            emit("dst.player.condition_changed", player, { condition = "sanity", state = name })
        end)
    end
    listen(player, "on_washed_ashore", function()
        emit("dst.player.incident", player, { kind = "washed_ashore" })
    end)
end

local contact_interval = 10

local function listen_combat(player, event, opponent_field)
    local incoming = opponent_field == "attacker"
    local period, recent, previous
    listen(player, event, function(_, data)
        local damage = data and data.damageresolved
        if type(damage) ~= "number" or not (damage > 0 and damage < math.huge) or data.redirected then return end
        local opponent = data[opponent_field]
        if not opponent or opponent == player or not opponent.prefab then return end

        local kind, key
        if values.is_player(opponent) then
            -- Native PvP notifies both players. The defender supplies the one record for this direction.
            if not incoming then return end
            kind = "pvp"
            key = values.player_userid(opponent) or opponent.GUID
        elseif not opponent:IsValid() then
            return
        elseif opponent:HasTag("epic") then
            kind, key = "epic", opponent.GUID
        else
            kind, key = "ordinary", opponent.prefab
        end

        local now = GetTime()
        local window = math.floor(now / contact_interval)
        if period ~= window then
            -- Only these two windows can contain a contact less than ten seconds old.
            previous = period == window - 1 and recent or nil
            recent, period = {}, window
        end
        local last = recent[key] or previous and previous[key]
        if last and now - last < contact_interval then return end

        local source, target = values.entity_ref(player), values.entity_ref(opponent)
        if source == json.null or target == json.null then return end
        if incoming then source, target = target, source end
        recent[key] = now
        telemetry.play("dst.player.combat_contact", {
            source = source, target = target, kind = kind, weapon = data.weapon and data.weapon.prefab or json.null,
        })
    end)
end

local function attach_play(player)
    attach_conditions(player)
    listen_combat(player, "onhitother", "target")
    listen_combat(player, "attacked", "attacker")
    local function result(event, data)
        data.player = values.entity_ref(player)
        telemetry.play(event, data)
    end
    local function ate(_, data)
        result("dst.player.ate", {
            food = (data.food or data.soul).prefab,
            feeder = data.feeder ~= player and values.entity_ref(data.feeder) or json.null,
        })
    end
    listen(player, "oneat", ate)
    listen(player, "oneatsoul", ate)
    local function crafted(kind, data)
        result("dst.player.crafted", { kind = kind, item = data.item.prefab, recipe = data.recipe.name })
    end
    listen(player, "builditem", function(_, data) crafted("item", data) end)
    listen(player, "buildstructure", function(_, data) crafted("structure", data) end)
    listen(player, "unlockrecipe", function(_, data)
        -- Builder also sends an empty unlockrecipe when it refreshes the recipe list.
        if data and data.recipe then
            result("dst.player.recipe_unlocked", { recipe = data.recipe })
        end
    end)
    local function fished(method, data)
        result("dst.player.fished", { method = method, fish = data.fish.prefab })
    end
    listen(player, "fishingcollect", function(_, data) fished("inland", data) end)
    listen(player, "fishcaught", function(_, data) fished("ocean", data) end)
    listen(player, "picksomethingfromaoe", function(_, data)
        if data.harvestedcount > 0 then
            result("dst.player.picked", { source = json.null, count = data.harvestedcount })
        end
    end)
    listen(player, "harvestsomething", function(_, data)
        result("dst.player.harvested", { source = values.entity_ref(data.object) })
    end)
    listen(player, "finishedwork", function(_, data)
        result("dst.player.finished_work", {
            target = values.entity_ref(data.target), action_id = data.action and data.action.id or json.null,
        })
    end)
    listen(player, "trade", function(_, data)
        if data.giver and data.giver:HasTag("player") then
            result("dst.player.traded", { giver = values.entity_ref(data.giver), item = data.item.prefab })
        end
    end)
    listen(player, "murdered", function(_, data)
        result("dst.player.creature_murdered", {
            victim = data.victim.prefab, count = data.stackmult,
            negligent = data.negligent == true, incinerated = data.incinerated == true,
        })
    end)
end

function player_events.attach(player)
    if player == nil or state.player_userids[player] ~= nil then return end
    state.player_userids[player] = values.entity_userid(player) or ""
    attach_lifecycle(player)
    if state.requested_profile == "play" then attach_play(player) end
end

function player_events.shard_left(_, player)
    if player.is_snapshot_user_session then return end
    pending_revivers[player] = nil
    emit("dst.player.shard_left", player, {})
end

function player_events.install_lifecycle()
    local extensions = require("prefabs/player_common_extensions")
    local original = extensions.OnRespawnFromVineSave
    assert(type(original) == "function", "OnRespawnFromVineSave is unavailable")
    -- The vine rescue restores health without emitting ms_respawnedfromghost.
    local capture = telemetry.guard("player.charlie_revived", function(player)
        revived(player, "charlie")
    end)
    local function completed(player, ...)
        capture(player)
        return ...
    end
    extensions.OnRespawnFromVineSave = function(...)
        local player = ...
        return completed(player, original(...))
    end
end

return player_events
