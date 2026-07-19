# `0x40000000` AI and Animation

A Brain makes longer-term choices, while a StateGraph handles immediate intent and presentation.
Within the Brain, the behaviour tree chooses the next action.

## `0x40001111` StateGraph Runtime

`StateGraph` controls short-lived presentation and action execution.
Start with `StateGraphInstance:StartAction` in `scripts/stategraph.lua`.
Then read `GoToState` and `UpdateState` to trace transitions, timelines, timeouts, and per-state events.

## `0x40001121` Brain Runtime

`Brain` controls longer-term decisions outside the StateGraph.
Start with `BrainManager`, `BT`, and `OnUpdate` in `scripts/brain.lua` and `scripts/behaviourtree.lua`.
A Brain usually produces intent that a StateGraph or component consumes rather than playing an animation directly.

## `0x40002000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/stategraph.lua` | `SGManager` | Updates SG instances, sleep states, and event queues |
| `scripts/stategraph.lua` | `StateGraphInstance` | Binds a state machine instance to an entity |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.Add*` | Appends reusable states to a `states` table |
| `scripts/stategraphs/SGwilson.lua` | `StateGraph("wilson")` | Defines the authoritative player StateGraph |
| `scripts/stategraphs/SGwilson_client.lua` | `StateGraph("wilson_client")` | Defines the predicted client player StateGraph |
| `scripts/brain.lua` | `BrainManager` | Schedules Brain updates |
| `scripts/behaviourtree.lua` | `PriorityNode` | Selects behaviour-tree branches |
| `scripts/update.lua` | `Update` | Calls `SGManager` before `BrainManager` on each simulation tick |

### `0x40002111` StateGraph Search Path

Search for `StateGraphInstance:StartAction` and `StateGraphInstance:UpdateState`.
Together they connect an action entry to its animation-frame effects.

### `0x40002121` Brain Search Path

Search for `BrainManager`, `BT`, and `OnUpdate` to see how AI decisions run outside the StateGraph.

## `0x40003000` Runtime Relationship

~~~mermaid
flowchart TD
    A["Prefab calls SetStateGraph and SetBrain"]
    A --> B["StateGraphInstance"]
    A --> C["Brain"]
    C --> D["BehaviourNode selects intent"]
    D --> E["BufferedAction / PushEvent"]
    D --> I["direct component call"]
    E --> B
    I --> J["combat / locomotor / homeseeker components"]
    B --> F["State.onenter"]
    F --> G["timeline / timeout / events"]
    G --> H["PerformBufferedAction or GoToState"]
~~~

### `0x40003111` Decision and Presentation Boundaries

Many objects and simple creatures have a `StateGraph` without a Brain.
Entities with a Brain often pass a `BufferedAction` to their SG.
Some behaviour leaves instead drive `combat`, `locomotor`, or `homeseeker` directly.

### `0x40003121` Server and Client StateGraphs

`SGwilson.lua` defines authoritative states.
`SGwilson_client.lua` declares predicted-to-server matches with `server_states` and `forward_server_states`.

## `0x40004111` Pages

- [StateGraph](0x4100-stategraph/README.md)
- [StateGraph Runtime](0x4100-stategraph/0x4101-stategraph-runtime.md)
- [CommonStates](0x4100-stategraph/0x4102-commonstates.md)
- [SGwilson](0x4100-stategraph/0x4103-sgwilson.md)
- [Brain and Behaviour](0x4200-brain-behaviour/README.md)
- [Brain Runtime](0x4200-brain-behaviour/0x4201-brain-runtime.md)
- [Behaviour Tree](0x4200-brain-behaviour/0x4202-behaviour-tree.md)
- [Tracing Examples](0x4300-tracing/README.md)
- [AI Tracing Examples](0x4300-tracing/0x4301-ai-examples.md)

## `0x40005100` Verification

~~~bash
rg -n "StateGraphInstance:StartAction|StateGraphInstance:UpdateState|BrainManager|PriorityNode" \
  scripts/stategraph.lua \
  scripts/brain.lua \
  scripts/behaviourtree.lua
~~~

### `0x40005111` Action Loop

Start with `ActionHandler(ACTIONS.EAT, "eat")` in `scripts/stategraphs/SGrabbit.lua`.
Then inspect `SetTimeout`, `ontimeout`, and `PerformBufferedAction` in the `eat` state.

### `0x40005112` AI Loop

Trace the rabbit prefab to its Brain, then back to `SGrabbit`.
This shows the Brain choosing intent and the StateGraph executing it.
