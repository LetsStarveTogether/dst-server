# `0x63030000` Events and Factions

Special-event switches affect resources, world components, and prefab behaviour.
They also shape follower and hostile relationships.
This guide follows those runtime effects instead of listing festival files.
Use `0x8000-reference` for exhaustive indexes.

## `0x63031111` Event Entry Points

`SPECIAL_EVENTS`, `WORLD_SPECIAL_EVENT`, and `WORLD_EXTRA_EVENTS` in `scripts/constants.lua` define the initial state.
`ApplySpecialEvent` and `ApplyExtraEvent` update that state.
`mainfunctions.lua` and `gamelogic.lua` then load global, backend, and frontend event prefabs.
World code and prefabs enable gameplay through `IsSpecialEventActive`.

## `0x63031211` Relationship Model

DST Lua has no single faction table that covers every friendly or hostile relationship.
Relationships commonly combine tags, `leader`, `follower`, and `combat:SetTarget`.
`Combat:TryRetarget` and prefab- or Brain-specific functions complete the result.
Trace actual call sites instead of searching only for `faction`.

## `0x63032000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/constants.lua` | `SPECIAL_EVENTS` | Defines event constants and switches |
| `scripts/simutil.lua` | `ApplySpecialEvent` | Applies event state |
| `scripts/mainfunctions.lua` | `GlobalInit` | Loads global event prefabs |
| `scripts/gamelogic.lua` | `TheSim:LoadPrefabs` | Loads backend and frontend event prefabs |
| `scripts/prefabs/event_deps.lua` | `SPECIAL_EVENT_DEPS` | Declares event resource dependencies |
| `scripts/prefabs/forest.lua` | `AddComponent("carnivalevent")` | Attaches world event components |
| `scripts/components/carnivalevent.lua` | `SpawnCarnivalHost` | Demonstrates a Carnival world component |
| `scripts/prefabs/carnival_plaza.lua` | `TheWorld.components.carnivalevent:RegisterPlaza` | Connects an event prefab to the world component |
| `scripts/prefabs/carnivalgame_golfgame.lua` | `AddGolfProp` / `SaveCourseData` | Course state and saves |
| `scripts/prefabs/carnivalgame_golfprops.lua` | `spring_*` / `movingwall_*` | Golf obstacles and force |
| `scripts/prefabs/carnivalgame_golf_tee.lua` | `SetIsCustomizable` / `OnDeactivateGame` | Tee customization |
| `scripts/components/golfclub.lua` | `StartAiming` / `OnSwingHit` | Handles aiming and swings |
| `scripts/components/golfable.lua` | `OnHit` / `OnExternalPhysics` | Handles ball hits and external force |
| `scripts/components/yoth_knightmanager.lua` | `IsKnightShrineActive` | Demonstrates a Year of the Horse manager |
| `scripts/components/leader.lua` | `AddFollower` | Stores followers |
| `scripts/components/follower.lua` | `SetLeader` | Maintains leadership and loyalty |
| `scripts/components/combat.lua` | `TryRetarget` / `SetTarget` | Selects hostile targets |

### `0x63032111` Event Switches

`IsSpecialEventActive(event)` returns true when `WORLD_SPECIAL_EVENT == event` or `WORLD_EXTRA_EVENTS[event] == true`.
`GetValidRecipe`, prefab assembly, menu skins, world components, and loot logic call it directly.
Events therefore affect recipes, loot, spawners, and prefab behaviour as well as frontend themes.

### `0x63032211` World Components

On the master simulation, `forest.lua` adds `carnivalevent` and `yotd_raceprizemanager`.
It also adds `yotc_raceprizemanager` and `yotb_stagemanager`.
Other managers include `yoth_knightmanager` and `yoth_hecklermanager`.
Some components always exist but remain inactive until `IsSpecialEventActive` or shrine and stage state enables them.

## `0x63033000` Runtime Flow

~~~mermaid
flowchart TD
    A["constants.lua: SPECIAL_EVENTS"]
    A --> B["simutil.lua: ApplySpecialEvent / ApplyExtraEvent"]
    B --> C["mainfunctions.lua: SPECIAL_EVENT_GLOBAL_PREFABS"]
    B --> D["gamelogic.lua: backend / frontend event prefabs"]
    D --> E["event_deps.lua: SPECIAL_EVENT_DEPS"]
    B --> F["forest.lua: world event components"]
    F --> G["carnivalevent.lua / YOT managers"]
    G --> H["Event prefabs: plaza, shrine, stage, race"]
    H --> I["leader / follower / combat / tag checks"]
    G --> J["carnivalgame_golfgame / golfclub / golfable"]
~~~

### `0x63033111` Event Dependency Loading

`GlobalInit` loads `SPECIAL_EVENT_GLOBAL_PREFABS`.
`gamelogic.lua` loads every event's backend prefab because world settings can override the active event.
Frontend prefabs follow `SPECIAL_EVENT_FRONTEND_PREFABS`.
`prefabs/event_deps.lua` centralizes event resource dependencies.

### `0x63033211` Carnival World Example

`carnivalevent` is a master-simulation component.
`SpawnCarnivalHost` first checks `IsSpecialEventActive(SPECIAL_EVENTS.CARNIVAL)`.
When active, `carnival_plaza` adds `activatable`.
It registers through `TheWorld.components.carnivalevent:RegisterPlaza`.
This path carries a global event switch into a concrete world entity.

### `0x63033221` Carnival Golf Example

`carnivalgame_golfgame` owns course bounds, `courseparts`, `trackedcourseparts`, `SaveCourseData`, and scoring.
The `golfclub` collector in `componentactions.lua` creates `ACTIONS.GOLF_START_AIMING`.
`actions.lua` delegates aiming to `components/golfclub.lua`, whose `OnSwingHit` calls `golfable:OnHit`.
`TERRAFORM_REMOVE` removes props that have `terraformerremoveable`.
`proxy_destroy_entity` ties course visuals and placement blockers back to the course.
`carnivalgame_golfprops.lua` synchronizes obstacles and applies force through `golfable:OnExternalPhysics`.
`carnivalgame_golfball.lua` checks hole attraction only near the ground to avoid airborne false positives.
`carnivalgame_golf_tee.lua` refunds custom-mode tokens on deactivation.
While asleep, it uses immediate animation and light cleanup.

### `0x63033311` Followers and Hostile Targets

The `Leader`, `Follower`, and `Combat` components shape these relationships.
`Leader:AddFollower` stores the follower and calls `follower.components.follower:SetLeader(self.inst)`.
`Follower:SetLeader` handles the old leader, item source, leash, `leaderchanged`, and friendly-target cleanup.
`Combat:TryRetarget` updates the target only when its target function returns a valid entity.
Faction-like behaviour is therefore the combination of following rules and target eligibility, not one global state.

## `0x63034111` Events, Recipes, and Technology

`Recipe` supports `require_special_event`.
`GetValidRecipe` filters that field through `IsSpecialEventActive`.
`ApplyEvent` in `simutil.lua` also enables the event's `TECH`.
Verify event recipes across `constants.lua`, `recipe.lua`, `recipes.lua`, and world settings.

## `0x63034211` Event Prefab Lifecycle

`carnival_host` checks `IsSpecialEventActive(SPECIAL_EVENTS.CARNIVAL)`.
After `carnival_plaza` calls `RegisterPlaza`, it pushes `ms_carnivalplazabuilt`.
The host listens for that event and can then appear at the plaza.
For any event prefab, find world-component registration before reading its StateGraph and Brain.

## `0x63034311` Annual Event Managers

`yoth_knightmanager` listens for `ms_knightshrineactivated` and `ms_knightshrinedeactivated`.
`IsKnightShrineActive` requires both a shrine and `IsSpecialEventActive(SPECIAL_EVENTS.YOTH)`.
Event managers commonly combine a world entity with an active event switch.

## `0x63035100` Verification

~~~bash
rg -n "SPECIAL_EVENTS|WORLD_SPECIAL_EVENT|WORLD_EXTRA_EVENTS|IsSpecialEventActive" \
  scripts/constants.lua \
  scripts/simutil.lua

rg -n "SPECIAL_EVENT_GLOBAL_PREFABS|SPECIAL_EVENT_BACKEND_PREFABS|SPECIAL_EVENT_FRONTEND_PREFABS" \
  scripts/mainfunctions.lua \
  scripts/gamelogic.lua \
  scripts/prefabs/event_deps.lua

rg -n "carnivalevent|RegisterPlaza|SpawnCarnivalHost|IsKnightShrineActive" \
  scripts/prefabs/forest.lua \
  scripts/components/carnivalevent.lua \
  scripts/prefabs/carnival_plaza.lua \
  scripts/components/yoth_knightmanager.lua

rg -n "carnivalgame_golfgame_kit|CARNIVAL_GOLFPROPS_ONE|IsGolfPropWithinGolfArea" \
  scripts/recipes.lua

rg -n "GOLF_START_AIMING|GOLF_START_CHARGING|TERRAFORM_REMOVE|golfclub|golfable" \
  scripts/actions.lua \
  scripts/componentactions.lua \
  scripts/components/golfclub.lua \
  scripts/components/golfable.lua \
  scripts/components/terraformerremoveable.lua \
  scripts/prefabs/carnivalgame_golfgame.lua \
  scripts/prefabs/carnivalgame_golfball.lua \
  scripts/prefabs/carnivalgame_golfprops.lua \
  scripts/prefabs/carnivalgame_golf_tee.lua

rg -n "AddFollower|SetLeader|TryRetarget|SetTarget|leaderchanged" \
  scripts/components/leader.lua \
  scripts/components/follower.lua \
  scripts/components/combat.lua
~~~

### `0x63035111` Reading Order

Confirm the event name and switch in `constants.lua`.
Read the apply functions in `simutil.lua`.
Then verify prefab loading in `mainfunctions.lua` and `gamelogic.lua`.
Use Carnival, Carnival golf, or YOTH as the sample.
Continue through its world component, prefab, and relationship logic.
