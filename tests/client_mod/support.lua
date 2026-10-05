package.path = "mods/fieldkit/scripts/?.lua;" .. package.path
local T = {}
local scripts = os.getenv("FIELDKIT_GAME_SCRIPTS") or "dst-scripts/scripts"

local function environment(env)
    env = env or {}
    if not getmetatable(env) then setmetatable(env, { __index = _G }) end
    return env
end

local function filename(path)
    return scripts .. "/" .. path:gsub("%.lua$", "") .. ".lua"
end

function T.source(path)
    local file = assert(io.open(filename(path)))
    local text = file:read("*a")
    file:close()
    return text
end

function T.eval(text, env, label)
    return setfenv(assert(loadstring(text, label)), environment(env))()
end

function T.native(path, env)
    return setfenv(assert(loadfile(filename(path))), environment(env))()
end

local primitives = {}
T.native("class", primitives)
T.Class = primitives.Class
primitives.require = function(name) assert(name == "class"); return T.Class end
T.native("vector3", primitives)
T.Vector3 = primitives.Vector3

local current, count = nil, 0
function T.case(name, fn)
    local parent = current
    current = parent and parent .. "/" .. name or name
    local label = current
    local ok, reason = xpcall(fn, debug.traceback)
    current = parent
    if not ok then error("[" .. label .. "]\n" .. tostring(reason), 0) end
    count = count + 1
end

function T.each(group, rows, fn)
    for index, row in ipairs(rows) do
        local name = type(row) == "table" and (row.name or row.id or index) or row
        T.case(group .. "/" .. tostring(name), function() fn(row, index) end)
    end
end

function T.done() print("fieldkit named cases: " .. count) end

T.pointer_orders = {
    { name = "raw/raw", down = true, up = true },
    { name = "raw/control", down = true, up = false },
    { name = "control/raw", down = false, up = true },
    { name = "control/control", down = false, up = false },
}
function T.pointer(raw, control, down, raw_first)
    local first, second = raw_first and raw or control, raw_first and control or raw
    first(down)
    second(down)
end

local event_env = { Class = T.Class, Entity = {},
    string = setmetatable({}, { __index = string }), table = setmetatable({}, { __index = table }),
    math = setmetatable({}, { __index = math }),
    require = function(name)
        if name == "class" then return T.Class end
        assert(name == "entityreplica" or name == "componentactions" or name == "entityscriptproxy",
            "unexpected event dependency: " .. name)
    end,
}
T.native("util", event_env)
T.native("entityscript", event_env)

function T.events(object)
    for _, method in ipairs({ "ListenForEvent", "RemoveEventCallback", "RemoveAllEventCallbacks", "PushEvent", "PushEvent_Internal" }) do
        object[method] = event_env.EntityScript[method]
    end
    return object
end

function T.listener_count(object)
    local total = 0
    for _, listeners in pairs(object.event_listeners or {}) do
        for _, callbacks in pairs(listeners) do total = total + #callbacks end
    end
    return total
end

local Entity = {}
Entity.__index = Entity
function Entity:IsValid() return self.valid ~= false and not self.removed end
function Entity:HasTag(tag) return self.tags[tag] == true end
function Entity:AddTag(tag) self.tags[tag] = true end
function Entity:RemoveTag(tag) self.tags[tag] = nil end
function Entity:GetPosition() return T.Vector3(self.x, self.y, self.z) end
function Entity:GetDistanceSq(point)
    return (self.x - point.x)^2 + (self.y - (point.y or 0))^2 + (self.z - point.z)^2
end
function Entity:GetDistanceSqToInst(other) return self:GetDistanceSq(other:GetPosition()) end
function Entity:GetCurrentPlatform() return self.platform end
function Entity:GetPhysicsRadius(default) return self.Physics and self.Physics:GetRadius() or default or 0 end
function Entity:GetDisplayName() return self.name or self.prefab end
function Entity:GetBufferedAction() return self.bufferedaction end
function Entity:ClearBufferedAction() self.bufferedaction = nil end
function Entity:Remove()
    if self.removed then return end
    self.removed = true
    self:PushEvent("onremove")
    self:RemoveAllEventCallbacks()
end

function T.entity(fields)
    local object = setmetatable(fields or {}, Entity)
    object.x, object.y, object.z = object.x or 0, object.y or 0, object.z or 0
    object.tags, object.components, object.replica = object.tags or {}, object.components or {}, object.replica or {}
    object.Transform = object.Transform or {
        GetWorldPosition = function() return object.x, object.y, object.z end,
        SetPosition = function(_, x, y, z)
            if type(x) == "table" then x, y, z = x.x, x.y, x.z end
            object.x, object.y, object.z = x, y or 0, z
        end,
    }
    object.entity = object.entity or { IsVisible = function() return object.visible ~= false and not object.hidden end }
    return T.events(object)
end

-- Advancing time and delivering callbacks stay explicit in each scenario.
function T.tasks(owner)
    local tasks = {}
    local function add(seconds, fn, periodic)
        local task = { seconds = seconds, fn = fn, periodic = periodic, owner = owner }
        function task:Cancel() self.cancelled = true end
        tasks[#tasks + 1] = task
        return task
    end
    function owner:DoTaskInTime(seconds, fn) return add(seconds, fn, false) end
    function owner:DoPeriodicTask(seconds, fn) return add(seconds, fn, true) end
    return tasks
end

function T.handlers()
    local self = { list = {} }
    function self:add(kind, key, fn)
        local handle = { kind = kind, key = key, fn = fn }
        function handle:Remove() self.removed = true end
        self.list[#self.list + 1] = handle
        return handle
    end
    function self:dispatch(kind, key, ...)
        local callbacks = {}
        for _, handle in ipairs(self.list) do callbacks[#callbacks + 1] = handle end
        for _, handle in ipairs(callbacks) do
            if not handle.removed and handle.kind == kind and handle.key == key then handle.fn(...) end
        end
    end
    return self
end

return T
