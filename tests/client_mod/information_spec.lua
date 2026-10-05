local T = require("tests.client_mod.support")
T.case("information contracts", function()
    local Information = require("fieldkit/game/information")
    local Scope = require("fieldkit/core/scope")
    T.each("temperature display", {
        { name = "positive", value = 70, expected = "70°" }, { name = "negative", value = -8.2, expected = "-8°" },
        { name = "absent" }, { name = "NaN", value = 0 / 0 }, { name = "infinite", value = math.huge },
        { name = "negative infinite", value = -math.huge }, { name = "wrong type", value = "21" },
    }, function(case) assert(Information.temperature(case.value) == (case.expected or "?")) end)

    local tuning = {
        BEEFALO_MIN_BUCK_TIME = 50, BEEFALO_MAX_BUCK_TIME = 800,
        BEEFALO_BUCK_TIME_MOOD_MULT = .2, BEEFALO_BUCK_TIME_NUDE_MULT = .2,
        BEEFALO_BUCK_TIME_UNDOMESTICATED_MULT = .3,
        SKILLS = { WATHGRITHR = { WATHGRITHR_BEEFALO_BUCK_TIME_MOD = 1.3 } },
    }
    T.each("public riding bounds", {
        { name = "wild", false, true, false, false, 15, 240 },
        { name = "skilled domestic", true, true, false, true, 65, 1040 },
        { name = "shaved wild", false, false, false, false, 3, 48 },
        { name = "mood", false, true, true, false, 3, 48 },
        { name = "shaved mood", false, false, true, false, .6, 9.6 },
    }, function(case)
        local low, high = Information.ride_window(tuning, case[1], case[2], case[3], case[4])
        assert(math.abs(low - case[5]) < 1e-9 and math.abs(high - case[6]) < 1e-9)
    end)
    assert(Information.ride_window({}, false, false, false, false) == nil)

    local now, cave, riding, mount, medallion = 0, true, false, nil, false
    local world = { state = {
        cycles = 9, season = "winter", elapseddaysinseason = 2, remainingdaysinseason = 13,
        temperature = -8.2, moonphase = "new", cavemoonphase = "full", iscavewaxingmoon = false,
        nightmarephase = "warn", nightmaretimeinphase = .25,
    }, HasTag = function(_, tag) return cave and tag == "cave" end }
    local player = {
        components = { skilltreeupdater = { HasSkillTag = function() return false end } },
        replica = {
            health = { GetCurrent = function() error("unattached replica has no measured value") end },
            hunger = { classified = {}, GetCurrent = function() return 12.5 end, Max = function() return 150 end },
            rider = { IsRiding = function() return riding end, GetMount = function() return mount end },
            inventory = { Has = function(_, prefab, count)
                assert(prefab == "nightmare_timepiece" and count == 1)
                return medallion
            end },
        },
        GetTemperature = function() return 21.5 end,
        GetMoisture = function() return 7 end,
        GetMaxMoisture = function() return 100 end,
    }
    T.entity(player)
    T.events(world)
    local function emit(source, ...) source:PushEvent(...) end
    local ctx = { player = player, config = { ride_timer = true, nightmare_indicator = "always" },
        G = { TheWorld = world, GetTime = function() return now end, TUNING = tuning } }
    ctx.scope = Scope.new(player, error)
    local information = Information.new(ctx)
    local snapshot = information:snapshot()
    assert(snapshot.own.health.quality == "unknown" and snapshot.own.health.value == nil)
    assert(snapshot.own.hunger.value == 12.5 and snapshot.own.hunger.max == 150)
    assert(snapshot.own.temperature.quality == "unknown" and snapshot.own.moisture.value == nil)
    assert(snapshot.world.day == 10 and snapshot.world.season_day == 3 and snapshot.world.remaining_days == 13)
    assert(snapshot.world.season_progress == 2 / 15)
    world.state.elapseddaysinseason, world.state.seasonprogress = 0, .5
    assert(information:snapshot().world.season_progress == 0,
        "the calendar follows elapsed days rather than the first season's environmental midpoint")
    world.state.elapseddaysinseason = 2
    assert(snapshot.world.moonphase == "full" and snapshot.world.waxing == false)
    assert(snapshot.nightmare.phase == "warn" and snapshot.nightmare.quality == "unknown")
    assert(snapshot.nightmare.visible and snapshot.ride == nil)
    assert(snapshot.naughtiness == nil, "no compatible server data cannot invent a naughtiness value")
    emit(player, "naughtydelta", { actions = 7, threshold = 31 })
    assert(information:snapshot().naughtiness.actions == 7)
    T.each("invalid external observations", { { actions = 0 / 0, threshold = 31 }, 5,
        { actions = -1, threshold = 31 }, { actions = 8, threshold = 0 } }, function(data)
        emit(player, "naughtydelta", data)
        assert(information:snapshot().naughtiness.actions == 7, "invalid data preserves the last observation")
    end)

    player.player_classified = {}
    snapshot = information:snapshot()
    assert(snapshot.own.temperature.value == 21.5 and snapshot.own.moisture.value == 7)
    world.state.remainingdaysinseason = 10000
    snapshot = information:snapshot()
    assert(snapshot.world.endless and snapshot.world.remaining_days == nil and snapshot.world.season_progress == nil)
    world.state.remainingdaysinseason = 0 / 0
    assert(information:snapshot().world.remaining_days == nil)
    cave = false
    assert(information:snapshot().world.moonphase == "new")

    for i = 0, 30 do
        now = i / 30
        emit(world, "nightmareclocktick", { phase = "warn", timeinphase = .25 + now / 120 })
    end
    snapshot = information:snapshot()
    assert(snapshot.nightmare.quality == "estimated" and math.abs(snapshot.nightmare.remaining - 89) < .001,
        "estimate uses elapsed simulation time, not an assumed 30 or 33 Hz frame rate")
    ctx.config.nightmare_indicator = "medallion"
    assert(not information:snapshot().nightmare.visible)
    medallion = true
    assert(information:snapshot().nightmare.visible, "inventory condition works without an item event")
    ctx.config.nightmare_indicator = "off"
    assert(not information:snapshot().nightmare.visible)

    now = 2
    emit(world, "nightmareclocktick", { phase = "warn", timeinphase = .5 })
    assert(information:snapshot().nightmare.quality == "unknown", "server correction discards stale slope")
    now = 3
    emit(world, "nightmareclocktick", { phase = "warn", timeinphase = .5 + 1 / 120 })
    assert(math.abs(information:snapshot().nightmare.remaining - 59) < .001)
    emit(world, "serverpauseddirty", { pause = true })
    assert(information:snapshot().nightmare.remaining == nil)
    now = 4
    emit(world, "nightmareclocktick", { phase = "wild", timeinphase = .1 })
    assert(information:snapshot().nightmare.phase == "wild" and information:snapshot().nightmare.remaining == nil)
    now = 5
    emit(world, "nightmareclocktick", { phase = "wild", timeinphase = .2 })
    assert(math.abs(information:snapshot().nightmare.remaining - 8) < .001)
    now = 6
    emit(world, "nightmareclocktick", { phase = "wild", timeinphase = .2 })
    assert(information:snapshot().nightmare.remaining == nil, "locked phases cannot invent a countdown")
    now = 7
    emit(world, "nightmareclocktick", { phase = "wild", timeinphase = .1 })
    assert(information:snapshot().nightmare.remaining == nil, "progress rollback resets estimate")
    now = 8
    emit(world, "nightmareclocktick", { phase = "wild", timeinphase = .2 })
    now = 11
    assert(information:snapshot().nightmare.remaining == nil, "missing clock ticks expire the estimate")
    emit(world, "nightmareclocktick", { phase = "none", timeinphase = 0 })
    assert(not information:snapshot().nightmare.visible)

    local action_env = { Class = T.Class, TheWorld = world, ACTIONS = { WALKTO = {}, GIVE = {} } }
    T.native("bufferedaction", action_env)
    local push = T.eval("local EntityScript = {}\n"
        .. assert(T.source("entityscript"):match("function EntityScript:PushBufferedAction%b().-\nend"))
        .. "\nreturn EntityScript.PushBufferedAction", action_env)
    local acknowledge = T.eval(assert(T.source("prefabs/player_classified")
        :match("local function OnIsPerformActionSuccessDirty%b().-\nend"))
        .. "\nreturn OnIsPerformActionSuccessDirty")
    ctx.G.ACTIONS = action_env.ACTIONS
    local function walk() push(player, action_env.BufferedAction(player, nil, ctx.G.ACTIONS.WALKTO)) end

    riding, now = true, 20
    emit(player, "isridingdirty")
    assert(information:snapshot().ride == nil)
    walk()
    mount = { IsValid = function() return true end,
        HasTag = function(_, tag) return tag == "beefalo" or tag == "has_beard" end }
    now = 21
    snapshot = information:snapshot()
    assert(snapshot.ride.observed == 1 and snapshot.ride.remaining_min == 14 and snapshot.ride.remaining_max == 239,
        "native WALKTO preserves the known start while the mount is still syncing")
    walk()
    now = 22
    snapshot = information:snapshot()
    assert(snapshot.ride.observed == 2 and snapshot.ride.remaining_min == 13 and snapshot.ride.remaining_max == 238,
        "native WALKTO preserves the same ride countdown after resolving the mount")
    acknowledge({ _parent = player, isperformactionsuccess = { value = function() return true end } })
    assert(information:snapshot().ride.quality == "unknown", "native anonymous confirmation cannot prove which action ran")
    T.each("ride bounds invalidation", {
        { name = "feeding", event = "performaction", data = { action = action_env.BufferedAction(player, nil, ctx.G.ACTIONS.GIVE) } },
        { name = "unidentified action", event = "performaction", data = {} },
        { name = "attacked", event = "attacked" },
    }, function(case)
        for _, pending in ipairs({ false, true }) do
            riding = false
            emit(player, "isridingdirty")
            local known_mount = mount
            if pending then mount = nil end
            riding = true
            emit(player, "isridingdirty")
            emit(player, case.event, case.data)
            mount = known_mount
            walk()
            assert(information:snapshot().ride.quality == "unknown", "walking cannot restore invalidated riding evidence")
        end
    end)
    riding = false
    emit(player, "isridingdirty")
    assert(information:snapshot().ride == nil)
    ctx.scope:close()
    assert(T.listener_count(player) == 0 and T.listener_count(world) == 0)
    ctx.scope = Scope.new(player, error)
    riding = true
    information = Information.new(ctx)
    assert(information:snapshot().ride.quality == "unknown", "joining mid-ride cannot recover its start time")
    ctx.scope:close()

    -- Execute the actual native Show/Hide methods; mouse hover hides these same Text widgets directly.
    local Widget = require("tests.client_mod.support.widgets")
    Widget.install()
    local StatusView = require("fieldkit/ui/status")
    local environment = { require = function() return {} end, Class = T.Class }
    local Status = T.native("widgets/statusdisplays.lua", environment)
    local function number(shown)
        local value = Widget("number")
        value.shown = shown
        return value
    end
    local status = setmetatable({}, { __index = Status })
    for _, name in ipairs({ "brain", "stomach", "heart", "moisturemeter", "boatmeter", "pethealthbadge", "wereness" }) do
        status[name] = { num = number(false) }
    end
    environment.RATE_SCALE = { INCREASE_HIGH = 1, INCREASE_MED = 2, INCREASE_LOW = 3,
        DECREASE_HIGH = 4, DECREASE_MED = 5, DECREASE_LOW = 6 }
    environment.math, environment.tostring = math, tostring
    local Moisture = T.native("widgets/moisturemeter.lua", environment)
    setmetatable(status.moisturemeter, { __index = Moisture })
    status.moisturemeter.Activate, status.moisturemeter.Deactivate = function() end, function() end
    status.moisturemeter.anim = { GetAnimState = function() return { SetPercent = function() end } end }
    status.moisturemeter.arrowdir = "neutral"
    status.boatmeter.Enable = function(self, boat) self.boat = boat end
    status.boatmeter.Disable = function(self) self.boat = nil end
    status.heart.wxshieldanimnum = number(false)
    status.heart.wxshieldanimflicker = { shown = false }
    ctx.hud = { controls = { status = status } }
    ctx.config.status_numbers = true
    ctx.scope = Scope.new(player, error)
    local original_hide = status.heart.num.Hide
    StatusView.install_numbers(ctx)
    assert(status.heart.num.shown and status.brain.num.shown)
    status:HideStatusNumbers()
    status.heart.num:Hide()
    assert(status.heart.num.shown and status.brain.num.shown, "controller close and mouse focus loss keep own numbers")
    assert(not status.heart.wxshieldanimnum.shown, "shield visibility remains owned by the native badge")
    status.moisturemeter:SetValue(1, 100, 0)
    assert(status.moisturemeter.num.shown and status.moisturemeter.num.text == "1")
    status.moisturemeter:SetValue(0, 100, 0)
    assert(not status.moisturemeter.num.shown, "inactive moisture gauge cannot show its stale positive value")
    status.boatmeter:Enable({})
    assert(status.boatmeter.num.shown)
    status.boatmeter:Disable()
    assert(not status.boatmeter.num.shown, "leaving the boat cannot leave its old number floating in the HUD")
    status.brain.num:Show()
    ctx.scope:close()
    assert(status.heart.num.Hide == original_hide and not status.heart.num.shown and status.brain.num.shown,
        "teardown restores the latest native visibility intent, not the initial value")

    -- Detailed numbers retain the native badge's value, including Wanda's inverse age display.
    local Wanda = T.native("widgets/wandaagebadge.lua", environment)
    environment.TUNING = { WANDA_MIN_YEARS_OLD = 20, WANDA_MAX_YEARS_OLD = 80 }
    environment.Lerp = function(a, b, t) return a + (b - a) * t end
    status.heart = { num = number(false), SetPercent = Wanda.SetPercent, year_hand = { SetRotation = function() end } }
    function status.heart.num:GetString() return self.text end
    environment.type = type
    environment.ValidateLineNumber = function() end
    environment.Vector3 = T.Vector3
    local NativeWidget = T.native("widgets/widget.lua", environment)
    status.heart.num.GetScale, status.heart.num.GetLooseScale, status.heart.num.SetScale = NativeWidget.GetScale,
        NativeWidget.GetLooseScale, NativeWidget.SetScale
    status.heart.num.parent = { GetScale = function() return { x = 2.5, y = 2.5, z = 2.5 } end }
    local text_env = { Class = T.Class, require = function() return {} end,
        LOC = { GetTextScale = function() return .85 end } }
    local NativeText = T.native("widgets/text.lua", text_env)
    status.heart.num.SetSize = NativeText.SetSize
    status.heart.num.inst = { TextWidget = { SetSize = function() end }, IsValid = function() return true end }
    local scale = { x = 1, y = .9, z = 1 }
    status.heart.num.inst.UITransform = {
        GetScale = function() return scale.x, scale.y, scale.z end,
        SetScale = function(_, x, y, z) scale = { x = x, y = y, z = z } end,
    }
    status.heart.num:SetSize(33)
    player.prefab, ctx.config.status_numbers = "wanda", "detailed"
    ctx.G.TUNING.WANDA_MAX_YEARS_OLD = 80
    ctx.scope = Scope.new(player, error)
    StatusView.install_numbers(ctx)
    assert(scale.x == .72 and scale.y == .9 * .72, "detail scale uses local dimensions, without multiplying HUD ancestors")
    status.heart:SetPercent(.5, 150)
    assert(status.heart.num.text == "50/80", "Wanda age must never be labelled with a 150 health maximum")
    status.heart:SetPercent(.25, 150)
    assert(status.heart.num.text == "65/80", "repeated refresh does not append duplicate maximums")
    status.moisturemeter:SetValue(4, 100, 0)
    assert(status.moisturemeter.num.text == "4/100")
    status.heart.num:SetScale(.8, .8)
    assert(scale.x == .8 * .72 and scale.y == .8 * .72,
        "native shield layout updates remain multiplied by the current detail scale")
    ctx.scope:close()
    assert(status.heart.num.text == "65" and status.heart.num.size == 33 * .85
        and scale.x == .8 and scale.y == .8,
        "cleanup restores the latest native local scale without applying locale or ancestor multipliers twice")

    local installed
    Information.install({ AddPlayerPostInit = function(fn) installed = fn end })
    local joining = { ListenForEvent = function(_, event, fn) assert(event == "naughtydelta"); installed = fn end }
    installed(joining)
    installed(joining, { actions = 0, threshold = 30 })

    ctx.scope = Scope.new(player, error)
    player.components.pethealthbar = { GetMaxHealth = function() return 600 end,
        GetPercent = function() return 1/3 end, GetMaxBonus = function() end }
    status.pethealthbadge.num.text = "200"
    status.pethealthbadge.num.GetString = function(self) return self.text end
    status.AddPetHunger = function(self)
        self.pethungerbadge = { num = number(false), SetPercent = function(meter, value, maximum)
            meter.num:SetString(tostring(math.ceil(value * maximum)))
        end }
    end
    StatusView.install_numbers(ctx)
    assert(status.pethealthbadge.num.text == "200/600", "joining with an existing pet reads its public replicated maximum")
    status:AddPetHunger()
    player.pet_hunger_classified = { Max = function() return 50 end, GetPercent = function() return .4 end }
    status.pethungerbadge:SetPercent(.4, 50)
    assert(status.pethungerbadge.num.text == "20/50" and status.pethungerbadge.num.shown,
        "a badge created after attach receives the same number policy")
    ctx.scope:close()
    assert(status.pethungerbadge.num.text == "20")

    ctx.scope = Scope.new(player, error)
    local Might = T.native("widgets/mightybadge.lua", environment)
    local overbuff = 5
    player.HasTag = function(_, tag) return tag == "wolfgang_overbuff_" .. overbuff end
    player.GetMightiness = function() return 1.5 end
    player.player_classified.currentmightiness = {}
    environment.ThePlayer = player
    environment.unpack = unpack
    ctx.G.TUNING.MIGHTINESS_MAX = 100
    ctx.G.TUNING.SKILLS.WOLFGANG_OVERBUFF_5 = 50
    local anim = { SetMultColour = function() end, Hide = function() end, Show = function() end, SetPercent = function() end }
    local ui_anim = { GetAnimState = function() return anim end }
    status.mightybadge = { num = number(false), anim = ui_anim, circleframe = ui_anim, SetPercent = Might.SetPercent }
    StatusView.install_numbers(ctx)
    status.mightybadge:SetPercent(1.5)
    assert(status.mightybadge.num.text == "150/150", "gym overbuff uses the skill's public upper bound")
    overbuff = 0
    status.mightybadge:SetPercent(.9)
    assert(status.mightybadge.num.text == "90/100", "maximum follows current skill tags rather than its bind-time value")
    ctx.scope:close()

    local maxlabel
    package.loaded["widgets/text"] = function()
        local label = number(false)
        label.SetPosition, label.SetClickable, label.Kill = function() end, function() end, function() end
        return label
    end
    status.heart.AddChild = function(_, child) maxlabel = child; return child end
    function ctx:text(_, en) return en end
    ctx.scope, ctx.config.status_numbers = Scope.new(player, error), true
    StatusView.install_numbers(ctx)
    assert(not maxlabel.shown and maxlabel.text == "Max 80")
    status:ShowStatusNumbers()
    assert(maxlabel.shown, "native controller status view also exposes maximum values")
    status:HideStatusNumbers()
    assert(not maxlabel.shown and status.heart.num.shown, "closing controller details preserves configured current numbers")
    ctx.scope:close()
end)
T.done()
