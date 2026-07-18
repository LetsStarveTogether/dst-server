# `0x51040000` Forest Map Output

本页解释 `scripts/map/forest_map.lua` 如何把 story graph 变成可保存的地图输出。

它是 `0x5101` 入口页和 `0x5103` layout 页之间的输出层。

## `0x51041111` 本页定位 / 要回答的运行时问题 / `forest_map.Generate` 到底产出什么 / 验证点

`forest_map.Generate` 返回的是 `save` table。

`save.map` 包含 tile 编码、topology、roads、尺寸、overrides、generated densities 和 `world_tile_map`。

`save.ents` 包含 worldgen 阶段预放置的实体坐标和保存数据。

`save.world_network.persistdata` 会写入起始季节与天气相关组件数据。

## `0x51041112` 本页定位 / 要回答的运行时问题 / `forest_map.Generate` 到底产出什么 / 边界条件

`forest_map.Generate` 不负责把实体 Spawn 到运行时世界。

它只负责生成存档输入。

运行时实体创建属于存档加载路径。

## `0x51042000` 源码锚点

| 文件 | 入口 | 作用 |
| --- | --- | --- |
| `scripts/map/forest_map.lua` | `Generate` | 生成 topology、tile map、entities 和 save map |
| `scripts/map/forest_map.lua` | `TranslateWorldGenChoices` | 把 worldgen overrides 转成 prefab density 和 runtime overrides |
| `scripts/map/forest_map.lua` | `ValidateGroundTile` | 把 noise tile 和非 land tile 规整为可用地面 |
| `scripts/map/storygen.lua` | `BuildStory` | 返回 topology save 与 `Story` 实例 |
| `scripts/map/graphnode.lua` | `Node:ConvertGround` | 执行 static layout 放置 |
| `scripts/map/graphnode.lua` | `Node:PopulateVoronoi` | 执行 density 型 prefab 分布 |
| `scripts/map/ocean_gen.lua` | `PopulateOcean` | 处理 ocean population |
| `scripts/map/ocean_gen_config.lua` | module table | 提供海洋预填 set piece 参数 |
| `scripts/map/bunch_spawner.lua` | `BunchSpawnerInit` / `BunchSpawnerRun` | 处理 bunch spawner |
| `scripts/map/archive_worldgen.lua` | `AncientArchivePass` | 处理 archive worldgen pass |
| `scripts/tiledefs.lua` | `TileManager.AddTile` | 注册 `WORLD_TILES` 与地表、小地图、turf 属性 |
| `scripts/worldtiledefs.lua` | `GetTileInfo` | 提供地面属性缓存和运行时查询 |
| `scripts/tilegroups.lua` | `TileGroupManager` 扩展 | 提供 land、ocean、impassable、noise 等 tile 分组判断 |

## `0x51043000` 运行流程

~~~mermaid
flowchart TD
    A["forest_map.Generate"]
    A --> B["复制 level.overrides"]
    B --> C["story_gen_params"]
    C --> D["BuildStory(tasks, story_gen_params, level)"]
    D --> E["WorldSim 初始化与 Voronoi"]
    E --> F["storygen:AddRegionsToMainland"]
    F --> G["WorldGen_Commit + ConvertToTileMap"]
    G --> H["SaveEncode topology"]
    H --> I["ConvertGround + static layouts"]
    I --> J["PopulateVoronoi + prefab densities"]
    J --> K["Ocean + bunch + archive passes"]
    K --> L["required_prefabs 校验"]
    L --> M["GetEncodedMap + world_tile_map"]
    M --> N["season/weather persistdata + roads"]
    N --> O["return save"]
~~~

### `0x51043111` 参数规整阶段 / `current_gen_params` / 输入来源

`Generate` 断言 `level.overrides` 存在。

它把 `level.overrides` 深拷贝为 `current_gen_params`。

然后把 `start_location`、`islands`、`branching`、`loop`、`layout_mode`、`has_ocean`、`world_size` 等字段转换进 `story_gen_params`。

### `0x51043112` 参数规整阶段 / `current_gen_params` / 生成尺寸

`world_size` 会被转换成 `min_size`。

非 PS4 平台的默认值和 large 都是 `425`。

`tiny` 是 `1`。

最终 `map_width` 和 `map_height` 都被设置为 `min_size`。

### `0x51043211` Story 与 WorldSim 阶段 / `BuildStory` / 返回值

`BuildStory(tasks, story_gen_params, level)` 返回 `topology_save` 和 `storygen`。

`topology_save.root` 是后续 `SaveEncode`、`ConvertGround`、`PopulateVoronoi` 和 required prefab 汇总的入口。

### `0x51043212` Story 与 WorldSim 阶段 / `BuildStory` / WorldSim 提交

`WorldSim:WorldGen_InitializeNodePoints()` 先初始化节点点位。

`WorldSim:WorldGen_VoronoiPass(100)` 先跑初始 Voronoi。

添加区域时回调会继续 `WorldGen_AddNewPositions` 和 `WorldGen_VoronoiPass(50)`。

`WorldSim:WorldGen_Commit()` 失败时 `Generate` 返回 nil。

`GenerateNew` 会在外层捕获 nil 并最多重试 5 次。

### `0x51043311` 地形与拓扑编码 / Topology Save / 编码字段

`topology_save.root:SaveEncode({width=map_width, height=map_height}, save.map.topology)` 写出 topology。

`WorldSim:CreateNodeIdTileMap(save.map.topology.ids)` 创建 node id tile map。

`WorldSim:GetEncodedMap(join_islands)` 写出 `tiles`、`tiledata`、`nav`、`adj` 和 `nodeidtilemap`。

### `0x51043312` 地形与拓扑编码 / Topology Save / Tile 注册依赖

`save.map.world_tile_map = GetWorldTileMap()` 依赖 tile 注册结果。

`tiledefs.lua` 通过 `TileManager.RegisterTileRange` 注册 LAND、NOISE、OCEAN 和 IMPASSABLE 范围。

`tiledefs.lua` 有 78 个 `TileManager.AddTile` 调用。

`worldtiledefs.lua` 负责 ground property cache、资产表、footstep 查询和 `GetTileInfo`。

`tilegroups.lua` 提供 `IsLandTile`、`IsOceanTile`、`IsImpassableTile`、`IsNoiseTile` 和 `IsShallowOceanTile` 等判断。

### `0x51043411` 实体填充阶段 / Static Layout / 执行路径

`topology_save.root:GlobalPrePopulate` 先执行全局预填充。

`topology_save.root:ConvertGround` 会进入 `Node:ConvertGround`。

`Node:ConvertGround` 处理 `countstaticlayouts` 和 `terrain_contents_extra.static_layouts`。

这些 layout 最终通过 `object_layout.Convert` 写入 `entities`。

### `0x51043421` 实体填充阶段 / Density Prefab / 执行路径

`TranslateWorldGenChoices(current_gen_params)` 会把部分 override 转成 `translated_prefabs`。

`topology_save.root:PopulateVoronoi` 会按 room contents 的 `distributeprefabs` 和 `countprefabs` 写实体。

`save.map.generated.densities` 同时记录生成密度。

如果 `translated_prefabs` 中某个 prefab 的倍率小于 `1`，后续还会裁剪已生成实体。

### `0x51043431` 实体填充阶段 / Ocean Pass / 执行路径

`story_gen_params.has_ocean` 为真时才进入海洋流程。

流程依次调用 `Ocean_SetWorldForOceanGen`、`Ocean_PlaceSetPieces`、`Ocean_ConvertImpassibleToWater`、
`PopulateOcean` 和 `MonkeyIsland_GenerateDocks`。

`storygen.ocean_population` 来自 `Story:ProcessOceanContent`。

### `0x51043441` 实体填充阶段 / 后处理 Pass / 执行路径

`BunchSpawnerInit` 和 `BunchSpawnerRun` 在主要实体填充后运行。

`AncientArchivePass` 在 bunch pass 之后运行。

`topology_save.root:GlobalPostPopulate` 在 ocean pass 之后运行。

`forest_map.Generate` 还会移除落在 impassable visual tile 上的实体。

### `0x51043511` 校验与最终字段 / Required Prefabs / 校验来源

required prefab 校验来自三个来源。

第一个来源是 `level.required_prefabs`。

第二个来源是 `topology_save.root:GetRequiredPrefabs()`。

第三个来源是 `storygen.ocean_population` 中 room data 的 `required_prefabs`。

如果 required prefab 被 worldgen override 设为 `never`，缺失时只打印 disabled 提示。

否则缺失会让 `Generate` 返回 nil。

### `0x51043521` 校验与最终字段 / Start Location / 校验方式

`save.ents` 必须至少包含 `spawnpoint_multiplayer`、`multiplayer_portal`、`quagmire_portal` 或
`lavaarena_portal` 中的一类。

否则 `Generate` 会打印 `PANIC: No start location!`。

在非跳过校验时它会返回 nil。

### `0x51043531` 校验与最终字段 / Season 与 Roads / 写入位置

`season_start` 会被解析为起始季节。

`SEASONS[start_season](start_season)` 生成 `seasons` 和可选 `weather`。

这些数据写入 `save.world_network.persistdata`。

道路数据来自 `WorldSim:GetRoad`。

它写入 `save.map.roads`。

## `0x51044100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "local function Generate|BuildStory|WorldGen_Commit|ConvertGround|PopulateVoronoi" \
  scripts/map/forest_map.lua
rg -n "PopulateOcean|GetEncodedMap|world_tile_map|No start location" \
  scripts/map/forest_map.lua

rg -n "TileManager\\.AddTile|RegisterTileRange|GetTileInfo|IsLandTile|IsOceanTile|IsImpassableTile" \
  scripts/tiledefs.lua \
  scripts/worldtiledefs.lua \
  scripts/tilemanager.lua \
  scripts/tilegroups.lua
~~~

### `0x51044111` 人工核对清单 / 最小闭环

- `Generate` 是否从 `level.overrides` 派生 `story_gen_params`。
- `BuildStory` 是否早于 `WorldGen_Commit`。
- `ConvertGround` 是否早于 `PopulateVoronoi`。
- ocean pass 是否受 `story_gen_params.has_ocean` 控制。
- required prefab 校验是否早于 `save.ents = entities`。
- `GetEncodedMap` 是否写出 tile、nav、adj 和 node id tile map。
- `save.world_network.persistdata` 是否由起始季节写入。
