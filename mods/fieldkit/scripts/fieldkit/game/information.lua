local Calendar = require("fieldkit/rules/calendar")
local Metrics = require("fieldkit/game/metrics")
local M = {}

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

function M.temperature(value)
    if not finite(value) then return "?" end
    return string.format("%.0f°", value)
end

local naughtiness = setmetatable({}, { __mode = "k" })
local function observe_naughtiness(player, data)
    if type(data) == "table" and finite(data.actions) and finite(data.threshold) and data.actions >= 0 and data.threshold > 0 then
        naughtiness[player] = { actions = data.actions, threshold = data.threshold }
    end
end

function M.install(env)
    env.AddPlayerPostInit(function(player)
        player:ListenForEvent("naughtydelta", observe_naughtiness)
    end)
end

-- These are public-rule bounds, not a reading of the server's buck task.
function M.ride_window(tuning, domestic, beard, mood, skilled)
    local low, high = tuning.BEEFALO_MIN_BUCK_TIME, tuning.BEEFALO_MAX_BUCK_TIME
    local moodmult = mood and tuning.BEEFALO_BUCK_TIME_MOOD_MULT or 1
    local beardmult = beard and 1 or tuning.BEEFALO_BUCK_TIME_NUDE_MULT
    local domesticmult = domestic and 1 or tuning.BEEFALO_BUCK_TIME_UNDOMESTICATED_MULT
    local skill = tuning.SKILLS and tuning.SKILLS.WATHGRITHR
        and tuning.SKILLS.WATHGRITHR.WATHGRITHR_BEEFALO_BUCK_TIME_MOD
    if not finite(low) or not finite(high) or not finite(moodmult)
        or not finite(beardmult) or not finite(domesticmult)
        or low < 0 or high < low or moodmult <= 0 or beardmult <= 0 or domesticmult <= 0
        or (skilled and (not finite(skill) or skill <= 0)) then
        return nil
    end
    local multiplier = moodmult * beardmult * domesticmult * (skilled and skill or 1)
    -- The domesticated tag can survive declining domestication until the mount goes feral.
    return low * multiplier, high * multiplier
end

function M.domestication(elapsed, low, high)
    if not finite(elapsed) or not finite(low) or not finite(high) or high <= low
        or elapsed < low - 1 or elapsed > high + 1 then return end
    -- Include sampling and replication uncertainty; the server can override these public rules.
    return math.max(0, (elapsed - 1 - low) / (high - low)), math.min(1, (elapsed + 1 - low) / (high - low))
end

local function world_state(world, calendar, style)
    local state = world.state
    local cave = world:HasTag("cave")
    local moonphase, waxing = state.moonphase, state.iswaxingmoon
    if cave then moonphase, waxing = state.cavemoonphase, state.iscavewaxingmoon end
    -- The native seasons component publishes this sentinel for endless seasons.
    local endless = state.remainingdaysinseason == 10000
    local seasons = Calendar.seasons(state)
    local elapsed, remaining = state.elapseddaysinseason, state.remainingdaysinseason
    -- Native seasonprogress is an environmental blend; the first autumn starts at .5.
    local progress = not endless and finite(elapsed) and finite(remaining)
        and elapsed >= 0 and remaining >= 0 and elapsed + remaining > 0
        and elapsed / (elapsed + remaining) or nil
    return {
        day = finite(state.cycles) and state.cycles + 1 or nil,
        season = state.season,
        season_day = finite(state.elapseddaysinseason) and state.elapseddaysinseason + 1 or nil,
        remaining_days = not endless and finite(state.remainingdaysinseason) and state.remainingdaysinseason or nil,
        endless = endless,
        temperature = finite(state.temperature) and state.temperature or nil,
        moonphase = moonphase,
        waxing = waxing,
        phase = cave and state.cavephase or state.phase,
        cave = cave,
        next_season = seasons.next_season,
        season_lengths = seasons.lengths,
        year_length = seasons.total,
        season_progress = progress,
        fullmoon = calendar:fullmoon(state, cave, style),
    }
end


function M.new(ctx)
    local G, world = ctx.G, ctx.G.TheWorld
    local calendar, moon_style = Calendar.new(), nil
    local observed, anchor, duration
    local ride, riding, pending
    local function animation()
        return ctx.player.AnimState and ctx.player.AnimState:GetCurrentAnimationName() or ""
    end
    local function window(mount)
        local updater = ctx.player.components.skilltreeupdater
        if not updater or not updater.HasSkillTag then return end
        return M.ride_window(G.TUNING, mount:HasTag("domesticated"), mount:HasTag("has_beard"),
            mount:HasTag("scarytoprey"), updater:HasSkillTag("beefalobucktime"))
    end
    local function finish_ride(reason)
        if not ride or not ctx.journal then return end
        local elapsed = math.max(0, (ride.buck_at or G.GetTime()) - ride.started)
        local low, high
        if ride.buck_at and not reason then low, high = M.domestication(elapsed, ride.low, ride.high) end
        ctx.journal:add("ride", { prefab = ride.mount.prefab, name = ride.name, duration = elapsed,
            low = low, high = high, state = low and "estimated" or reason or (ride.buck_at and "unknown" or "dismounted") })
        ctx.journal:flush()
    end
    local function update_ride()
        local rider = ctx.player.replica.rider
        local is_riding = rider and rider:IsRiding() or false
        if ride and not ride.buck_at and animation() == "buck" then ride.buck_at = G.GetTime() end
        if not is_riding or not ctx.config.ride_timer then
            finish_ride(not ctx.config.ride_timer and "interrupted" or nil)
            ride, pending = nil, nil
        else
            if not riding then pending = { started = G.GetTime(), known_start = riding == false } end
            local mount = rider:GetMount()
            if mount and mount:IsValid() and mount:HasTag("beefalo") then
                if not ride or ride.mount ~= mount then
                    finish_ride("interrupted")
                    local start = pending or { started = G.GetTime() }
                    ride = { mount = mount, started = start.started, name = mount.GetDisplayName and mount:GetDisplayName() }
                    pending = nil
                    if start.known_start then ride.low, ride.high = window(mount) end
                elseif ride.low then
                    local low, high = window(mount)
                    if low ~= ride.low or high ~= ride.high then
                        ride.low, ride.high = nil, nil
                    end
                end
            else
                finish_ride("interrupted")
                ride = nil
            end
        end
        riding = is_riding
    end
    local function invalidate_ride()
        if ride then ride.low, ride.high = nil, nil end
        if pending then pending.known_start = false end
    end
    local phases = { calm = true, warn = true, wild = true, dawn = true }
    local function observe(_, data)
        if not data or not phases[data.phase] or not finite(data.timeinphase)
            or data.timeinphase < 0 or data.timeinphase > 1 then
            observed, anchor, duration = nil, nil, nil
            return
        end
        local now, progress = G.GetTime(), data.timeinphase
        local changed = not observed or data.phase ~= observed.phase
            or progress < observed.progress or now < observed.at
        observed = { phase = data.phase, progress = progress, at = now }
        if changed or not anchor then
            duration, anchor = nil, observed
        elseif now - anchor.at >= 1 then
            local delta = progress - anchor.progress
            local measured = delta > 0 and (now - anchor.at) / delta or nil
            if measured and finite(measured) then
                -- A server correction invalidates the old slope; one clean sample recalibrates it.
                if not duration or math.abs(measured / duration - 1) < .02 then
                    duration = measured
                else
                    duration = nil
                end
            else
                duration = nil
            end
            anchor = observed
        end
    end

    local function reset()
        anchor, duration = nil, nil
    end
    ctx.scope:listen("nightmareclocktick", observe, world)
    ctx.scope:listen("serverpauseddirty", reset, world)
    ctx.scope:listen("naughtydelta", observe_naughtiness)
    ctx.scope:listen("moonphasestylechanged", function(_, data) moon_style = data and data.style end, world)
    ctx.scope:listen("isridingdirty", update_ride)
    ctx.scope:listen("performaction", function(_, data)
        -- Native WALKTO leaves the buck timer unchanged; remote echoes omit the action.
        if data and data.action and data.action.action == G.ACTIONS.WALKTO then return end
        local anim = animation()
        if ride and not ride.mount_ack and G.GetTime() - ride.started < 2
            and (anim == "mount" or anim == "heavy_mount") then ride.mount_ack = true; return end
        invalidate_ride()
    end)
    ctx.scope:listen("attacked", invalidate_ride)
    ctx.scope:listen("feedmount", invalidate_ride)
    if ctx.journal then
        ctx.scope:periodic(.1, update_ride)
        ctx.scope:defer(function() finish_ride("interrupted"); ride = nil end)
    end
    observe(nil, { phase = world.state.nightmarephase, timeinphase = world.state.nightmaretimeinphase })
    anchor = nil -- The initial world snapshot may precede this frame's clock tick.
    update_ride()

    local self = {}
    function self:snapshot()
        update_ride()
        local nightmare = { quality = "unknown", visible = false }
        if observed then
            nightmare.phase, nightmare.progress = observed.phase, observed.progress
            local inventory = ctx.player.replica.inventory
            local mode = ctx.config.nightmare_indicator
            nightmare.visible = mode == "always" or (mode == "medallion"
                and inventory ~= nil and inventory:Has("nightmare_timepiece", 1)) or false
            if duration and observed.progress < 1 and G.GetTime() - observed.at <= 2 then
                nightmare.remaining = math.max(0, (1 - observed.progress) * duration)
                nightmare.quality = "estimated"
            end
        end
        local ride_state
        if ride then
            local elapsed = math.max(0, G.GetTime() - ride.started)
            ride_state = { observed = elapsed, quality = "unknown" }
            if ride.high and ride.high > elapsed then
                ride_state.remaining_min = math.max(0, ride.low - elapsed)
                ride_state.remaining_max = ride.high - elapsed
                ride_state.quality = "estimated"
            end
        end
        return { own = Metrics.snapshot(ctx), world = world_state(world, calendar, moon_style),
            nightmare = nightmare, ride = ride_state, naughtiness = naughtiness[ctx.player] }
    end
    return self
end

return M
