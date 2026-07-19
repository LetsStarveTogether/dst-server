# `0x60000000` Gameplay Systems

This section maps gameplay features by the runtime paths players experience.
Start with the player entity, then follow combat, survival, crafting, boats, events, and follower relationships.

## `0x60001111` Purpose and Scope

Gameplay systems are not a list of prefab names.
A complete behaviour often crosses a `Prefab`, component, action, StateGraph, Brain, world component, and UI or replica.
Find the authoritative server component first.
Inspect the client `replica` and UI inputs when prediction or presentation matters.

## `0x60002000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/prefabs/player_common.lua` | `MakePlayerCharacter` | Assembles player characters |
| `scripts/components/combat.lua` | `Combat` | Selects targets and resolves attacks |
| `scripts/components/health.lua` | `Health` | Applies health changes and death |
| `scripts/components/builder.lua` | `Builder` | Executes authoritative crafting |
| `scripts/components/inventory.lua` | `Inventory` | Stores and grants items |
| `scripts/prefabs/boat.lua` | `walkableplatform` / `boatphysics` | Assembles boat platforms and physics |
| `scripts/constants.lua` | `SPECIAL_EVENTS` | Defines event switches |
| `scripts/components/leader.lua` | `AddFollower` | Manages follower relationships |

### `0x60002111` Choosing an Entry Point

Use `MakePlayerCharacter` to identify the components available on a player.
Enter a feature through its action or authoritative component.
For UI-driven or predicted behaviour, continue through the replica, `playercontroller`, and RPC path.

## `0x60003000` Runtime Map

~~~mermaid
flowchart TD
    A["player_common.lua: MakePlayerCharacter"]
    A --> B["components: combat / builder / inventory"]
    B --> C["actions.lua / bufferedaction.lua"]
    C --> D["StateGraph: SGwilson"]
    D --> E["Prefab side effects"]
    B --> F["world components"]
    F --> G["events / managers / spawners"]
    B --> H["replica / playercontroller / RPC"]
    H --> B
~~~

### `0x60003111` Scope Boundary

These chapters document runtime relationships and source entry points.
Use `0x8000-reference` for exhaustive Prefab, Component, StateGraph, Brain, Widget, and Screen indexes.

## `0x60004111` Pages

- [Characters and Creatures](0x6100-characters-creatures/README.md)
- [Characters and Skill Trees](0x6100-characters-creatures/0x6101-characters-skilltrees.md)
- [Creatures and Bosses](0x6100-characters-creatures/0x6102-creatures-bosses.md)
- [Survival and Combat](0x6200-survival-combat/README.md)
- [Combat and Damage](0x6200-survival-combat/0x6201-combat-damage.md)
- [Survival, Food, and Farming](0x6200-survival-combat/0x6202-survival-food-farming.md)
- [Items, Crafting, and Events](0x6300-items-crafting-events/README.md)
- [Items and Crafting](0x6300-items-crafting-events/0x6301-items-crafting.md)
- [Ocean and Boating](0x6300-items-crafting-events/0x6302-ocean-boating.md)
- [Events and Factions](0x6300-items-crafting-events/0x6303-events-factions.md)

## `0x60005100` Verification

~~~bash
rg -n "MakePlayerCharacter|AddComponent\\(\"combat\"\\)|AddComponent\\(\"builder\"\\)" \
  scripts/prefabs/player_common.lua

rg -n "ACTIONS.ATTACK|ACTIONS.BUILD|DoBuild|Combat:DoAttack|Health:DoDelta" \
  scripts/actions.lua \
  scripts/components/builder.lua \
  scripts/components/combat.lua \
  scripts/components/health.lua

rg -n "Recipe2|boatphysics|SPECIAL_EVENTS|AddFollower|SetLeader" \
  scripts/recipes.lua \
  scripts/prefabs/boat.lua \
  scripts/constants.lua \
  scripts/components/leader.lua \
  scripts/components/follower.lua
~~~

### `0x60005111` Minimal Traces

Trace combat through `ACTIONS.ATTACK -> Combat:DoAttack -> Health:DoDelta`.
Trace crafting through `Recipe2 -> Builder:MakeRecipe -> ACTIONS.BUILD -> Builder:DoBuild`.
Trace an event through `SPECIAL_EVENTS -> IsSpecialEventActive -> world manager -> prefab behaviour`.
