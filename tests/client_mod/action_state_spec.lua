local T = require("tests.client_mod.support")
local State = require("fieldkit/game/action_state")

T.each("native work continuation", { "host", "predict", "remote" }, function(mode)
    T.each("action", { "CHOP", "MINE", "REMOVELUNARBUILDUP", "HAMMER", "DIG" }, function(id)
        T.each("form", { "normal", "woodcutter", "beaver", "spin" }, function(form)
            local player = T.entity({ tags = { working = true, [form] = true } })
            player.GetModuleTypeCount = function() return form == "spin" and 1 or 0 end
            player.components.playercontroller = { IsBusy = function() return false end, ismastersim = mode == "host" }
            local definition, tags = { id = id }, { working = true }
            local action = { action = definition, target = T.entity(), invobject = T.entity() }
            local ticket = { sent = true, action = action, succeeded = mode == "host", attempt_hint = true,
                performed = function() return true end }
            if mode ~= "remote" then
                local source = T.source("stategraphs/SGwilson" .. (mode == "predict" and "_client" or ""))
                local start = assert(source:find("ActionHandler(ACTIONS." .. id .. ",", 1, true))
                local handler = T.eval("return ActionHandler" .. assert(source:sub(start):match("^ActionHandler(%b())")), {
                    ACTIONS = { [id] = definition }, ActionHandler = function(_, fn) return { deststate = fn } end,
                })
                player.sg = { HasStateTag = function(_, tag) return tags[tag] end,
                    sg = { actionhandlers = { [definition] = handler } } }
            end
            local ctx = { player = player }
            assert(not State.idle(ctx) and State.repeat_ready(ctx, action, ticket))
            local guard = form == "beaver" and "gnawing" or form == "spin"
                and (id == "CHOP" or id == "MINE" or id == "REMOVELUNARBUILDUP") and "prespin"
                or ({ CHOP = "prechop", MINE = "premine", REMOVELUNARBUILDUP = "premine",
                    HAMMER = "prehammer", DIG = "predig" })[id]
            player.tags[guard] = true
            assert(not State.repeat_ready(ctx, action, ticket), "server guard still owns the animation")
            player.tags[guard] = nil
            if player.sg then
                tags[guard] = true
                assert(not State.repeat_ready(ctx, action, ticket), "local guard still owns the animation")
                tags[guard] = nil
            end
            ticket.succeeded, ticket.attempt_hint = false, false
            assert(not State.repeat_ready(ctx, action, ticket), "prediction alone cannot authorize another request")
            ticket.attempt_hint, ticket.failed = true, true
            assert(not State.repeat_ready(ctx, action, ticket))
            ticket.failed, ticket.closed = false, true
            assert(not State.repeat_ready(ctx, action, ticket))
            ticket.closed, player.bufferedaction = false, {}
            assert(not State.repeat_ready(ctx, action, ticket), "another action retains ownership")
            player.bufferedaction = nil
            assert(not State.repeat_ready(ctx, { action = definition, target = T.entity() }, ticket))
        end)
    end)
end)

T.case("unknown work and ordinary idle", function()
    local player = T.entity({ tags = { idle = true } })
    player.components.playercontroller = { IsBusy = function() return false end }
    local ctx = { player = player }
    assert(State.idle(ctx))
    player.tags.doing = true
    assert(not State.idle(ctx))
    player.tags.doing, player.tags.working = nil, true
    assert(not State.repeat_ready(ctx, { action = { id = "MODDED_WORK" } }))
end)

T.done()
