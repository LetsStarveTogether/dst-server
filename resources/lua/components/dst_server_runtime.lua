local bootstrap = require("dst_server.bootstrap")
local connections = require("dst_server.connections")
local state = require("dst_server.state")
local telemetry = require("dst_server.telemetry")

local Runtime = Class(function(self, inst)
    self.inst = inst
    bootstrap.attach(self)
end)

function Runtime:Initialize()
    if self.installed == nil then
        self.installed = bootstrap.install()
        if self.installed then
            self.connections_ready = telemetry.install("connections", function() connections.install(self.inst) end)
        end
    end
    if self.installed and self.post_initialized and not self.reported then
        self.reported = true
        -- The network creates the standard voter during world initialization.
        telemetry.install("voting", function() return require("dst_server.voting").install() end)
        state.control_ready = self.connections_ready and state.capabilities.voting ~= "failed"
        if state.telemetry_active then
            telemetry.install("votes", function() return require("dst_server.vote_events").install() end)
            telemetry.install("presence", function()
                self.presence_task = connections.start(self.inst)
                return self.presence_task ~= nil
            end)
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
