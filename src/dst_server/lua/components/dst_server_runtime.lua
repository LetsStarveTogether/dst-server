local bootstrap = require("dst_server.bootstrap")
local telemetry = require("dst_server.telemetry")

local Runtime = Class(function(self, inst)
    self.inst = inst
    bootstrap.attach(self)
end)

function Runtime:Initialize()
    if self.installed == nil then
        self.installed = bootstrap.install()
        if self.installed and not pcall(function()
            require("dst_server.connections").install(self.inst)
        end) then
            telemetry.report("connections.install", "installation_failed")
        end
    end
    if self.installed and self.post_initialized and not self.reported then
        self.reported = true
        -- The world network (and its voter) is created after the world prefab.
        if require("dst_server.state").requested_profile ~= "off" and not pcall(function()
            require("dst_server.vote_events").install()
        end) then
            telemetry.report("votes.install", "installation_failed")
        end
        local ok, task = pcall(function()
            return require("dst_server.connections").start(self.inst)
        end)
        if ok then
            self.presence_task = task
        else
            telemetry.report("connections.start", "installation_failed")
        end
        bootstrap.ready()
    end
end

function Runtime:OnRemoveFromEntity()
    if self.presence_task ~= nil then self.presence_task:Cancel() end
end

function Runtime:OnPostInit()
    self.post_initialized = true
    self:Initialize()
end

return Runtime
