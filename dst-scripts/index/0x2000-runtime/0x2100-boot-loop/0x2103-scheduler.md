# `0x21030000` 调度器

调度器页解释两类入口。
`EntityScript:DoTaskInTime()` 与 `DoPeriodicTask()` 进入 `attime` 定时回调表。
`StartThread()` 进入 coroutine task 表，并在 `Scheduler:Run()` 中 resume。

## `0x21031111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是把定时回调和 coroutine 休眠分开。
`ExecuteInTime()` 不会创建 coroutine。
它通过 `ExecutePeriodic(..., limit = 1)` 写入 `attime[wakeuptick]`，并在 `Scheduler:OnTick()` 中直接执行回调。

## `0x21032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/entityscript.lua` | `EntityScript:DoTaskInTime` | 普通实体延迟回调入口 |
| `scripts/entityscript.lua` | `EntityScript:DoPeriodicTask` | 普通实体周期回调入口 |
| `scripts/entityscript.lua` | `EntityScript:DoStaticTaskInTime` | 静态实体延迟回调入口 |
| `scripts/entityscript.lua` | `EntityScript:DoStaticPeriodicTask` | 静态实体周期回调入口 |
| `scripts/scheduler.lua` | `Scheduler:ExecuteInTime` | 把单次回调转换为 limit 为 1 的 periodic |
| `scripts/scheduler.lua` | `Scheduler:ExecutePeriodic` | 计算 wake tick 并写入 `attime` |
| `scripts/scheduler.lua` | `Scheduler:OnTick` | 执行到期 `attime` 回调并唤醒 sleep coroutine |
| `scripts/scheduler.lua` | `Scheduler:Run` | resume `running` coroutine task |
| `scripts/scheduler.lua` | `Sleep` | coroutine 内部按 tick 休眠 |
| `scripts/scheduler.lua` | `RunScheduler` | 普通 scheduler 每 tick 外部入口 |
| `scripts/scheduler.lua` | `RunStaticScheduler` | 静态 scheduler 每 tick 外部入口 |
| `scripts/update.lua` | `Update` | 调用 `RunScheduler(i)` 的主循环位置 |
| `scripts/update.lua` | `StaticUpdate` | 调用 `RunStaticScheduler(i)` 的主循环位置 |

### `0x21032111` 主锚点 / `scripts/entityscript.lua` / 搜索信号

先搜索 `DoTaskInTime`，确认普通实体任务写入 `scheduler`。
再搜索 `DoStaticTaskInTime`，确认静态实体任务写入 `staticScheduler`。

## `0x21033000` 运行流程

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
    M --> P["dead or error removed"]
    Q["StaticUpdate"] --> R["RunStaticScheduler(i)"]
~~~

### `0x21033111` 流程分段 / 定时回调路径 / 边界条件

`DoTaskInTime()` 和 `DoPeriodicTask()` 返回 `Periodic` 对象，并记录在 `inst.pendingtasks`。
到期时 `Scheduler:OnTick()` 直接调用 `k.fn(...)`。
这条路径不经过 `Scheduler:Run()`。

### `0x21033121` 流程分段 / Coroutine Task 路径 / 边界条件

`StartThread()` 和 `StartStaticThread()` 用 `Scheduler:AddTask()` 创建 `Task` 和 coroutine。
`Scheduler:Run()` resume `running` 表中的 coroutine。
如果 coroutine `Sleep(time)`，它会 yield `SLEEP` 和目标 tick，并被放入 `waitingfortick`。

### `0x21033131` 流程分段 / 普通 Scheduler 与静态 Scheduler / 边界条件

`scheduler = Scheduler()` 使用 `GetTick()` 和 `GetTime()`。
`staticScheduler = Scheduler(true)` 使用 `GetStaticTick()` 和 `GetStaticTime()`。
两者都用 `GetTickTime()` 把秒数换成 wake tick。

## `0x21034111` 结构细节 / 数据结构与生命周期 / `Scheduler` 表结构 / 需要核对的字段

`Scheduler` 包含 `tasks`、`running`、`waitingfortick`、`waking`、`hibernating` 和 `attime`。
`attime` 存放 `Periodic` 定时回调。
`running`、`waitingfortick`、`waking`、`hibernating` 存放 coroutine `Task`。

## `0x21034121` 结构细节 / 数据结构与生命周期 / `Periodic` 生命周期 / 需要核对的字段

`Periodic:Cancel()` 会清理 list、`fn`、`arg` 和 `nexttick`，并调用 `onfinish`。
实体任务通过 `task_finish` 从 `inst.pendingtasks` 移除。
单次延迟任务靠 `limit = 1` 在第一次执行后 `Cleanup()`。

## `0x21035100` 阅读与验证路线 / 从哪里开始读源码

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
  -e "RunScheduler" \
  -e "RunStaticScheduler" \
  scripts/entityscript.lua \
  scripts/scheduler.lua \
  scripts/update.lua
~~~

### `0x21035111` 推荐顺序 / 最小闭环

先追 `EntityScript:DoTaskInTime()` 到 `Scheduler:OnTick()` 的定时回调路径。
再追 `StartThread()`、`Sleep()`、`Scheduler:Run()` 的 coroutine 路径。
最后用 `update.lua` 确认普通 scheduler 与静态 scheduler 分别由哪个 tick 入口驱动。
