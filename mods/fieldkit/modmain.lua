-- A dedicated server never installs client hooks, widgets, or input handlers.
if not GLOBAL.TheNet:IsDedicated() then
    Assets = { Asset("ANIM", "anim/firefighter_placement.zip"), Asset("ANIM", "anim/lavaarena_item_pickup_fx.zip") }
    require("fieldkit/application").install(env)
end
