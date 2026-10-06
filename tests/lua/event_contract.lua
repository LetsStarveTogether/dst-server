-- Drive real game listeners and components, then feed their wire frames to the Rust/Python decoders.
local root, scripts, profile = assert(arg[1]), assert(arg[2]), assert(arg[3])
dofile(root .. "/tests/lua/setup.lua")(root, scripts)
require("class")
require("vector3")
require("scheduler")
Entity = {}
require("entityscript")
require("bufferedaction")

local function noop() end
global = noop
STRINGS = {ACTIONS = {}}
package.loaded["prefabs/weed_defs"] = {weighted_seed_table = {}}
require("actions")

local function pack(...) return {n = select("#", ...), ...} end
local function entity(prefab, guid, userid)
    return setmetatable({
        prefab = prefab, GUID = guid, userid = userid, isplayer = userid ~= nil,
        components = {}, replica = {}, tags = {},
        Transform = {GetWorldPosition = function() return 1, 0, 2 end},
        IsValid = function() return true end,
        GetPosition = function() return Vector3(1, 0, 2) end,
        HasTag = function(self, tag) return tag == "player" and userid ~= nil or self.tags[tag] == true end,
    }, {__index = EntityScript})
end
local emitted = {}
local function records(name) return emitted[name] or {} end
local function latest(name)
    local list = records(name)
    return assert(list[#list], "missing " .. name)
end
local frames = {}
local nonce = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
dofile(root .. "/tests/lua/capture_remote.lua")(function(frame)
    assert(frame:sub(1, 9) == "DST_OTEL|" and #frame < 65536)
    local record = json.decode(frame:sub(10))
    assert(record.nonce == nonce and record.generation == 1 and record.seq == #frames + 1)
    local list = records(record.event)
    list[#list + 1] = record.data
    emitted[record.event] = list
    frames[#frames + 1] = frame
end)

local player = entity("wilson", 2, "KU_PLAYER")
local friend = entity("wendy", 3, "KU_FRIEND")
local plant = entity("berrybush_juicy", 4)
local fruit = entity("berries_juicy", 5)
local frog = entity("frog", 6)
local rift = entity("lunarrift_portal", 7)
TheWorld = entity("forest", 1)
TheWorld.ismastersim, TheWorld.ismastershard = true, true
TheWorld.meta, TheWorld.state = {session_identifier = "SESSION"}, {cycles = 1, phase = "day"}
TheWorld.components.riftspawner = {
    GetLunarRiftsEnabled = function() return true end,
    GetShadowRiftsEnabled = function() return false end,
}
local watchers, snapshots = {}, {}
TheWorld.WatchWorldState = function(_, name, fn) watchers[name] = fn end
TheWorld.DoStaticPeriodicTask = function(_, seconds, fn, delay)
    assert(seconds == 60 and delay == 0)
    snapshots[#snapshots + 1] = fn
    return { Cancel = noop }
end
TheWorld.net = {components = {worldvoter = {}}}
AllPlayers, Ents = {player}, {[2] = player, [3] = friend}
local now, tick, static_tick = 0, 1, 1
GetTimeReal = function() return now end
GetTick, GetStaticTick = function() return tick end, function() return static_tick end
GetTickTime = function() return 1 / 30 end
GetTime, GetStaticTime = function() return tick / 30 end, function() return static_tick / 30 end
toarrayornil = function(...) return select("#", ...) > 0 and {...} or nil end
local function advance(paused)
    now, static_tick = now + 33, static_tick + 1
    if not paused then tick = tick + 1 end
    staticScheduler:OnTick(static_tick)
end
local clients, roster_reads, id_lookups = {}, 0, 0
GetPlayerClientTable = function() roster_reads = roster_reads + 1; return clients end
TheNet = {
    GetServerMaxPlayers = function() return 4 end,
    IsServerPaused = function() return false end,
    GetClientTableForUser = function()
        id_lookups = id_lookups + 1
        error("gameplay must not look up account metadata")
    end,
}
Networking_ModOutOfDateAnnouncement, Networking_Say, Networking_RollAnnouncement = noop, noop, noop
OnSimPaused, OnSimUnpaused, c_save = noop, noop, noop
MAX_CHAT_INPUT_LENGTH = 150
string.utf8len = function(value) return #value end
POSTACTIVATEHANDSHAKE = {NONE = 0, READY = 4}
package.loaded.cooking = {GetRecipe = function() return {} end}
local commands = {
    GetCommandFromHash = function() return {name = "kick"} end,
    GetCommandFromName = function() return {name = "kick"} end,
    FinishVote = function() return true end,
}
package.loaded.usercommands = commands
local pickable = require("components/pickable")
local stewer = require("components/stewer")
local native_pick = pickable.Pick
-- Mods can extend native returns with embedded and trailing nils.
pickable.Pick = function(self, picker)
    local done, loot = native_pick(self, picker)
    return done, loot, nil, "mod result", nil
end
local pick, harvest, action = pickable.Pick, stewer.Harvest, BufferedAction.Do
local deployable = require("components/deployable")
local heal, deploy = ACTIONS.HEAL.fn, deployable.Deploy
local walk, attack = ACTIONS.WALKTO.fn, ACTIONS.ATTACK.fn
local combat = require("components/combat")
local get_attacked, resolve, do_attack = combat.GetAttacked, combat.GetAttacked_Internal, combat.DoAttack
local driver = require("dst_server")
local options = { nonce = nonce, generation = 1, profile = profile }
local health = driver.install(options)
assert(health.capabilities.public_chat == (profile == "off" and "disabled" or "active"))
local optional = profile == "play" and "active" or "disabled"
assert(health.capabilities.actions == optional and health.capabilities.deployment == optional)
local connections = require("dst_server.connections")
connections.install(TheWorld)
connections.start(TheWorld)
if profile ~= "off" then require("dst_server.vote_events").install() end
assert(not pcall(driver.install, options), "a second install must not stack wrappers")
assert(BufferedAction.Do == action and ACTIONS.WALKTO.fn == walk and ACTIONS.ATTACK.fn == attack)
assert(combat.GetAttacked == get_attacked and combat.GetAttacked_Internal == resolve and combat.DoAttack == do_attack)
assert((ACTIONS.HEAL.fn ~= heal) == (profile == "play") and (deployable.Deploy ~= deploy) == (profile == "play"))
assert(profile == "play" or (pickable.Pick == pick and stewer.Harvest == harvest))
local listeners = player.event_listeners or {}
assert(not listeners.newstate)
assert((listeners.attacked ~= nil) == (profile == "play") and (listeners.onhitother ~= nil) == (profile == "play"))
if arg[4] == "burst" then
    local reads_before = roster_reads
    dofile(root .. "/tests/lua/play_burst.lua")(entity, advance, frames)
    assert(roster_reads == reads_before and id_lookups == 0, "gameplay must use local player identity")
    return
end

-- A lobby visit entirely between presence polls still renews the control clock.
now = 10000
TheWorld:PushEvent("ms_clientauthenticationcomplete", {userid = "KU_LOBBY"})
now = 12000
TheWorld:PushEvent("ms_clientdisconnected", {userid = "KU_LOBBY"})
now, AllPlayers = 15000, {}
assert(roster_reads == 0 and id_lookups == 0)
local control = driver.call("presence")
assert(control.reliable and control.idle_seconds == 3 and control.day == 2 and roster_reads == 1)
Networking_ModOutOfDateAnnouncement("ExampleMod")
assert(driver.call("presence").outdated_mods[1] == "ExampleMod")
OnSimPaused()
assert(not pcall(driver.call, "save", {}))
OnSimUnpaused()
assert(driver.call("save", {}))
if profile == "off" then
    assert(#snapshots == 0 and not player.event_listeners and next(watchers) == nil)
    TheWorld:PushEvent("ms_playerjoined", player)
    player:PushEvent("ms_skilltreeinitialized")
    for _ = 1, 64 do player:PushEvent("oneat", {food = fruit}) end
    Networking_Say(2, "KU_PLAYER", "Will", "wilson", "hello")
    assert(#frames == 0 and id_lookups == 0 and not next(TheWorld.pendingtasks or {}))
    return
end

AllPlayers, clients = {player}, {{userid = "KU_PLAYER"}, {userid = "KU_LOBBY"}}
assert(#snapshots == 1)
player._PostActivateHandshakeState_Server = POSTACTIVATEHANDSHAKE.NONE
snapshots[1]()
local initial = latest("dst.server.presence")
assert(initial.reason == "startup" and initial.players[1].loaded == false and #initial.clients == 2)
assert(initial.world.lunar_rifts_enabled == true and initial.world.shadow_rifts_enabled == false)
player._PostActivateHandshakeState_Server = POSTACTIVATEHANDSHAKE.READY
player:PushEvent("ms_skilltreeinitialized")
snapshots[1]()
assert(latest("dst.server.presence").players[1].loaded == true)
player._PostActivateHandshakeState_Server = nil
snapshots[1]()
assert(latest("dst.server.presence").players[1].loaded == nil)
assert(latest("dst.player.loaded").player.userid == "KU_PLAYER")
TheWorld:PushEvent("ms_playerdespawnandmigrate", {player = player, worldid = 2})
TheWorld:PushEvent("serverpauseddirty", {pause = true, autopause = true})
TheWorld:PushEvent("master_shardbossdefeated", {bossprefab = "daywalker", shardid = 2})
TheWorld:PushEvent("lunarrift_opened")
do
    TILE_SCALE = 4
    local spawner = setmetatable({
        inst = TheWorld, rifts = {}, rifts_count = 0, spawnmode = 1,
        _worldsettingstimer = {ActiveTimerExists = function() return false end},
    }, {__index = require("components/riftspawner")})
    local saved = entity("lunarrift_portal", 70)
    POPULATING = true
    spawner:LoadPostPass({[saved.GUID] = {entity = saved}}, {rift_guids = {saved.GUID}})
    POPULATING = false
    assert(spawner.rifts_count == 1 and #records("dst.world.rift_changed") == 0)
    spawner:AddRiftToPool(rift, rift.prefab)
    saved:PushEvent("onremove")
    local changes = records("dst.world.rift_changed")
    assert(#changes == 2 and changes[1].active == true and changes[1].rift.guid == rift.GUID)
    assert(changes[2].active == false and changes[2].rift.guid == saved.GUID)
end
watchers.cycles(TheWorld, 2)

TheWorld:PushEvent("entity_death", {inst = player, afflicter = frog, cause = "frog"})
assert(latest("dst.entity.death").victim.userid == "KU_PLAYER")
local follower = entity("pigman", 9)
follower.components.follower = {GetLeader = function() return friend end}
TheWorld:PushEvent("entity_death", {inst = frog, afflicter = follower})
assert(#records("dst.entity.death") == (profile == "play" and 2 or 1))
if profile == "play" then
    local kill = latest("dst.entity.death")
    assert(kill.afflicter.prefab == "pigman" and kill.attributed_userid == "KU_FRIEND")
    local tornado = entity("tornado", 13)
    local deaths = records("dst.entity.death")
    local before = #deaths
    TheWorld:PushEvent("entity_death", {inst = frog, afflicter = tornado})
    assert(#deaths == before)
    tornado.WINDSTAFF_CASTER = player
    TheWorld:PushEvent("entity_death", {inst = frog, afflicter = tornado})
    assert(#deaths == before + 1 and deaths[#deaths].attributed_userid == player.userid)
    assert(deaths[#deaths].afflicter.prefab == "tornado")
end
do
    CanEntityBecomeCorpse = function() return false end
    NotifyPlayerProgress = noop
    local function kill(victim, attacker)
        local hp = setmetatable({inst = victim, currenthealth = 1, maxhealth = 1, penalty = 0, nofadeout = true},
            {__index = require("components/health")})
        hp:SetVal(0, attacker.prefab, attacker)
        assert(hp:IsDead())
    end
    local catapult = entity("winona_catapult", 71)
    catapult._engineerid = friend.userid
    local bernie = entity("bernie_big", 76)
    bernie.brain, bernie.bernieleader = {_leader = friend}, friend
    local deaths = records("dst.entity.death")
    for _, attacker in ipairs({catapult, bernie}) do
        local before = #deaths
        kill(frog, attacker)
        if profile == "play" then
            assert(#deaths == before + 1 and deaths[#deaths].attributed_userid == friend.userid)
            assert(deaths[#deaths].afflicter.guid == attacker.GUID and deaths[#deaths].afflicter.userid == nil)
        else
            assert(#deaths == before)
        end
    end
    local projectile = entity("winona_catapult_projectile", 72)
    projectile.components.complexprojectile = {attacker = catapult}
    projectile.caster = player
    local boss = entity("deerclops", 73)
    boss.tags.epic = true
    bernie.brain._leader = nil
    kill(boss, bernie)
    assert(deaths[#deaths].afflicter.guid == bernie.GUID and deaths[#deaths].attributed_userid == nil)
    kill(boss, projectile)
    local death = deaths[#deaths]
    assert(death.victim.guid == boss.GUID and death.afflicter.guid == projectile.GUID)
    assert(death.afflicter.userid == nil and death.attributed_userid == player.userid)
    projectile.caster = nil
    kill(boss, projectile)
    assert(deaths[#deaths].attributed_userid == friend.userid)
    local vines = entity("trap_vines", 75)
    vines.attacker, vines.caster = catapult, player
    kill(boss, vines)
    assert(deaths[#deaths].afflicter.guid == vines.GUID and deaths[#deaths].attributed_userid == player.userid)
end
player.tags.playerghost = true
player:PushEvent("ms_becameghost")
do
    local function revive(request)
        local before = #records("dst.player.revived")
        if request then player:PushEvent("respawnfromghost", request) end
        player:PushEvent("ms_respawnedfromghost")
        assert(#records("dst.player.revived") == before + 1)
        return latest("dst.player.revived").reviver
    end
    local wanda = entity("wanda", 77, "KU_WANDA")
    local watch = entity("pocketwatch_revive", 78)
    watch.components.inventoryitem = setmetatable({owner = wanda}, {__index = require("components/inventoryitem")})
    assert(revive({user = friend}).userid == friend.userid)
    assert(revive({source = watch, from_haunt = false}).userid == wanda.userid)
    assert(revive() == nil, "previous rescuers cannot leak to the next revival")
    assert(revive({source = watch, from_haunt = true}) == nil)
    player:PushEvent("respawnfromghost", {user = friend})
    assert(revive({source = watch, from_haunt = false}) == nil, "two rescuers are ambiguous")
end
player.tags.playerghost = nil

player.components.attuner = {}
SpawnPrefab = function(prefab)
    assert(prefab == "attunable_classified")
    return {AddTag = noop, Remove = noop, AttachToPlayer = noop}
end
local function bind(prefab, tag, loading)
    local source = entity(prefab, 18)
    source.AddTag = noop
    local attunable = require("components/attunable")(source)
    attunable:SetAttunableTag(tag)
    assert(attunable:LinkToPlayer(player, loading) and not attunable:LinkToPlayer(player))
    attunable:UnlinkFromPlayer(player)
end
bind("resurrectionstatue", "remoteresurrector", true)
bind("other", "other")
assert(#records("dst.player.revival_bound") == 0)
bind("resurrectionstatue", "remoteresurrector")
bind("wendy_resurrectiongrave", "gravestoneresurrector")
local bindings = records("dst.player.revival_bound")
assert(#bindings == 2 and bindings[1].source == "resurrectionstatue")
assert(bindings[2].source == "wendy_resurrectiongrave" and bindings[2].player.userid == player.userid)

local before_chat = #frames
Networking_Say(2, "KU_PLAYER", "Will", "wilson", "secret", nil, true, false)
assert(#frames == before_chat)
Networking_Say(2, "KU_PLAYER", "Will", "wilson", "hello", nil, false, false)
Networking_RollAnnouncement("KU_PLAYER", "Will", "wilson", nil, {2, 6}, 6)
assert(#records("dst.player.public_chat") == 1 and latest("dst.player.public_chat").message == "hello")
TheWorld:PushEvent("master_worldvoterupdate", {commandid = 42, starteruserid = "KU_PLAYER", targetuserid = "KU_FRIEND"})
TheWorld:PushEvent("master_worldvoterupdate", {commandid = 0})
commands.FinishVote("kick", {user = "KU_FRIEND", username = "Friend"}, {})

if profile == "play" then
    advance()
    local drops = 0
    plant.components.lootdropper = {SpawnLootPrefab = function() drops = drops + 1 end}
    local bush = setmetatable({inst = plant, droppicked = true, numtoharvest = 3, product = fruit.prefab,
        canbepicked = true, caninteractwith = true}, {__index = pickable})
    local returns = pack(bush:Pick(player))
    assert(returns.n == 5 and returns[1] and returns[2] == nil and returns[4] == "mod result")
    assert(not bush:Pick(player) and drops == 3)
    local single = latest("dst.player.picked")
    assert(#records("dst.player.picked") == 1 and single.source == plant.prefab and single.count == 1)
    bush.canbepicked = true
    assert(bush:Pick(TheWorld))
    player:PushEvent("picksomethingfromaoe", {harvestedcount = 4})
    assert(#records("dst.player.picked") == 2 and latest("dst.player.picked").count == 4)
    local failure = {}
    bush.canbepicked, bush.onpickedfn = true, function() error(failure) end
    local ok, err = pcall(bush.Pick, bush, player)
    assert(not ok and err == failure and #records("dst.player.picked") == 2)

    local delivered = 0
    player.components.inventory = {GiveItem = function() delivered = delivered + 1 end}
    SpawnPrefab = function(prefab) return entity(prefab, 10) end
    local pot = setmetatable({inst = plant, done = true, product = "meatballs", chef_id = friend.userid}, {__index = stewer})
    assert(pot:Harvest(player) and not pot:Harvest(player) and delivered == 1)
    assert(#records("dst.player.dish_harvested") == 1)
    local dish = latest("dst.player.dish_harvested")
    assert(dish.chef_userid == friend.userid and dish.player.userid == player.userid and dish.product == "meatballs")

    fruit.components.inventoryitem = {RemoveFromOwner = noop}
    local trader = setmetatable({inst = player, enabled = true, test = function() return false end},
        {__index = require("components/trader")})
    assert(not trader:AcceptGift(friend, fruit))
    trader.test = nil
    assert(trader:AcceptGift(friend, fruit))
    assert(#records("dst.player.traded") == 1 and latest("dst.player.traded").giver.userid == friend.userid)

    player:PushEvent("oneat", {food = fruit, feeder = friend})
    player:PushEvent("builditem", {recipe = {name = "axe"}, item = entity("axe", 11)})
    local skeleton = entity("skeleton_player", 74)
    skeleton.userid = friend.userid
    local workable = setmetatable({inst = skeleton, action = ACTIONS.HAMMER, workleft = 1},
        {__index = require("components/workable")})
    workable:WorkedBy_Internal(player, 1)
    player:PushEvent("harvestsomething", {object = plant})
    player:PushEvent("fishcaught", {fish = entity("oceanfish_small_1_inv", 12)})
    player:PushEvent("startstarving")
    -- The game and LuaJIT supply the bit library used by Klei's health component.
    if bit then
        local health = setmetatable({inst = player}, {__index = require("components/health")})
        local flags = require("prefabs/wagboss_util").LunarBurnFlags
        local conditions = records("dst.player.condition_changed")
        local before = #conditions
        health:RegisterLunarBurnSource("supernova", flags.NEAR_SUPERNOVA)
        health:RegisterLunarBurnSource("supernova", bit.bor(flags.NEAR_SUPERNOVA, flags.SUPERNOVA))
        health:RegisterLunarBurnSource("supernova", flags.NEAR_SUPERNOVA)
        health:UnregisterLunarBurnSource("supernova")
        assert(#conditions == before + 2)
        assert(conditions[before + 1].condition == "lunar_burn" and conditions[before + 1].active == true)
        assert(conditions[before + 2].condition == "lunar_burn" and conditions[before + 2].active == false)
    end
    player:PushEvent("on_washed_ashore")
    player:PushEvent("unlockrecipe", {})
    player:PushEvent("unlockrecipe", {recipe = "axe"})
    player:PushEvent("murdered", {victim = frog, stackmult = 4, negligent = true})
    while next(TheWorld.pendingtasks or {}) do advance() end
    local work = latest("dst.player.finished_work")
    assert(work.player.userid == player.userid and work.action_id == "HAMMER")
    assert(work.target.guid == skeleton.GUID and work.target.userid == nil)
    assert(#records("dst.player.recipe_unlocked") == 1)
    advance()
    dofile(root .. "/tests/lua/gameplay_actions_contract.lua")(entity, player, friend, records, pack)
    dofile(root .. "/tests/lua/combat_contract.lua")(entity, advance, records)
else
    assert(not (player.event_listeners.oneat or player.event_listeners.finishedwork))
    assert(#records("dst.player.dice_rolled") == 0)
end
-- A departing avatar can have its account cleared during a seamless character swap.
local snapshot_player = entity("wilson", 14, "KU_OFFLINE")
snapshot_player.is_snapshot_user_session = true
TheWorld:PushEvent("ms_playerleft", snapshot_player)
assert(#records("dst.player.shard_left") == 0)
player.userid = nil
TheWorld:PushEvent("ms_playerleft", player)
assert(latest("dst.player.shard_left").player.userid == "KU_PLAYER")
assert(id_lookups == 0 and driver.health().errors == (profile == "play" and 1 or 0))
for _, frame in ipairs(frames) do io.write(frame, "\n") end
