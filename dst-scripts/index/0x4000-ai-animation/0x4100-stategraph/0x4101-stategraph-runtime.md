# `0x41010000` StateGraph Runtime

A `StateGraph` definition becomes an entity-bound `StateGraphInstance`.
The instance schedules actions, events, timelines, and timeouts.

## `0x41011111` Purpose

Read `StateGraphInstance:StartAction` to see why an action usually enters an animation state before it changes the world.
Read `StateGraphInstance:UpdateState` to see why the actual side effect often occurs on a timeline or timeout.

## `0x41011121` Scheduling Boundary

`SGManager` tracks `updaters`, `tickwaiters`, `hibernaters`, and `haveEvents`.
`StateGraphWrangler:Update` updates runnable instances before `UpdateEvents` handles buffered events.

## `0x41012000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/stategraph.lua` | `StateGraphWrangler:Update` | Updates SG instances and queued events |
| `scripts/stategraph.lua` | `ActionHandler` | Maps `ACTIONS.*` to a state or function |
| `scripts/stategraph.lua` | `EventHandler` | Defines global or state-local event responses |
| `scripts/stategraph.lua` | `State` | Collects tags, events, timeline entries, and timeout callbacks |
| `scripts/stategraph.lua` | `StateGraph` | Indexes states, events, and action handlers |
| `scripts/stategraph.lua` | `StateGraphInstance` | Stores entity state, `statemem`, and `mem` |
| `scripts/stategraph.lua` | `SoundFrameEvent` | Plays a sound at a frame with optional volume |

### `0x41012111` Main Runtime Anchors

Search for `StateGraphWrangler:Update` and `StateGraphInstance:StartAction`.
Then read `StateGraphInstance:GoToState` to connect scheduling, action dispatch, and transitions.

### `0x41012121` State Construction Anchors

Search for `State = Class` to see `args.events` converted into `self.events`.
Search for `table.sort(self.timeline, Chronological)` to confirm chronological timeline order.

## `0x41013000` Runtime Flow

~~~mermaid
flowchart TD
    A["StateGraph(name, states, events, defaultstate, actionhandlers)"]
    A --> B["StateGraphInstance(stategraph, inst)"]
    B --> C["StartAction(bufferedaction)"]
    C --> D["ActionHandler.deststate"]
    D --> E["GoToState(state)"]
    E --> F["State.onenter(params)"]
    F --> G["SetTimeout / timelineindex"]
    G --> H["UpdateState(dt)"]
    H --> I["ontimeout or TimeEvent.fn"]
    I --> J["PerformBufferedAction / GoToState"]
    B --> K["PushEvent(event, data)"]
    K --> L["HandleEvents"]
    L --> M["State:HandleEvent, then sg.events fallback"]
~~~

### `0x41013111` Action Dispatch

`StateGraphInstance:StartAction` proceeds only when it finds a `handler` whose `condition` passes.
When `deststate` returns a state name, it sets `statemem.is_going_to_action_state` and calls `GoToState`.
Without `deststate`, it calls `inst:PerformBufferedAction()` directly.

### `0x41013211` Event Dispatch

`StateGraphInstance:HandleEvent` first calls the current state's `State:HandleEvent`.
Only a truthy state-handler result prevents fallback to `self.sg.events[eventname]`.
`PushEvent` stores the originating state in `data.state` so a later state's local handler ignores the stale event.
The graph-level `self.sg.events[eventname]` fallback can still handle it.

### `0x41013311` Timed Dispatch

`StateGraphInstance:UpdateState` decrements the timeout first and stops the current pass if `ontimeout` changes state.
It then executes timeline entries in `timelineindex` order.
If a timeline callback changes state, `UpdateState` calls `self:Update(extra_time)`.
`StateGraphInstance:Update()` accepts no argument, so the current source ignores `extra_time`.

## `0x41014111` Definition Indexes

`StateGraph` indexes action handlers by `v.action` and events by lowercase name.
It indexes states by `v.name`.
Construction also applies `ModManager:GetPostInitData` and `StategraphPostInit`.

## `0x41014121` Instance Memory

`StateGraphInstance` stores `currentstate`, `timeinstate`, `timelineindex`, `bufferedevents`, `tags`, `statemem`, and `mem`.
`GoToState` clears `statemem`, while `mem` persists across states.

## `0x41014211` Tag Synchronization

`GoToState` copies state tags into `self.tags`.
Server entities and entities without Network also mirror SG tags such as `busy`, `moving`, and `attack` to entity tags.

## `0x41014311` Timeline Sound Helper

`SoundFrameEvent(frame, sound_event, vol)` wraps `TimeEvent(frame * FRAMES, ...)`.
It passes `vol` to `SoundEmitter:PlaySound(sound_event, nil, vol)`.
`SGchester.lua` uses this path to lower the boat-jump sound volume.

## `0x41015100` Verification

~~~bash
rg -n "StateGraphWrangler:Update|StateGraphInstance:StartAction|StateGraphInstance:GoToState" \
  scripts/stategraph.lua

rg -n "StateGraphInstance:UpdateState|State:HandleEvent" \
  scripts/stategraph.lua

rg -n "SoundFrameEvent|PlaySound\\(sound_event" \
  scripts/stategraph.lua \
  scripts/stategraphs/SGchester.lua
~~~

### `0x41015111` Minimum Trace

Read how `ActionHandler` stores `deststate` and how `StartAction` calls `GoToState`.
Then see how `UpdateState` triggers `TimeEvent` or `ontimeout`.

### `0x41015112` Sample Checks

Use the `eat` state in `scripts/stategraphs/SGrabbit.lua` to verify the timeout path.
Use the `hop` state to verify that a state with both a timeline and `onupdate` remains active every frame.
