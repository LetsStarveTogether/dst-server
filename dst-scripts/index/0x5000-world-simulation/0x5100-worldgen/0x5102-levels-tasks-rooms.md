# `0x51020000` Level Task Room

本页解释 `Level -> Task -> Room -> Story Graph` 如何把 preset 数据变成拓扑节点。
它重点区分 `map/levels.lua` 的注册数据、`map/level.lua` 的运行时类和 `map/storygen.lua` 的节点构造。

## `0x51021111` 本页定位 / 要回答的运行时问题 / 三层数据各自决定什么 / 验证点

`Level` 选择 task set。
`Task` 提供 `room_choices`、锁钥和背景房间。
`Room` 提供 tile、tag、prefab distribution 和 static layout 计数。
`Story` 把这些表转换成 `Graph` 节点，再交给 `WorldSim`。

## `0x51021112` 本页定位 / 要回答的运行时问题 / 三层数据各自决定什么 / 边界条件

`map/levels.lua` 不是 `Level` 类定义文件。
`Level` 类定义在 `scripts/map/level.lua`。
`map/levels.lua` 负责加载 preset、注册 `AddLevel` 和 `AddWorldGenLevel` 数据。

## `0x51022000` 源码锚点

| 文件 | 入口 | 作用 |
| --- | --- | --- |
| `scripts/map/levels.lua` | `AddWorldGenLevel` | 注册 worldgen preset |
| `scripts/map/levels.lua` | `AddLevel` | 注册 frontend/settings 可见 preset |
| `scripts/map/level.lua` | `Level` | 包装 level data 并保存 overrides |
| `scripts/map/level.lua` | `Level:ChooseTasks` | 根据 `overrides.task_set` 选择 task |
| `scripts/map/tasksets.lua` | `AddTaskSet` / `GetGenTasks` | 注册并取回 task set |
| `scripts/map/tasksets/forest.lua` | `AddTaskSet("default")` | 森林默认 task set |
| `scripts/map/tasksets/caves.lua` | `AddTaskSet("cave_default")` | 洞穴默认 task set |
| `scripts/map/tasks.lua` | `AddTask` | 注册 task 定义 |
| `scripts/map/task.lua` | `Task` | 保存 task 字段并规范化锁钥 |
| `scripts/map/rooms.lua` | `AddRoom` | 注册 room 定义 |
| `scripts/map/storygen.lua` | `Story:GenerateNodesFromTask` | 把 task 的 rooms 变成 graph nodes |

### `0x51022111` `Level` 注册与运行时对象 / `scripts/map/levels.lua` / 搜索信号

搜索 `AddWorldGenLevel` 可以看到 worldgen preset 如何进入 `worldgenlist`。
搜索 `AddLevel` 可以看到 settings preset 如何进入 `levellist`。

### `0x51022112` `Level` 注册与运行时对象 / `scripts/map/levels.lua` / 代码事实

`AddWorldGenLevel` 和 `AddLevel` 都会调用 `Level(data)`。
因此注册页本身保存的是数据入口，运行时字段含义仍要回到 `map/level.lua`。

### `0x51022121` `Level` 注册与运行时对象 / `scripts/map/level.lua` / 搜索信号

搜索 `function Level:ChooseTasks()`。
它断言 `self.overrides["task_set"] ~= nil`，然后用 `tasksets.GetGenTasks(task_set)` 拉取任务集合。

### `0x51022122` `Level` 注册与运行时对象 / `scripts/map/level.lua` / 代码事实

`ChooseTasks` 会把 task set 数据合并进 `self`。
随后它按 `self.tasks`、`self.optionaltasks` 和 mod hook 形成 `self.chosen_tasks`。

### `0x51022131` `Level` 注册与运行时对象 / `scripts/map/tasksets.lua` / 搜索信号

搜索 `AddTaskSet`、`GetGenTasks` 和 `require("map/tasksets/forest")`。
`tasksets.lua` 不是 task 定义文件，而是 task 集合注册器。

### `0x51022132` `Level` 注册与运行时对象 / `scripts/map/tasksets.lua` / 代码事实

`GetGenTasks(id)` 会从 mod task set 或原生 `taskgrouplist` 取回深拷贝。
`Level:ChooseTasks` 读取的 `self.tasks`、`self.optionaltasks`、`valid_start_tasks`、`numoptionaltasks` 等字段都来自这个返回值。

### `0x51022211` `Task` 与 `Room` / `scripts/map/tasks.lua` / 搜索信号

搜索 `AddTask`、`room_choices`、`background_room`、`locks` 和 `keys_given`。
这些字段是 `Story:GenerateNodesFromTask` 读取的主要 task 合同。

### `0x51022212` `Task` 与 `Room` / `scripts/map/tasks.lua` / 代码事实

`AddTask(name, data)` 会创建 `Task(name, data)` 并插入 `taskdefinitions`。
`Level:EnqueueATask` 之后用 `tasks.GetTaskByName(taskname)` 复制这些定义。

### `0x51022221` `Task` 与 `Room` / `scripts/map/rooms.lua` / 搜索信号

搜索 `AddRoom` 和 `contents`。
Room 的 `contents` 会在 storygen 阶段被复制到 node 的 `terrain_contents`。

### `0x51022222` `Task` 与 `Room` / `scripts/map/rooms.lua` / 代码事实

`AddRoom` 把 room data 保存到 `rooms[name]`。
`Story:GetRoom` 会 `deepcopy(Rooms.GetRoomByName(roomname))`，再应用 `RoomPreInit` mod hook。

## `0x51023000` 运行流程

~~~mermaid
flowchart TD
    A["map/levels.lua AddWorldGenLevel"]
    A --> B["Level(data)"]
    B --> C["Level:ChooseTasks"]
    C --> D["tasksets.GetGenTasks(overrides.task_set)"]
    D --> E["task set 字段合并到 Level"]
    E --> F["Level:EnqueueATask"]
    F --> G["tasks.GetTaskByName"]
    G --> H["Story:GenerateNodesFromTask"]
    H --> I["Story:GetRoom"]
    I --> J["Graph(task.id)"]
    J --> K["task_node:AddNode"]
    K --> L["node.data.terrain_contents"]
~~~

### `0x51023111` Level 阶段 / `Level:ChooseTasks` / 必要输入

`overrides.task_set` 是强制字段。
没有它时 `ChooseTasks` 会断言失败。
`tasksets.GetGenTasks(task_set)` 返回 nil 时也会断言失败，并提示 preset 可能依赖未启用的 mod。

### `0x51023112` Level 阶段 / `Level:ChooseTasks` / Mod 介入点

`ChooseTasks` 会触发 `TaskSetPreInit`、`TaskSetPreInitAny`、`LevelPreInit` 和 `LevelPreInitAny`。
因此 task list 的最终形态不只来自静态 preset。

### `0x51023211` Task 阶段 / `Level:EnqueueATask` / 数据复制

`EnqueueATask` 使用 `deepcopy(task)`。
后续 `ApplyModsToTasks` 和 `GetOverridesForTasks` 修改的是本次生成的 task 副本。

### `0x51023212` Task 阶段 / `Level:EnqueueATask` / 常见字段

`room_choices` 决定显式房间数量。
`background_room` 决定背景节点模板。
`room_bg` 决定 task graph 的默认地皮。
`locks` 和 `keys_given` 参与 `LinkNodesByKeys` 或 `RestrictNodesByKey`。
`region_id` 会让 `Story` 把 task 放进 `region_tasksets`，非 `mainland` 区域随后由 `AddRegionsToMainland` 连接回主大陆。

### `0x51023311` Room 阶段 / `Story:GenerateNodesFromTask` / 房间入栈

`entrance_room` 会先被随机判断并压入 `room_choices` stack。
随后 task 的 `room_choices` 按数量生成 room 副本并压栈。

### `0x51023312` Room 阶段 / `Story:GenerateNodesFromTask` / 节点字段

每个 room 最终进入 `task_node:AddNode`。
节点数据里保留 `type`、`task`、`name`、`value`、`tags`、`terrain_contents`、`terrain_contents_extra` 和 `required_prefabs`。

### `0x51023411` Story Graph 阶段 / `Story:GenerateNodesForRegion` / Task 间连接

`GenerateNodesForRegion` 先为每个 task 调用 `GenerateNodesFromTask`。
然后根据 `layout_mode` 选择 `RestrictNodesByKey` 或 `LinkNodesByKeys`。
`RestrictNodesByKey` 只在 `layout_mode` 字符串匹配时使用。
其他模式会落回 `LinkNodesByKeys`。

### `0x51023412` Story Graph 阶段 / `Story:GenerateNodesForRegion` / 起点和循环

`_FindStartingTask` 优先选择无锁 task。
`_AddPlayerStartNode` 会插入玩家起点。
如果 `loop_percent` 命中，`SeperateStoryByBlanks` 会添加隔离节点形成 loop。

## `0x51024111` 结构细节 / 字段对应关系 / Level 字段 / 读取位置

`location` 被 `GenerateNew` 用作地图 prefab。
`overrides` 被 `forest_map.Generate` 复制为 `current_gen_params`。
`background_node_range` 和 `blocker_blank_room_name` 被 `Story:GenerationPipeline` 和 `AddBGNodes` 使用。

## `0x51024121` 结构细节 / 字段对应关系 / Task 字段 / 读取位置

`room_choices` 在 `GenerateNodesFromTask` 展开。
`set_pieces` 和 `random_set_pieces` 会挂到 `Graph(task.id, {...})`。
`substitutes` 会在 `RunTaskSubstitution` 里影响 `distributeprefabs`。

## `0x51024131` 结构细节 / 字段对应关系 / Room 字段 / 读取位置

`contents.fn` 会在 room 入栈后执行。
`contents.distributeprefabs` 会被 task substitution 改写。
`tags` 会通过 `GetExtrasForRoom` 变成 extra prefabs、static layouts、额外 tags 或 global tags。

## `0x51024211` 结构细节 / 易错点 / `Level` 与 `levels.lua` / 校验方式

如果文档说 `levels.lua` 定义 `Level` 类，就是错误。
应该写成 `levels.lua` 注册 preset，`level.lua` 定义 `Level` 类。

## `0x51024221` 结构细节 / 易错点 / Room 与 Prefab / 校验方式

Room 不直接 Spawn 运行时实体。
Room 只把 `terrain_contents` 写入拓扑节点。
实体坐标要等 `forest_map.Generate` 中的 `PopulateVoronoi`、`object_layout` 和海洋流程执行。

## `0x51025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "AddWorldGenLevel|AddLevel|function Level:ChooseTasks|function Level:EnqueueATask" \
  scripts/map/levels.lua \
  scripts/map/level.lua

rg -n "AddTaskSet|GetGenTasks|AddTask|AddRoom|GenerateNodesFromTask|GenerateNodesForRegion" \
  scripts/map/levels.lua \
  scripts/map/level.lua \
  scripts/map/tasksets.lua \
  scripts/map/tasksets/forest.lua \
  scripts/map/tasksets/caves.lua \
  scripts/map/tasks.lua \
  scripts/map/task.lua \
  scripts/map/rooms.lua \
  scripts/map/storygen.lua
~~~

### `0x51025111` 推荐顺序 / 最小闭环

先在 `map/levels.lua` 找一个 preset。
再到 `map/level.lua` 看 `ChooseTasks` 如何从 `task_set` 取 task。
接着在 `map/tasks.lua` 找 task 的 `room_choices`。
最后在 `map/storygen.lua` 看这些 room 如何进入 `task_node:AddNode`。

### `0x51025112` 推荐顺序 / 人工核对清单

- `overrides.task_set` 是否是 task 选择的真实入口。
- `tasksets.GetGenTasks` 是否先返回 task set 深拷贝。
- `AddTask` 是否先构造 `Task(name, data)`。
- `Story:GetRoom` 是否对 room 做 `deepcopy`。
- `node.data.terrain_contents` 是否来自 room 的 `contents`。
- `LinkNodesByKeys` 或 `RestrictNodesByKey` 是否发生在每个 task graph 生成之后。
