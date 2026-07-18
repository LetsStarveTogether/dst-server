local values = require("dst_server.values")
local queries = {}

function queries.get_runtime()
    if TheWorld == nil or TheWorld.meta == nil then
        error("world metadata unavailable")
    end
    local meta = TheWorld.meta
    return {
        session_id = tostring(meta.session_identifier),
        snapshot = TheNet:GetCurrentSnapshot(),
        build_version = tostring(meta.build_version),
        save_version = meta.saveversion,
        generated_on_save_version = meta.generated_on_saveversion,
        seed = meta.seed,
        level_id = tostring(meta.level_id),
        branch = tostring(BRANCH),
        app_version = tostring(APP_VERSION),
        shard_id = tostring(TheShard:GetShardId()),
        is_master_shard = TheWorld.ismastershard == true,
        is_cave = TheWorld:HasTag("cave"),
    }
end

function queries.get_mods()
    local result = {}
    for _, id in ipairs(ModManager:GetEnabledModNames()) do
        local info = KnownModIndex:GetModInfo(id)
        result[#result + 1] = {
            id = tostring(id),
            name = tostring(info ~= nil and info.name or id),
            version = tostring(info ~= nil and info.version or ""),
        }
    end
    table.sort(result, function(a, b) return a.id < b.id end)
    return result
end

function queries.get_room()
    if TheWorld == nil then
        error("world unavailable")
    end
    return {
        name = TheNet:GetServerName(),
        description = TheNet:GetServerDescription(),
        game_mode = TheNet:GetServerGameMode(),
        playstyle = TheNet:GetServerPlaystyle() or json.null,
        max_players = TheNet:GetServerMaxPlayers(),
        player_count = #GetPlayerClientTable(),
        pvp = TheNet:GetServerPVP() == true,
        is_paused = TheNet:IsServerPaused(true) == true,
        has_password = TheNet:GetServerHasPassword() == true,
        is_dedicated = TheNet:GetServerIsDedicated() == true,
        is_online = TheNet:IsOnlineMode() == true,
        lan_only = TheNet:GetServerLANOnly() == true,
        friends_only = TheNet:GetServerFriendsOnly() == true,
        mods_enabled = TheNet:GetServerModsEnabled() == true,
        clan_id = TheNet:GetServerClanID(),
        clan_only = TheNet:GetServerClanOnly() == true,
        shard_id = tostring(TheShard:GetShardId()),
        is_master_shard = TheWorld.ismastershard == true,
    }
end

function queries.get_world()
    if TheWorld == nil or TheWorld.components.worldstate == nil then
        error("world state unavailable")
    end
    local state = TheWorld.state
    local cave = TheWorld:HasTag("cave")
    return {
        age = TheWorld.components.worldstate:GetWorldAge(),
        cycles = state.cycles,
        day = state.cycles + 1,
        time = state.time,
        time_in_phase = state.timeinphase,
        phase = cave and state.cavephase or state.phase,
        is_day = (cave and state.iscaveday or not cave and state.isday) == true,
        is_dusk = (cave and state.iscavedusk or not cave and state.isdusk) == true,
        is_night = (cave and state.iscavenight or not cave and state.isnight) == true,
        moon_phase = cave and state.cavemoonphase or state.moonphase,
        is_waxing_moon = (
            cave and state.iscavewaxingmoon or not cave and state.iswaxingmoon
        ) == true,
        is_full_moon = (
            cave and state.iscavefullmoon or not cave and state.isfullmoon
        ) == true,
        is_new_moon = (
            cave and state.iscavenewmoon or not cave and state.isnewmoon
        ) == true,
        season = state.season,
        is_spring = state.isspring,
        is_summer = state.issummer,
        is_autumn = state.isautumn,
        is_winter = state.iswinter,
        elapsed_days_in_season = state.elapseddaysinseason,
        season_progress = state.seasonprogress,
        remaining_days_in_season = state.remainingdaysinseason,
        spring_length = state.springlength,
        summer_length = state.summerlength,
        autumn_length = state.autumnlength,
        winter_length = state.winterlength,
        temperature = state.temperature,
        moisture = state.moisture,
        moisture_ceiling = state.moistureceil,
        precipitation_probability = state.pop,
        precipitation_rate = state.precipitationrate,
        precipitation = state.precipitation,
        is_raining = state.israining,
        is_snowing = state.issnowing,
        is_lunar_hailing = state.islunarhailing,
        is_acid_raining = state.isacidraining,
        is_snow_covered = state.issnowcovered,
        snow_level = state.snowlevel,
        lunar_hail_level = state.lunarhaillevel,
        lunar_hail_rate = state.lunarhailrate,
        wetness = state.wetness,
        is_wet = state.iswet,
        is_cave = cave,
    }
end

function queries.get_shards(args)
    local result = {
        {
            id = tostring(TheShard:GetShardId()),
            name = values.required_string(args, "current_name"),
            is_current = true,
            ready = true,
            tags = {},
        },
    }
    for id, shard in pairs(Shard_GetConnectedShards()) do
        result[#result + 1] = {
            id = tostring(id),
            name = shard.shard_name or "",
            is_current = false,
            ready = shard.ready == true,
            tags = values.tags(shard.tags),
        }
    end
    table.sort(result, function(a, b)
        if a.is_current ~= b.is_current then
            return a.is_current
        end
        return a.id < b.id
    end)
    return result
end

return queries
