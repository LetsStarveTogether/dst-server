local Actions = require("fieldkit/game/actions")
local M = {}

local function finite(value)
    return type(value) == "number" and value == value and math.abs(value) < math.huge
end

function M.new(ctx)
    local self, G, player = {}, ctx.G, ctx.player
    local inventory, controller = player.replica.inventory, player.components.playercontroller
    local function equipped()
        local item = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
        return item and item:IsValid() and item.replica.oceanfishingrod and item or nil
    end
    local function action(item, point, id)
        for _, candidate in ipairs(Actions.candidates(ctx, nil, "right", item, point)) do
            if candidate.action.id == id then return candidate end
        end
    end
    function self:start(point)
        if ctx.config.work == false then return false, ctx:text("交互辅助已关闭", "Interaction assistance is disabled") end
        local ready, reason = ctx:ready()
        if not ready then return false, reason end
        local item = equipped()
        if not item then return false, ctx:text("请先装备海钓竿", "Equip an ocean fishing rod first") end
        if inventory:GetActiveItem() then return false, ctx:text("请先放下鼠标物品", "Put away the cursor item first") end
        if not point or not finite(point.x) or not finite(point.z) then
            return false, ctx:text("请选择有效的海钓位置", "Choose a valid fishing position")
        end
        point = G.Vector3(point.x, 0, point.z)
        local rod = item.replica.oceanfishingrod
        if not rod:GetTarget() and not action(item, point, "OCEAN_FISHING_CAST") then
            return false, ctx:text("所指位置不能海钓", "Ocean fishing is unavailable at that position")
        end
        local limit = ctx.params.fish_count
        local name = ctx:text("自动海钓", "Ocean fishing")
        local job = { name = name, caught = 0, timeout = 600, input = { mode = "world" },
            phase = rod:GetTarget() and "fishing" or "cast",
            timeout_message = ctx:text("海钓已达到时限，辅助停止", "Ocean fishing reached its time limit; assistance stopped"),
            error_message = ctx:text("海钓辅助失败，已停止", "Ocean fishing assistance failed; stopped") }
        local ticket, requested_at, next_reel, catch_target, launched
        local repeat_delay = G.TUNING.OCEAN_FISHING.REEL_ACTION_REPEAT_DELAY
        local function release(reason)
            Actions.stop(ctx, ticket, reason)
            ticket, requested_at = nil, nil
        end
        local function send(id, now)
            local candidate = action(item, point, id)
            if not candidate then return false end
            release("done")
            local why
            ticket, why = Actions.dispatch(ctx, candidate, "right")
            if not ticket then return nil, why end
            requested_at = now
            return true
        end
        function job:step(now)
            if self.closed then return "failed" end
            if equipped() ~= item then
                return "failed", ctx:text("海钓竿已更换，辅助停止", "Fishing rod changed; assistance stopped")
            end
            if not ctx.equipment:ready(self) then return "running" end
            local target = rod:GetTarget()
            if ticket and ticket.failed then
                return "failed", ctx:text("海钓动作失败，辅助停止", "Fishing action failed; assistance stopped")
            end
            if self.phase == "casting" then
                if target then
                    self.phase = "fishing"
                    release("done")
                elseif now - requested_at >= 8 then
                    return "failed", ctx:text("抛竿未获确认，辅助停止", "Cast was not confirmed; assistance stopped")
                end
                return "running"
            elseif self.phase == "catching" then
                -- The same fish becomes a projectile before its inventory form appears.
                -- Keep this observation if replication removes it before the action echo.
                launched = launched or catch_target:IsValid() and catch_target:HasTag("projectile")
                local confirmed = ticket.succeeded or launched and (ticket.attempted or ticket.attempt_hint)
                if not target and confirmed then
                    self.caught, self.phase = self.caught + 1, "cast"
                    catch_target, launched = nil, nil
                    release("done")
                    if self.caught >= limit then
                        return "done", ctx:text("海钓完成，钓获：", "Ocean fishing complete; catches: ") .. self.caught
                    end
                elseif now - requested_at >= 8 then
                    return "failed", ctx:text("收鱼结果未获确认，辅助停止", "Catch result was not confirmed; assistance stopped")
                end
                return "running"
            elseif self.phase == "cast" then
                if controller:IsBusy() or player:HasTag("fishing_idle") then return "running" end
                -- A boat can drift beyond the selected cast point. Do not walk off its deck to recast.
                local range = rod:GetMaxCastDist()
                if player:GetDistanceSqToPoint(point.x, 0, point.z) > range * range then
                    return "failed", ctx:text("钓点已超出抛竿范围，辅助停止", "Fishing point is out of casting range; assistance stopped")
                end
                local sent, why = send("OCEAN_FISHING_CAST", now)
                if not sent then return "failed", why or ctx:text("钓点已不可用", "Fishing point is no longer available") end
                self.phase = "casting"
            else
                if not target then
                    return "failed", ctx:text("鱼线已脱离目标，辅助停止", "Fishing target was lost; assistance stopped")
                end
                if target:HasTag("projectile") or target:HasTag("fishinghook") or controller:IsBusy() then return "running" end
                if ticket and not ticket.sent then
                    if now - requested_at >= 8 then
                        return "failed", ctx:text("收线请求未发送，辅助停止", "Reeling request was not sent; assistance stopped")
                    end
                    return "running"
                end
                if target:HasTag("oceanfishing_catchable") then
                    catch_target, launched = target, false
                    local sent, why = send("OCEAN_FISHING_CATCH", now)
                    if sent == nil then return "failed", why end
                    if sent then self.phase = "catching" end
                elseif not rod:IsLineTensionHigh() and (not next_reel or now >= next_reel) then
                    local sent, why = send("OCEAN_FISHING_REEL", now)
                    if sent == nil then return "failed", why end
                    if sent then next_reel = now + repeat_delay + .1 end
                end
            end
            self.name = string.format("%s · %d/%d", name, self.caught, limit)
            return "running"
        end
        function job:cancel(reason)
            self.closed = true
            -- Release automation without unequipping the rod: native interruption can lose tackle.
            release(reason)
        end
        return ctx.executor:start(job)
    end
    return self
end

return M
