# `0x41030000` SGwilson

SGwilson is the main player-presentation example.
It covers authoritative actions, client prediction, failure recovery, and specialized states.

## `0x41031111` Purpose

Trace one player action from its `ActionHandler` through the target state, timeline, and `PerformBufferedAction` call.
Avoid reading all of `SGwilson.lua` in file order.

## `0x41031121` Server and Client Roles

`scripts/stategraphs/SGwilson.lua` returns `StateGraph("wilson", ...)`.
`scripts/stategraphs/SGwilson_client.lua` returns `StateGraph("wilson_client", ...)`.
`server_states` declares the accepted authoritative states.

## `0x41032000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/stategraphs/SGwilson.lua` | `local actionhandlers` | Maps server player actions to states |
| `scripts/stategraphs/SGwilson.lua` | `name = "doshortaction"` | Executes common short actions |
| `scripts/stategraphs/SGwilson.lua` | `name = "attack"` | Executes common combat actions |
| `scripts/stategraphs/SGwilson.lua` | `CommonStates.AddHopStates` | Reuses player boat-jump states |
| `scripts/stategraphs/SGwilson_client.lua` | `server_states` | Matches predicted states to server states |
| `scripts/actions.lua` | `ACTIONS` | Defines action constants |

### `0x41032111` Server Search Path

Search `scripts/stategraphs/SGwilson.lua` for `ActionHandler(ACTIONS.PICKUP`, `ActionHandler(ACTIONS.CHOP`, or `ActionHandler(ACTIONS.ATTACK`.
Then open the returned state, such as `name = "doshortaction"`, `name = "chop"`, or `name = "attack"`.

### `0x41032121` Client Search Path

Search `scripts/stategraphs/SGwilson_client.lua` for the same `ACTIONS.*` value and state name.
Then inspect `server_states` and `PerformPreviewBufferedAction()`.

## `0x41033000` Action Flow

~~~mermaid
flowchart TD
    A["BufferedAction: ACTIONS.PICKUP / CHOP / ATTACK"]
    A --> B["SGwilson action handler"]
    B --> C["Target State"]
    C --> D["onenter stores action or target"]
    D --> E["timeline / timeout / animover"]
    E --> F["PerformBufferedAction"]
    F --> G["Component or prefab side effect"]
    A --> H["SGwilson_client action handler"]
    H --> I["Predicted State"]
    I --> J["PerformPreviewBufferedAction"]
    J --> K["server_states reconciles the server result"]
~~~

### `0x41033111` Short Actions

The server handler for `ACTIONS.PICKUP` branches on riding state and target tags such as `heavy` and `minigameitem`.
In a short-action state, a timeline entry usually calls `inst:PerformBufferedAction()` at the action frame.

### `0x41033211` Work Actions

`ACTIONS.CHOP` spans states such as `chop_start` and `chop` to model startup, looping, and work frames.
The corresponding client state uses `server_states = { "chop_start", "chop" }`.

### `0x41033311` Attacks

The `attack` state stores its target and branches on weapon type and special attacks.
Inspect `combat:StartAttack()`, the timeline action frame, and failure recovery.
Do not stop at the first `PerformBufferedAction` match.

## `0x41034111` Action Handler Table

`local actionhandlers` is the main server-side player-action index.
Each entry can return a state name directly or compute one from equipment, target, riding, boat, and platform conditions.

## `0x41034211` State Side Effects

An action handler normally selects a state rather than executing the action.
The state's `onenter`, `timeline`, `ontimeout`, or event handler decides when to call `PerformBufferedAction`.

## `0x41034311` Client Prediction

`server_states` declares which server states can validate the current predicted state.
`forward_server_states = true` carries the previous matching set across a transition.
These fields feed the client branches of `StateGraphInstance:ServerStateMatches` and `GoToState` in `stategraph.lua`.

## `0x41035100` Verification

~~~bash
rg -n "ActionHandler\\(ACTIONS\\.(PICKUP|CHOP|ATTACK)|PerformBufferedAction|server_states" \
  scripts/stategraphs/SGwilson.lua \
  scripts/stategraphs/SGwilson_client.lua

rg -n "name = \\\"(doshortaction|chop_start|chop|attack)\\\"" \
  scripts/stategraphs/SGwilson.lua \
  scripts/stategraphs/SGwilson_client.lua
~~~

### `0x41035111` Minimum Trace

Choose `ACTIONS.PICKUP` and follow its server handler to the target action frame.
Then find the client prediction state and its `server_states`.

### `0x41035112` Failure Path

Search for `actionfailed` to find states that return to `idle` or enter a dedicated recovery state.
This confirms that the successful flow is not the only path.
