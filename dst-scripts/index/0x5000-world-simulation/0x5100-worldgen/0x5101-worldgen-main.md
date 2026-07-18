# `0x51010000` Worldgen 主入口

Worldgen 主入口页解释一次新世界生成如何从 `GEN_PARAMETERS` 进入独立 Lua 环境。
它重点跟踪 `worldgen_main.lua -> forest_map.lua -> storygen.lua -> WorldSim -> savedata`。

## `0x51011111` 本页定位 / 要回答的运行时问题 / 世界生成为什么不像普通运行时更新 / 验证点

`scripts/worldgen_main.lua` 在文件末尾直接 `return LoadParametersAndGenerate(false)`。
这说明它是一个被宿主加载后立即执行的 worldgen 入口，而不是 `main.lua` 主循环中的常驻模块。

## `0x51011112` 本页定位 / 要回答的运行时问题 / 世界生成为什么不像普通运行时更新 / 边界条件

本页只覆盖新世界生成路径。
存档升级、洞穴回填和运行时刷实体不在这个入口里完成。

## `0x51012000` 源码锚点

| 文件 | 入口 | 作用 |
| --- | --- | --- |
| `scripts/worldgen_main.lua` | `LoadParametersAndGenerate` | 解码 `GEN_PARAMETERS` 并设置 DLC |
| `scripts/worldgen_main.lua` | `GenerateNew` | 构建 `Level`、选择 tasks、重试生成 |
| `scripts/worldgen_main.lua` | `AddSetPeices` | 把 boons、traps、poi 等 set piece 合入 level |
| `scripts/worldgen_main.lua` | `CheckMapSaveData` | 校验 `savedata.map` 与 `savedata.ents` |
| `scripts/prefabswaps.lua` | `SelectPrefabSwaps` | 在生成前按 location 与 overrides 选择 prefab 替换 |
| `scripts/worldentities.lua` | `AddWorldEntities` | 在 map 校验后注入世界级实体 |
| `scripts/map/forest_map.lua` | `Generate` | 调用 storygen、WorldSim 烘焙、实体落点和编码 |
| `scripts/map/storygen.lua` | `BuildStory` | 创建 `Story` 并执行 `GenerationPipeline` |

### `0x51012111` 入口文件 / `scripts/worldgen_main.lua` / 搜索信号

`LoadParametersAndGenerate` 只做参数入口。
真正的生成阶段从 `GenerateNew` 开始。

### `0x51012112` 入口文件 / `scripts/worldgen_main.lua` / 代码事实

`GenerateNew` 用 `Level(world_gen_data.level_data)` 包装 preset。
随后按顺序执行 `level:ChooseTasks()`、`AddSetPeices(level)`、`level:ChooseSetPieces()`。
最后把 `level:GetTasksForLevel()` 的结果交给 `forest_map.Generate`。
在 task 选择前，`PrefabSwaps.SelectPrefabSwaps(prefab, level.overrides)` 会先按地图 prefab 和 overrides 处理 prefab 替换。

### `0x51012211` 地图实现文件 / `scripts/map/forest_map.lua` / 搜索信号

`Generate` 先把 `level.overrides` 转为 `story_gen_params`。
随后 `require("map/storygen")` 并调用 `BuildStory(tasks, story_gen_params, level)`。

### `0x51012212` 地图实现文件 / `scripts/map/forest_map.lua` / 代码事实

`forest_map.Generate` 是 worldgen 的重工作区。
它负责 `WorldSim:SetWorldSize`、`WorldSim:WorldGen_Commit`、`WorldSim:ConvertToTileMap`、实体放置、海洋生成和 `WorldSim:GetEncodedMap`。
本页只追踪它的调用边界。
内部输出结构进入 `0x5104-forest-map-output.md`。

## `0x51013000` 运行流程

~~~mermaid
flowchart TD
    A["宿主提供 GEN_PARAMETERS"]
    A --> B["worldgen_main.LoadParametersAndGenerate"]
    B --> C["worldgen_main.GenerateNew"]
    C --> D["Level(world_gen_data.level_data)"]
    D --> E["level:ChooseTasks"]
    E --> F["AddSetPeices + level:ChooseSetPieces"]
    F --> G["forest_map.Generate"]
    G --> H["storygen.BuildStory"]
    H --> I["WorldSim 烘焙地形与拓扑"]
    I --> J["PopulateVoronoi / object_layout / ocean"]
    J --> K["savedata.map + savedata.ents"]
    K --> L["CheckMapSaveData"]
    L --> M["worldentities.AddWorldEntities"]
~~~

### `0x51013111` 参数到 `Level` / `LoadParametersAndGenerate` / 输入字段

`GEN_PARAMETERS` 必须能解码出 `world_gen_data`。
`world_gen_data.level_data` 不能为空。
`world_gen_data.level_type` 会被写进 `savedata.map.topology.level_type`。

### `0x51013112` 参数到 `Level` / `LoadParametersAndGenerate` / 失败条件

`GEN_PARAMETERS == nil` 会触发断言。
`level.location == nil` 也会触发断言，因为地图 prefab 来自 `Level.location`。

### `0x51013211` `Level` 到 `forest_map.Generate` / `GenerateNew` / 顺序约束

`ChooseSetPieces` 必须在 `ChooseTasks` 之后运行。
源码中 `Level:ChooseSetPieces` 对 `self.chosen_tasks` 做了断言。

### `0x51013212` `Level` 到 `forest_map.Generate` / `GenerateNew` / 重试边界

`forest_map.Generate` 返回 `nil` 时，`GenerateNew` 最多重试 5 次。
每次失败后会 `collectgarbage("collect")` 并调用 `WorldSim:ResetAll()`。

### `0x51013311` `forest_map.Generate` 到 `savedata` / `WorldSim` 烘焙阶段 / 核心调用

`BuildStory` 生成拓扑树。
`WorldSim:WorldGen_Commit` 提交 worldgen。
`WorldSim:ConvertToTileMap` 把模拟结果转成 tile map。
`topology_save.root:SaveEncode` 写出 topology。

### `0x51013312` `forest_map.Generate` 到 `savedata` / `WorldSim` 烘焙阶段 / 实体写入点

`forest_map.Generate` 创建本地 `entities` 表。
房间内容、maze layout、ocean population 和 prefab density 最终都向这个表追加记录。
函数末尾把它赋给 `save.ents`。

## `0x51014111` 结构细节 / 数据对象生命周期 / `world_gen_data` / 保留字段

`level_data` 用来构造 `Level`。
`level_type` 用于日志、`forest_map.Generate` 参数和最终 topology。
`show_debug` 会影响是否调用 `ShowDebug(savedata)`。

## `0x51014121` 结构细节 / 数据对象生命周期 / `savedata` / 最小结构

`CheckMapSaveData` 要求 `savedata.map` 存在。
它还要求 `map.prefab`、`map.tiles`、`map.width`、`map.height`、`map.topology` 和 `ents` 存在。

## `0x51014122` 结构细节 / 数据对象生命周期 / `savedata` / 元信息写入

`GenerateNew` 在校验前写入 `savedata.meta`。
其中包含 build 信息、`SEED`、`level.id`、`WorldSim:GenerateSessionIdentifier()` 和 save version。

## `0x51014131` 结构细节 / 数据对象生命周期 / `data` / 返回结构

`GenerateNew` 不直接返回完整 Lua table 形式的 `savedata`。
它先临时取出 `savedata.ents`。
然后用 `DataDumper` 分别序列化 `savedata` 的顶层字段和每类实体数组。
返回值是可由宿主侧接收的 dumped data table。

## `0x51014211` 结构细节 / Set Piece 选择边界 / `AddSetPeices` / 数据来源

`AddSetPeices` 会读取 `level.overrides` 中的 `boons`、`touchstone`、`traps`、`poi` 和 `protected`。
它通过 `AddSingleSetPeice` 从 `map/traps`、`map/pointsofinterest`、`map/protected_resources` 和 `map/boons` 选择布局名。

## `0x51014212` 结构细节 / Set Piece 选择边界 / `AddSetPeices` / 拼写注意

源码函数名是 `AddSetPeices` 和 `AddSingleSetPeice`。
文档保留源码拼写，不在正文中修正为 `Pieces`。

## `0x51015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "LoadParametersAndGenerate|GenerateNew|AddSetPeices|CheckMapSaveData|BuildStory|function Generate" \
  scripts/worldgen_main.lua \
  scripts/map/forest_map.lua \
  scripts/map/storygen.lua
~~~

### `0x51015111` 推荐顺序 / 最小闭环

先读 `LoadParametersAndGenerate` 到 `GenerateNew`。
再跳到 `forest_map.Generate` 的 `BuildStory`、`WorldGen_Commit`、`PopulateVoronoi` 和 `GetEncodedMap`。
最后回到 `GenerateNew` 看 `CheckMapSaveData` 与 `worldentities.AddWorldEntities`。

### `0x51015112` 推荐顺序 / 人工核对清单

- `Level(world_gen_data.level_data)` 是否只依赖 `level_data`。
- `level:ChooseTasks()` 是否先于 set piece 分配。
- `forest_map.Generate` 的返回值是否是 `savedata` 结构，而不是已经 Spawn 的实体。
- `worldentities.AddWorldEntities(savedata)` 是否发生在 `CheckMapSaveData(savedata)` 之后。
- `GenerateNew` 的最终返回值是否已经过 `DataDumper` 序列化。
