-- Loaded avatars with native component methods used by list_players and get_player.
return function(rich)
    local function constant(value) return function() return value end end
    local skills = {}
    for _, group in ipairs({ "alchemy", "torch", "beard" }) do
        for level = 1, rich and 5 or 1 do
            skills["wilson_" .. group .. "_" .. level] = true
        end
    end
    local clients, players = {}, {}
    for index = 1, 64 do
        local userid = string.format("KU_client%04d", index)
        clients[index] = {
            userid = userid, netid = string.format("7656119800000%04d", index),
            name = string.format("玩家 Wilson %02d", index), prefab = "wilson",
            admin = index == 1, moderator = false, netscore = 47,
        }
        local function entity(prefab, offset)
            return { prefab = prefab, GUID = 100000 + index * 100 + offset }
        end
        local followers = {}
        for slot = 1, rich and 16 or 1 do followers[entity("spider_warrior", slot)] = true end
        players[index] = {
            userid = userid, prefab = "wilson", IsValid = constant(true), HasTag = constant(false),
            Transform = {GetWorldPosition = function() return index + 1/3, 0, -index - 2/3 end},
            components = {
                age = { GetAge = constant(29375.45), GetAgeInDays = constant(61), GetDisplayAgeInDays = constant(62) },
                health = { currenthealth = 113.27, maxhealth = 150, GetPercent = constant(113.27/150),
                    IsDead = constant(false), IsInvincible = constant(false) },
                hunger = { current = 114.35, max = 150, GetPercent = constant(114.35/150) },
                sanity = { current = 146.38, max = 200, GetPercent = constant(146.38/200) },
                temperature = { GetCurrent = constant(43.63), GetMax = constant(90) },
                moisture = { GetMoisture = constant(23.42), GetMaxMoisture = constant(100),
                    GetMoisturePercent = constant(23.42/100) },
                combat = { target = rich and entity("dragonfly", 17) or nil, GetWeapon = constant(entity("spear", 18)) },
                rider = { GetMount = constant(rich and entity("beefalo", 19) or nil) },
                leader = { followers = followers },
                skilltreeupdater = { GetSkillXP = constant(160), GetAvailableSkillPoints = constant(0),
                    GetActivatedSkills = constant(skills) },
            },
        }
    end
    AllPlayers = players
    GetPlayerClientTable = constant(clients)
    LookupPlayerInstByUserID = function(userid)
        for _, player in ipairs(players) do
            if player.userid == userid then return player end
        end
    end
end
