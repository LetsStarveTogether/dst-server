local M = { INTERVAL = .1 }
local guards = { CHOP = "prechop", MINE = "premine", REMOVELUNARBUILDUP = "premine",
    HAMMER = "prehammer", DIG = "predig" }

local function tagged(player, tag)
    return player:HasTag(tag) or player.sg and player.sg:HasStateTag(tag) or false
end

function M.idle(ctx)
    local player = ctx.player
    if player.components.playercontroller:IsBusy() or tagged(player, "working") or tagged(player, "doing") then return false end
    if player.sg then return player.sg:HasStateTag("idle") or player.sg:HasStateTag("moving") end
    return player.components.playercontroller.ismastersim or player:HasTag("idle") or player:HasTag("moving")
end

-- Only continue the same confirmed work; an unrelated job must wait for idle.
function M.repeat_ready(ctx, action, ticket)
    if not action or not guards[action.action.id] then return M.idle(ctx) end
    if not ticket or ticket.failed or ticket.closed or not ticket.sent then return false end
    local previous, player = ticket.action, ctx.player
    if not previous or previous.action ~= action.action or previous.target ~= action.target
        or previous.invobject ~= action.invobject then return M.idle(ctx) end
    if player.components.playercontroller:IsBusy() then return false end
    if not ticket.succeeded and not (ticket.attempt_hint and ticket:performed()) then return false end
    local id, sg = action.action.id, player.sg
    local guard = guards[id]
    local pending = player:GetBufferedAction()
    if pending and pending ~= previous then return false end
    if player:HasTag("beaver") then guard = "gnawing"
    elseif (id == "CHOP" or id == "MINE" or id == "REMOVELUNARBUILDUP")
        and player.GetModuleTypeCount and player:GetModuleTypeCount("spin") > 0 then guard = "prespin" end
    if tagged(player, guard) then return false end
    if not tagged(player, "working") then return M.idle(ctx) end
    local handler = sg and sg.sg and sg.sg.actionhandlers and sg.sg.actionhandlers[action.action]
    if handler then
        return (not handler.condition or handler.condition(player))
            and handler.deststate ~= nil and handler.deststate(player, action) ~= nil
    end
    -- Unpredicted clients only receive the server's working/guard tags.
    return not sg
end

return M
