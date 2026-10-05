local T = require("tests.client_mod.support")
T.case("emote lifecycle", function()
    local runs, access, mounted, ready = {}, true, false, true
    package.loaded.usercommands = {
        CanUserAccessCommand = function() return access end,
        CanUserStartCommand = function() return true end,
        RunUserCommand = function(name) runs[#runs + 1] = name end,
    }
    local player = { prefab = "mod_character", replica = { rider = { IsRiding = function() return mounted end } } }
    function player:IsValid() return not self.removed end
    function player:HasTag(tag) return tag == "playerghost" and self.ghost or false end
    local ctx = { player = player, config = { gestures = true, text_scale = 1 }, scope = {},
        executor = { cancel = function() end }, G = {
            GetCommonEmotes = function() return {
                wave = { data = { mounted = true, randomanim = true, anim = { "wave1", "wave2" } } },
                dance = { data = { loop = true, randomanim = true, anim = { { "dance_pre", "dance_loop" }, "dance2" } } },
            } end,
            DST_CHARACTERLIST = { "wilson" },
            EMOTE_ITEMS = { pet = { cmd_name = "pet", data = { mounted = true, mountonly = true } },
                duplicate = { cmd_name = "wave", data = {} } },
            STRINGS = { UI = { EMOTES = { WAVE = "Wave" } } },
            kleifileexists = function() return false end,
        },
    }
    function ctx:ready() return ready, "Not ready" end
    function ctx:text(_, en) return en end
    function ctx:report(err) error(err) end
    function ctx:notify(reason) self.message = reason end
    local service = require("fieldkit/features/emotes").new(ctx)
    local entries = service:entries()
    assert(#entries == 2 and entries[2].label == "Wave" and entries[2].atlas == "images/emotes_generic.xml")
    assert(entries[2].texture == "gesture_generic_wave.tex")
    assert(entries[1].animation == "dance_loop" and entries[2].animation == "wave1")
    assert(service:appearance() == nil, "unknown characters keep their native icons without borrowing a character model")
    player.prefab, player.userid = "wilson", "KU_TEST"
    local client_skins = { base_skin = "wilson_formal", body_skin = "body_test" }
    ctx.G.TheNet = { GetClientTableForUser = function() return client_skins end }
    local skin_build = "wilson_formal"
    player.AnimState = { GetBuild = function() return skin_build end }
    player.GetSkinName = function() return player.prefab .. "_none" end
    local skins_env = setmetatable({ require = function() return {} end,
        PREFAB_SKINS = { wilson = { "wilson_formal" } },
        Prefabs = { wilson_formal = { skins = { normal_skin = "wilson_formal" } } } }, { __index = _G })
    T.native("skinsutils", skins_env)
    ctx.G.GetSkinModeFromBuild, ctx.G.GetSkinModes = skins_env.GetSkinModeFromBuild, skins_env.GetSkinModes
    local appearance = service:appearance()
    assert(appearance.prefab == "wilson" and appearance.base == "wilson_formal" and appearance.clothes.body == "body_test")
    assert(appearance.mode.type == "normal_skin" and appearance.build == skin_build)
    ctx.G.DST_CHARACTERLIST = { "wilson", "wolfgang", "woodie", "wormwood", "wanda", "wurt" }
    for _, form in ipairs({ { "wolfgang", "mighty_skin" }, { "wolfgang", "wimpy_skin" }, { "woodie", "werebeaver_skin" },
        { "wormwood", "stage_4" }, { "wanda", "old_skin" }, { "wurt", "powerup" } }) do
        player.prefab, skin_build, client_skins = form[1], form[1] .. "_" .. form[2], nil
        skins_env.PREFAB_SKINS[player.prefab] = { player.prefab .. "_none" }
        skins_env.Prefabs[player.prefab .. "_none"] = { skins = { [form[2]] = skin_build } }
        local current = service:appearance()
        assert(current.mode.type == form[2] and current.base == player:GetSkinName(),
            "native build resolves body shape without character-specific preview rules or a ready client table")
    end
    player.prefab, skin_build, client_skins = "wilson", "wilson_formal", { base_skin = "wilson_formal", body_skin = "body_test" }
    assert(not service:execute("pet"))
    mounted = true
    assert(#service:entries() == 2 and service:execute("pet") and runs[1] == "pet")
    assert(not service:execute("dance"), "mount restrictions are checked again at execution")
    access = false
    assert(#service:entries() == 0 and not service:execute("wave"))
    access, ready = true, false
    assert(not service:execute("wave"))
    ready, ctx.config.gestures = true, false
    assert(not service:execute("wave") and #runs == 1)
    ctx.config.gestures = true

    mounted = false
    assert(service:set_favourite(3, "wave") and service:set_favourite(7, "pet"))
    local favourites, enabled = service:wheel_entries()
    assert(enabled and #favourites == 10 and favourites[1].disabled and favourites[3].name == "wave"
        and favourites[7].disabled and favourites[7].name == "pet" and favourites[9].name == "dance")
    mounted = true
    favourites = service:wheel_entries()
    assert(favourites[3].name == "wave" and not favourites[7].disabled,
        "availability changes never move favourite sectors")
    assert(service:set_favourite(3, nil) and not service:set_favourite(9, "wave") and not service:set_favourite(2, "unknown"))
    service:toggle_favourites()
    favourites, enabled = service:wheel_entries()
    assert(not enabled and #favourites == 2)

    local pending, saved
    ctx.scope = require("fieldkit/core/scope").new(T.entity(), error)
    ctx.store = { ready = false, get = function() return { slots = { "wave", false, "pet", false, false, false, false, "removed_emote" } } end,
        set = function(_, key, data) assert(key == "emotes"); saved = data; return true end,
        on_ready = function(_, fn) pending = fn; return function() pending = nil end end }
    local personalised = require("fieldkit/features/emotes").new(ctx)
    personalised:set_favourite(1, "dance")
    personalised:set_favourite(3, nil)
    ctx.store.ready = true; pending()
    assert(saved.slots[1] == "dance" and saved.slots[3] == false and saved.slots[8] == "removed_emote")
    favourites = personalised:wheel_entries()
    assert(favourites[1].disabled and favourites[8].disabled and #favourites == 10,
        "late loading preserves edits and retains unavailable favourites in their fixed slots")
    ctx.scope:close(); ctx.scope, ctx.store = {}, nil

    -- Use real native input, screen lifecycle and FrontEnd dispatch, with only rendering stubbed.
    Class = T.Class
    local tasks = {}
    local RenderWidget = require("tests.client_mod.support.widgets")
    local Widget = Class(RenderWidget, function(self, name)
        RenderWidget._ctor(self, name)
        self.inst.DoTaskInTime = function(_, _, fn) tasks[#tasks + 1] = fn end
    end)
    function Widget:GetWorldPosition() return { x = 640 + self.position.x, y = 360 + self.position.y } end
    package.loaded["widgets/widget"], package.loaded["widgets/image"], package.loaded["widgets/text"] = Widget, Widget, Widget
    local puppet_count = 0
    package.loaded["widgets/skinspuppet"] = function()
        puppet_count = puppet_count + 1
        local puppet = Widget("puppet")
        puppet.animstate = { SetPercent = function(_, animation, percent)
            puppet.animation, puppet.percent = animation, percent
            puppet.poses = (puppet.poses or 0) + 1
        end }
        function puppet:SetSkins(prefab, base, clothes, idle, mode, curse)
            assert(idle and mode)
            self.prefab, self.base, self.clothes, self.mode, self.curse = prefab, base, clothes, mode, curse
        end
        return puppet
    end
    ANCHOR_MIDDLE, SCALEMODE_PROPORTIONAL = 1, 1
    MOVE_LEFT, MOVE_RIGHT, MOVE_UP, MOVE_DOWN = 1, 2, 3, 4
    local controls = { "CONTROL_PRIMARY", "CONTROL_SECONDARY", "CONTROL_ACCEPT", "CONTROL_CANCEL", "CONTROL_CONTROLLER_ACTION",
        "CONTROL_CONTROLLER_ALTACTION", "CONTROL_SCROLLBACK", "CONTROL_SCROLLFWD", "CONTROL_FOCUS_LEFT", "CONTROL_FOCUS_UP",
        "CONTROL_FOCUS_RIGHT", "CONTROL_FOCUS_DOWN", "CONTROL_MOVE_LEFT", "CONTROL_MOVE_RIGHT", "CONTROL_MOVE_UP", "CONTROL_MOVE_DOWN" }
    for i, name in ipairs(controls) do _G[name] = i end
    KEY_ESCAPE, KEY_PAGEUP, KEY_PAGEDOWN = 27, 280, 281
    MOUSEBUTTON_SCROLLUP, MOUSEBUTTON_SCROLLDOWN = 1003, 1004
    local native = T.native
    local env = setmetatable({ require = function(name) return package.loaded[name] or Widget end,
        RESOLUTION_X = 1280, RESOLUTION_Y = 720, ValidateLineNumber = function() end,
        TheInputProxy = { FlushInput = function() end, GetInputDeviceCount = function() return 0 end },
        IsNotConsole = function() return false end, IsConsole = function() return false end,
        Print = function() end, VERBOSITY = { DEBUG = 0 },
        TheSim = { SetUIRoot = function() end, FeedbackEnabled = function() return false end },
    }, { __index = _G })
    package.loaded["widgets/screen"] = native("widgets/screen", env)
    native("events", env)
    native("input", env)
    native("frontend", env)
    local front = setmetatable({ screenstack = {}, screenroot = Widget("screenroot"), consoletext = Widget("console"),
        serverpausewidget = Widget("pause"), IsControlsDisabled = function() return false end,
        GetSound = function() return { PlaySound = function() end } end,
        Update = function(self) local active = self:GetActiveScreen(); if active then active:OnUpdate(0) end end,
    }, { __index = env.FrontEnd })
    local pointer, controller, analog = { x = 640, y = 360 }, false, {}
    local input = { onkey = env.EventProcessor(), onkeydown = env.EventProcessor(), onkeyup = env.EventProcessor(),
        oncontrol = env.EventProcessor(), onmousebutton = env.EventProcessor(), mouse_enabled = true,
        ControllerAttached = function() return controller end,
        GetScreenPosition = function() return { x = pointer.x, y = pointer.y } end,
        GetAnalogControlValue = function(_, control) return analog[control] or 0 end,
        UpdateEntitiesUnderMouse = function() end,
    }
    env.TheInput, env.TheFrontEnd = input, front
    local Screen = package.loaded["widgets/screen"]
    ctx.hud = Screen("hud")
    player.HUD = ctx.hud
    front.screenstack[1] = ctx.hud
    ctx.G.ThePlayer, ctx.G.TheWorld, ctx.G.TheFrontEnd, ctx.G.TheInput = player, {}, front, input
    ctx.G.TheSim = { GetScreenSize = function() return 1280, 720 end }
    ctx.G.TUNING = { CONTROLLER_DEADZONE_RADIUS = .2 }
    ctx.G.IsPaused = function() return false end
    ctx.G.TheNet = { IsServerPaused = function() return false end }
    local all = {}
    for i = 1, 10 do all[i] = { name = "emote" .. i, label = "Emote " .. i, atlas = "native.xml", texture = "native.tex", animation = "pose" .. i } end
    local submitted = {}
    ctx.emotes = {
        wheel_entries = function() return all end,
        appearance = function() return appearance end,
        execute = function(_, name)
            assert(front:GetActiveScreen() == ctx.hud and ctx.screen == nil, "close modal before running native command")
            if not ready then return false, "Not ready" end
            submitted[#submitted + 1] = name; return true
        end,
    }
    local Wheel = require("fieldkit/ui/emote_wheel")
    T.each("wheel sectors", {
        { name = "deadzone", 0, 51 }, { name = "boundary", 0, 52, 1 },
        { name = "NE", 100, 100, 2 }, { name = "E", 100, 0, 3 }, { name = "SE", 100, -100, 4 },
        { name = "S", 0, -100, 5 }, { name = "SW", -100, -100, 6 },
        { name = "W", -100, 0, 7 }, { name = "NW", -100, 100, 8 },
    }, function(case) assert(Wheel.sector(case[1], case[2], 52) == case[3]) end)
    local function open(key)
        local wheel = Wheel(ctx, key)
        ctx.screen = wheel
        front:PushScreen(wheel)
        return wheel
    end
    local function key(key, down) env.Input.OnRawKey(input, key, down) end
    local function control(control, down) env.Input.OnControl(input, control, down, down and 1 or 0) end
    input.onkey:AddEventHandler("onkey", function(key, down) front:OnRawKey(key, down) end)
    input.onkeydown:AddEventHandler(289, function() if not ctx.screen then open(289) end end)

    key(289, true)
    assert(ctx.screen and not ctx.screen.selected)
    pointer.y = 540
    ctx.screen:OnUpdate()
    assert(ctx.screen.selected == 1)
    key(289, false)
    assert(#submitted == 1 and submitted[1] == "emote1" and front:GetActiveScreen() == ctx.hud)
    key(289, false)
    assert(#submitted == 1, "a repeated key release cannot reopen or submit twice")

    pointer = { x = 640, y = 360 }
    local wheel = open(289)
    wheel:Choose(1)
    local puppet, created = wheel.puppet, puppet_count
    assert(puppet.animation == "pose1" and puppet.percent == .5 and not puppet.enable_idle_emotes)
    assert(puppet.prefab == "wilson" and puppet.base == "wilson_formal" and puppet.clothes.body == "body_test"
        and puppet.mode == appearance.mode)
    wheel:Choose(1)
    assert(puppet.poses == 1, "polling the selected sector must not restart its pose")
    local initial_appearance = appearance
    appearance = { prefab = "woodie", base = "woodie_none", clothes = {}, build = "werebeaver",
        mode = { type = "werebeaver_skin", anim_bank = "werebeaver", scale = .82 } }
    skin_build = appearance.build
    wheel:OnUpdate()
    assert(wheel.puppet == puppet and puppet_count == created and puppet.mode == appearance.mode and puppet.animation == "idle_loop",
        "a transformed build refreshes the same puppet and preserves that bank's native resting pose")
    appearance, skin_build = initial_appearance, initial_appearance.build
    wheel:OnUpdate()
    assert(puppet.mode == appearance.mode and puppet.animation == "pose1", "returning to a human bank restores the selected pose")
    wheel:ChangePage(1)
    assert(not puppet.shown)
    wheel:Choose(1)
    assert(puppet_count == created and wheel.puppet == puppet and puppet.animation == "pose9" and puppet.shown,
        "all sectors and pages share one native puppet")
    wheel:ChangePage(-1)
    wheel:Choose(1)
    pointer.x = 641
    key(289, false)
    assert(#submitted == 1, "moving back into the center cancels")
    wheel = open(289)
    key(KEY_PAGEDOWN, true)
    assert(wheel.page == 2 and not wheel.selected)
    control(CONTROL_FOCUS_RIGHT, true)
    assert(wheel.selected == nil, "raw directional controls do not duplicate FrontEnd's focus polling")
    front:OnFocusMove(MOVE_RIGHT, true)
    control(CONTROL_ACCEPT, true)
    control(CONTROL_ACCEPT, false)
    assert(submitted[2] == "emote9")
    wheel:OnControl(CONTROL_CONTROLLER_ACTION, false)
    assert(#submitted == 2, "accept aliases are idempotent")

    -- Native input emits both mapped and raw mouse events for each physical wheel step.
    T.each("wheel aliases", T.pointer_orders, function(order)
        wheel = open(nil)
        local function scroll(button, mapped)
            local function raw(down) env.Input.OnMouseButton(input, button, down, pointer.x, pointer.y) end
            local function mapped_control(down) control(mapped, down) end
            T.pointer(raw, mapped_control, true, order.down)
            T.pointer(raw, mapped_control, false, order.up)
        end
        scroll(MOUSEBUTTON_SCROLLDOWN, CONTROL_SCROLLFWD)
        assert(wheel.page == 2, "one wheel step advances once regardless of raw/mapped dispatch order")
        scroll(MOUSEBUTTON_SCROLLUP, CONTROL_SCROLLBACK)
        assert(wheel.page == 1, "reverse wheel step returns once instead of wrapping the two-page catalog")
        wheel:Close()
        assert(#submitted == 2)
    end)

    wheel = open(nil)
    control(CONTROL_ACCEPT, false)
    assert(ctx.screen == wheel, "the menu-opening button release cannot execute")
    pointer.y = 530
    control(CONTROL_PRIMARY, true)
    control(CONTROL_PRIMARY, false)
    assert(submitted[3] == "emote1", "native FrontEnd maps mouse primary to accept")

    controller, pointer = true, { x = 640, y = 360 }
    wheel = open(nil)
    analog[CONTROL_MOVE_RIGHT] = 1
    wheel:OnUpdate()
    assert(wheel.selected == 3)
    front:OnFocusMove(MOVE_RIGHT, true)
    assert(wheel.selected == 3, "the analog stick's focus alias does not rotate the radial selection")
    analog[CONTROL_MOVE_RIGHT] = 0
    wheel:OnUpdate()
    assert(submitted[4] == "emote3", "returning the stick to neutral commits once")
    wheel:OnUpdate()
    assert(#submitted == 4)

    controller = false
    wheel = open(289)
    wheel:Choose(2)
    local other = Screen("other")
    front:PushScreen(other)
    assert(wheel.cancelled and not wheel.closed)
    for _, fn in ipairs(tasks) do fn() end
    tasks = {}
    assert(front:GetActiveScreen() == other and #front.screenstack == 2 and #submitted == 4,
        "focus loss removes only the wheel after native stack mutation")
    front:PopScreen(other)
    wheel = open(289)
    front:PopScreen(wheel)
    for _, fn in ipairs(tasks) do fn() end
    tasks = {}
    assert(#front.screenstack == 1 and front:GetActiveScreen() == ctx.hud, "external PopScreen cannot reenter and remove the HUD")
    wheel = open(289)
    wheel:Choose(1)
    player.ghost = true
    wheel:OnUpdate()
    assert(wheel.closed and #submitted == 4)
    player.ghost = false
    wheel = open(289)
    wheel:Choose(1)
    ready = false
    key(289, false)
    assert(#submitted == 4 and ctx.message == "Not ready", "late availability changes are checked after close")
    ready = true
    local catalog = all
    all = {}
    for i = 1, 8 do all[i] = { label = "Empty", disabled = true } end
    all[6] = catalog[2]
    wheel = open(nil)
    assert(#wheel.slots == 8)
    wheel:Choose(1); wheel:Close(true)
    assert(#submitted == 4, "an empty favourite cannot execute an emote")
    wheel = open(nil)
    wheel:Choose(6); wheel:Close(true)
    assert(submitted[5] == "emote2", "fixed slots stay addressable across empty sectors")
end)
T.case("native emote eligibility", function()
    local player = T.entity({ prefab = "woodie", userid = "KU_TEST" })
    local server = T.entity({ prefab = player.prefab, userid = player.userid })
    local state, sent, cancelled, heavy, mounted = nil, 0, 0, false, false
    local env = { FRAMES = 1 / 30, EMOTE_ITEMS = {}, STRINGS = { UI = { EMOTES = {} } },
        COMMAND_PERMISSION = { USER = 0, MODERATOR = 1, ADMIN = 2 },
        COMMAND_RESULT = { INVALID = 0, DISABLED = 1, ALLOW = 2 },
        require = function() return {} end, smallhash = function(value) return value end,
        print = function() end, dumptablequiet = function() end,
        kleifileexists = function() return false end,
        TheNet = { GetClientTableForUser = function() return {} end },
    }
    local commands = T.native("usercommands", env)
    env.UserToClientID = function() end
    env.UserToPlayer = function() return server end
    T.native("emotes", env)
    local on_emote = T.eval("return " .. assert(T.source("stategraphs/SGwilson")
        :match('EventHandler%("emote",%s*(function.-\n        end)%)')), env)
    server.components.inventory = { IsHeavyLifting = function() return heavy end }
    server.components.rider = { IsRiding = function() return mounted end }
    local NativeInventory = T.native("components/inventory_replica", { Class = T.Class })
    player.replica.inventory = { inst = player, IsHeavyLifting = NativeInventory.IsHeavyLifting,
        classified = { heavylifting = { value = function() return heavy end } } }
    player.replica.rider = server.components.rider
    local stategraph = T.source("stategraph")
    local has_state = T.eval("local StateGraphInstance = {}\n"
        .. assert(stategraph:match("function StateGraphInstance:HasAnyStateTag%b().-\nend"))
        .. "\nreturn StateGraphInstance.HasAnyStateTag")
    local replicated = T.eval("return " .. assert(stategraph:match("local SGTagsToEntTags%s*=%s*(%b{})")))
    server.sg = { HasAnyStateTag = has_state, IsListeningForEvent = function() return false end,
        GoToState = function(_, next_state) state = next_state end }
    server:ListenForEvent("emote", on_emote)
    function env.TheNet:SendSlashCmdToServer(name)
        sent = sent + 1
        commands.RunUserCommand(name, {}, player, true)
        env.HandleUserCmdQueue()
    end
    package.loaded.usercommands, package.loaded["fieldkit/features/emotes"] = commands, nil
    local ctx = { player = player, G = env, config = { gestures = true },
        ready = function() return true end, text = function(_, _, en) return en end,
        executor = { cancel = function() cancelled = cancelled + 1 end } }
    local service = require("fieldkit/features/emotes").new(ctx)
    T.each("player state", {
        { name = "human" }, { name = "remote idle", remote = true },
        { name = "busy", state = "busy" }, { name = "nopredict", state = "nopredict" },
        { name = "sleeping", state = "sleeping" }, { name = "floating", state = "floating" },
        { name = "remote busy", state = "busy", remote = true },
        { name = "remote nopredict", state = "nopredict", remote = true },
        { name = "remote sleeping", state = "sleeping", remote = true },
        { name = "beaver", tag = "beaver" },
        { name = "moose", tag = "weremoose" }, { name = "goose", tag = "weregoose" },
        { name = "carrying", heavy = true }, { name = "mounted", mounted = true },
    }, function(row)
        player.tags = row.tag and { [row.tag] = true } or {}
        if row.remote and row.state then
            assert(replicated[row.state], "remote guards may only use replicated native tags")
            player.tags[row.state] = true
        end
        server.tags, server.sg.tags = player.tags, row.state and { [row.state] = true } or {}
        player.sg = not row.remote and server.sg or nil
        heavy, mounted = row.heavy or false, row.mounted or false
        T.each("command", { "wave", "dance", "pet" }, function(name)
            state = nil
            commands.RunUserCommand(name, {}, player, true)
            env.HandleUserCmdQueue()
            local allowed = state == "emote"
            local listed = false
            for _, entry in ipairs(service:entries()) do listed = listed or entry.name == name end
            assert(listed == allowed, "wheel choices must match the native stategraph")
            state, sent, cancelled = nil, 0, 0
            assert(service:execute(name) == allowed, "execution must use the same eligibility as the wheel")
            assert((state == "emote") == allowed and sent == (allowed and 1 or 0)
                and cancelled == sent, "rejected emotes must preserve the current task and send no command")
        end)
    end)
    player.tags, heavy = {}, false
    assert(#service:entries() > 0)
    heavy, state, sent, cancelled = true, nil, 0, 0
    assert(not service:execute("dance") and not state and sent == 0 and cancelled == 0,
        "lifting after opening the wheel is checked before cancellation")
end)
T.done()
