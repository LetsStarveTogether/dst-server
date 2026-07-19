# `0x21030000` Scheduler

`EntityScript:DoTaskInTime()` and `DoPeriodicTask()` create entries in `attime`.

`StartThread()` creates coroutine tasks resumed by `Scheduler:Run()`.

## `0x21031111` Purpose

Timed callbacks and sleeping coroutines use different scheduler paths.

`ExecuteInTime()` does not create a coroutine.

It calls `ExecutePeriodic(..., limit = 1)` and stores the callback in `attime[wakeuptick]`.

`Scheduler:OnTick()` executes that callback directly.

## `0x21032000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/entityscript.lua` | `EntityScript:DoTaskInTime` | Schedule a normal one-shot entity callback |
| `scripts/entityscript.lua` | `EntityScript:DoPeriodicTask` | Schedule a normal periodic entity callback |
| `scripts/entityscript.lua` | `EntityScript:DoStaticTaskInTime` | Schedule a static one-shot entity callback |
| `scripts/entityscript.lua` | `EntityScript:DoStaticPeriodicTask` | Schedule a static periodic entity callback |
| `scripts/scheduler.lua` | `Scheduler:ExecuteInTime` | Create a periodic callback with `limit = 1` |
| `scripts/scheduler.lua` | `Scheduler:ExecutePeriodic` | Calculate the wake tick and populate `attime` |
| `scripts/scheduler.lua` | `Scheduler:OnTick` | Run due callbacks and wake sleeping coroutines |
| `scripts/scheduler.lua` | `Scheduler:Run` | Resume coroutine tasks in `running` |
| `scripts/scheduler.lua` | `Sleep` / `Yield` | Suspend or yield the current coroutine |
| `scripts/scheduler.lua` | `RunScheduler` | Drive the normal scheduler each tick |
| `scripts/scheduler.lua` | `RunStaticScheduler` | Drive the static scheduler each static tick |
| `scripts/update.lua` | `Update` | Call `RunScheduler(i)` |
| `scripts/update.lua` | `StaticUpdate` | Call `RunStaticScheduler(i)` |

### `0x21032111` Primary Search

Find `DoTaskInTime` to confirm that normal tasks use `scheduler`.

Then find `DoStaticTaskInTime` to confirm that static tasks use `staticScheduler`.

## `0x21033000` Runtime Flow

~~~mermaid
flowchart TD
    A["EntityScript:DoTaskInTime"] --> B["scheduler:ExecuteInTime"]
    B --> C["Scheduler:ExecutePeriodic limit=1"]
    C --> D["GetListForTimeFromNow"]
    D --> E["attime[wakeuptick]"]
    F["update.lua: Update"] --> G["RunScheduler(i)"]
    G --> H["Scheduler:OnTick(i)"]
    H --> I["execute due attime callbacks"]
    H --> J["move waitingfortick coroutine tasks to waking"]
    G --> K["Scheduler:Run()"]
    K --> L["resume running coroutine tasks"]
    L --> M{"yield type"}
    M --> N["HIBERNATE to hibernating"]
    M --> O["SLEEP to waitingfortick"]
    M --> P["plain yield remains running"]
    M --> Q["remove dead or failed task"]
    R["StaticUpdate"] --> S["RunStaticScheduler(i)"]
~~~

### `0x21033111` Timed Callbacks

`DoTaskInTime()` and `DoPeriodicTask()` return `Periodic` objects stored in `inst.pendingtasks`.

When due, `Scheduler:OnTick()` calls `k.fn(...)` directly without going through `Scheduler:Run()`.

### `0x21033121` Coroutine Tasks

`StartThread()` and `StartStaticThread()` call `Scheduler:AddTask()` to create a `Task` and coroutine.

`Scheduler:Run()` resumes tasks in `running`.

`Sleep(time)` yields `SLEEP` and a target tick only when that tick is in the future.

Otherwise, it performs a plain yield.

`Yield()` also performs a plain yield, leaving the task in `running` for the next `Scheduler:Run()`.

### `0x21033131` Normal and Static Schedulers

`scheduler = Scheduler()` uses `GetTick()` and `GetTime()`.

`staticScheduler = Scheduler(true)` uses `GetStaticTick()` and `GetStaticTime()`.

Both use `GetTickTime()` to convert seconds to wake ticks.

## `0x21034111` Scheduler Tables

`Scheduler` owns `tasks`, `running`, `waitingfortick`, `waking`, `hibernating`, and `attime`.

`attime` holds `Periodic` callbacks, while the other execution tables hold coroutine `Task` objects.

## `0x21034121` `Periodic` Lifecycle

`Periodic:Cancel()` removes the callback from its list, clears `fn`, `arg`, and `nexttick`, and calls `onfinish`.

Entity tasks use `task_finish` to leave `inst.pendingtasks`.

One-shot tasks call `Cleanup()` after their first run because `limit = 1`.

## `0x21035100` Verification

~~~bash
rg -n \
  -e "DoTaskInTime" \
  -e "DoStaticTaskInTime" \
  -e "DoPeriodicTask" \
  -e "ExecuteInTime" \
  -e "ExecutePeriodic" \
  -e "Scheduler:OnTick" \
  -e "Scheduler:Run" \
  -e "StartThread" \
  -e "Sleep" \
  -e "Yield" \
  -e "RunScheduler" \
  -e "RunStaticScheduler" \
  scripts/entityscript.lua \
  scripts/scheduler.lua \
  scripts/update.lua
~~~

### `0x21035111` Next Read

Trace `EntityScript:DoTaskInTime()` to `Scheduler:OnTick()`.

Then trace `StartThread()`, `Sleep()`, `Yield()`, and `Scheduler:Run()`.

Finally, confirm the normal and static entry points in `update.lua`.
