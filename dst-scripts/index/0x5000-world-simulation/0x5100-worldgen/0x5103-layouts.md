# `0x51030000` Layout 与 Set Piece

Layout 与 Set Piece 页解释静态布局如何从 worldgen 选择表进入 `savedata.ents`。
它区分 graph layout、static layout 转换、set piece 分配和 object layout 放置。

## `0x51031111` 本页定位 / 要回答的运行时问题 / Layout 在 Worldgen 中什么时候生效 / 验证点

`worldgen_main.lua` 的 `AddSetPeices` 只把布局名写入 `level.set_pieces`。
`storygen.lua` 把布局名写到节点的 `countstaticlayouts`。
`graphnode.lua` 的 `Node:ConvertGround` 才调用 `object_layout.Convert`。
真正的坐标预留和实体写入发生在 `object_layout.ReserveAndPlaceLayout`。

## `0x51031112` 本页定位 / 要回答的运行时问题 / Layout 在 Worldgen 中什么时候生效 / 边界条件

`map/layout.lua` 负责 graph node 的 force-directed 空间布局。
`map/static_layout.lua` 把 Tiled 静态布局模块转换成 layout table。
`map/object_layout.lua` 才负责把 layout table 放到地图节点或具体坐标。

## `0x51032000` 源码锚点

| 文件 | 入口 | 作用 |
| --- | --- | --- |
| `scripts/map/layout.lua` | `layout.run` | graph node 空间布局算法 |
| `scripts/map/static_layout.lua` | `Get` | 把 Tiled 静态布局转换成 layout table |
| `scripts/map/layouts.lua` | `Layouts` | 注册通用 static layout |
| `scripts/map/traps.lua` | `Layouts` / `Sandbox` | 注册 trap layout 与随机选择表 |
| `scripts/map/pointsofinterest.lua` | `Layouts` / `Sandbox` | 注册 POI layout 与随机选择表 |
| `scripts/map/protected_resources.lua` | `Layouts` / `Sandbox` | 注册 protected resource layout 与随机选择表 |
| `scripts/map/boons.lua` | `Layouts` / `Sandbox` | 注册 boon layout 与随机选择表 |
| `scripts/map/maze_layouts.lua` | `AllLayouts` | 注册 maze room layout |
| `scripts/worldgen_main.lua` | `AddSingleSetPeice` | 从 choice file 抽取 layout 名 |
| `scripts/map/level.lua` | `Level:ChooseSetPieces` | 把 layout 名分配给 task |
| `scripts/map/storygen.lua` | `Story:InsertAdditionalSetPieces` | 写入节点 `countstaticlayouts` |
| `scripts/map/graphnode.lua` | `Node:ConvertGround` | 执行 `object_layout.Convert` |
| `scripts/map/object_layout.lua` | `ReserveAndPlaceLayout` | 预留空间并写入 `entities` |

### `0x51032111` Graph Layout / `scripts/map/layout.lua` / 搜索信号

搜索 `RunForceDirected`、`ForceDirected` 和 `layout = {run=`。
这些函数处理节点之间的空间排布，而不是 prefab 列表。

### `0x51032112` Graph Layout / `scripts/map/layout.lua` / 代码事实

`map/layout.lua` 返回全局 `layout` 表。
它服务于拓扑节点位置计算。
它不读取 `map/layouts.lua` 的 `Layouts` 表，也不调用 `ReserveAndPlaceLayout`。

### `0x51032211` Static Layout / `scripts/map/static_layout.lua` / 搜索信号

搜索 `ConvertStaticLayoutToLayout` 和 `return { Get =`。
这条路径把 `map/static_layouts/...` 模块变成 object layout 可解释的 table。

### `0x51032212` Static Layout / `scripts/map/static_layout.lua` / 代码事实

`ConvertStaticLayoutToLayout` 会读取 tile layer 与 object group。
它生成 `layout.type = LAYOUT.STATIC`、`ground_types`、`ground` 和 `layout.layout`。
`map/layouts.lua` 大量通过 `StaticLayout.Get("map/static_layouts/...")` 注册这些布局。
`GROUND_TYPES` 表把 Tiled tile index 转成 `WORLD_TILES`。
因此静态布局的地皮语义还依赖 `tiledefs.lua` 和 `worldtiledefs.lua` 完成的 tile 注册。

### `0x51032311` Object Layout / `scripts/map/object_layout.lua` / 搜索信号

搜索 `LayoutForDefinition`、`ConvertLayoutToEntitylist`、`ReserveAndPlaceLayout`、`Convert` 和 `Place`。
这条链路才把 layout 名变成实体坐标。

### `0x51032312` Object Layout / `scripts/map/object_layout.lua` / 代码事实

`LayoutForDefinition` 会按顺序检查多个 layout 来源。
包括 `map/layouts`、`map/traps`、`map/protected_resources`、`map/boons`、`map/maze_layouts` 和 `map/pointsofinterest`。
`ConvertLayoutToEntitylist` 展开 `layout.layout`、`layout.areas`、`layout.defs` 和 `layout.count`。
`ReserveAndPlaceLayout` 调用 `WorldSim:ReserveSpace` 或使用传入 position。
随后它通过 `add_entity.fn` 写入 `entities`。
如果布局带 `ground`，`ReserveAndPlaceLayout` 会先准备 tile 数据。
有显式 position 时它会直接调用 `world:SetTile`。
没有显式 position 时它会把 tile 数据交给 `WorldSim:ReserveSpace`。

## `0x51033000` 运行流程

~~~mermaid
flowchart TD
    A["worldgen_main.AddSingleSetPeice"]
    A --> B["GetRandomFromLayouts"]
    B --> C["level.set_pieces[name]"]
    C --> D["Level:ChooseSetPieces"]
    D --> E["task.set_pieces / task.random_set_pieces"]
    E --> F["Story:InsertAdditionalSetPieces"]
    F --> G["node.data.terrain_contents.countstaticlayouts"]
    G --> H["forest_map.Generate"]
    H --> I["topology_save.root:ConvertGround"]
    I --> J["Node:ConvertGround"]
    J --> K["object_layout.Convert"]
    K --> L["ReserveAndPlaceLayout"]
    L --> M["savedata.ents"]
~~~

### `0x51033111` Set Piece 选择阶段 / `worldgen_main.GetRandomFromLayouts` / 选择来源

`AddSingleSetPeice` 会 `require(choicefile)`。
`choicefile` 必须提供 `Sandbox` 表。
`GetRandomFromLayouts` 从 `Sandbox` 的 area 中选一个 layout 名。

### `0x51033112` Set Piece 选择阶段 / `worldgen_main.GetRandomFromLayouts` / Area 过滤

`GetAreasForChoice` 会把选择出的 area 和 task 的 `room_bg` 对齐。
`Any` 和 `Rare` 可以跳过具体地皮匹配。

### `0x51033211` Task 分配阶段 / `Level:ChooseSetPieces` / 写入字段

普通选择结果进入 `task.set_pieces`。
`required_setpieces` 和随机抽取结果进入 `task.random_set_pieces`。

### `0x51033212` Task 分配阶段 / `Level:ChooseSetPieces` / 数量边界

源码注释说明每个 task 只放一个 level set piece。
循环会在 `count` 归零或可选 task 用完时停止。

### `0x51033311` Storygen 标记阶段 / `Story:InsertAdditionalSetPieces` / 节点写入

对于 `task.set_pieces`，函数会挑选非入口、非背景、非空白的节点。
它把 layout 名写入 `task.nodes[choice].data.terrain_contents.countstaticlayouts[name] = 1`。

### `0x51033312` Storygen 标记阶段 / `Story:InsertAdditionalSetPieces` / 随机 Set Piece

对于 `task.random_set_pieces`，函数会在候选节点中随机选择。
命中后同样写入 `countstaticlayouts`。

### `0x51033411` 实体放置阶段 / `Node:ConvertGround` / `countstaticlayouts`

`forest_map.Generate` 调用 `topology_save.root:ConvertGround`。
`Node:ConvertGround` 遍历 `self.data.terrain_contents.countstaticlayouts`。
每个 layout 名都会调用 `obj_layout.Convert(self.id, k, add_fn)`。

### `0x51033412` 实体放置阶段 / `Node:ConvertGround` / `static_layouts`

`Node:ConvertGround` 还会处理 `terrain_contents_extra.static_layouts`。
这部分来自 `Story:GetExtrasForRoom` 对 room tags 的转换。

### `0x51033421` 实体放置阶段 / `object_layout.ReserveAndPlaceLayout` / `add_fn`

`Node:ConvertGround` 创建的 `add_fn` 最终会调用 `PopulateWorld_AddEntity`。
maze 和 ocean 等定位流程则会传入 `forest_map.Generate` 中定义的 `add_fn`。
两者都会把 prefab 写入 worldgen 的 `entities` 表。

### `0x51033422` 实体放置阶段 / `object_layout.ReserveAndPlaceLayout` / 运行时边界

layout 成功放置后写入的是 `savedata.ents` 的来源表。
真正运行时实体仍要等存档加载路径再 Spawn。

## `0x51034111` 结构细节 / Room Contents 与 Static Layout / `countstaticlayouts` / 读取路径

`Story:InsertAdditionalSetPieces` 会创建或更新 `terrain_contents.countstaticlayouts`。
`Node:ConvertGround` 是该字段的主要执行点。

## `0x51034121` 结构细节 / Room Contents 与 Static Layout / `distributeprefabs` / 读取路径

`Story:RunTaskSubstitution` 会改写 `contents.distributeprefabs`。
`forest_map.Generate` 后续通过 `PopulateVoronoi` 和 `ConvertGround` 处理 density 型分布。

## `0x51034211` 结构细节 / `object_layout` 数据结构 / Layout 定义字段 / 解析规则

`areas` 会把区域占位对象扩展成实际 prefab。
`defs` 会把抽象对象替换为候选 prefab。
`layout` 是静态坐标集合。
`count` 交给 `LAYOUT_FUNCTIONS[layout.type]` 生成位置。

## `0x51034221` 结构细节 / `object_layout` 数据结构 / 空间预留字段 / 解析规则

`ground`、`ground_types`、`start_mask`、`fill_mask` 和 `layout_position` 影响 `WorldSim:ReserveSpace`。
`SafeFromDisconnect` 会让对应 tile 进入 `WorldSim:MakeSafeFromDisconnect`。

## `0x51034231` 结构细节 / `object_layout` 数据结构 / 定位入口 / 解析规则

`Convert` 用于节点内自动找位置。
它把 node id、layout 名和 `add_fn` 交给 `ReserveAndPlaceLayout`。
`Place` 用于 maze、ocean 或其他已经有 tile 坐标的位置型布局。
它用 `"POSITIONED"` 作为 node id，并把 position 传给 `ReserveAndPlaceLayout`。

## `0x51034311` 结构细节 / 命名风险 / `AddSetPeices` / 校验方式

源码里函数名拼写是 `AddSetPeices` 和 `AddSingleSetPeice`。
`rg "AddSetPieces"` 找不到真实入口。
文档和查询块必须使用源码拼写。

## `0x51034321` 结构细节 / 命名风险 / `GetRandomFromLayouts` / 校验方式

`GetRandomFromLayouts` 定义在 `worldgen_main.lua`。
它不在 `storygen.lua`。
如果页面把它归到 `storygen.lua`，应视为错误。

## `0x51035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "GetRandomFromLayouts|AddSingleSetPeice|ChooseSetPieces|InsertAdditionalSetPieces" \
  scripts/worldgen_main.lua \
  scripts/map/level.lua \
  scripts/map/storygen.lua

rg -n "countstaticlayouts|Node:ConvertGround|LayoutForDefinition|ReserveAndPlaceLayout" \
  scripts/map/graphnode.lua \
  scripts/map/object_layout.lua

rg -n "ConvertStaticLayoutToLayout|StaticLayout.Get|layout = \\{run" \
  scripts/map/static_layout.lua \
  scripts/map/layouts.lua \
  scripts/map/layout.lua

rg -n "Sandbox|Layouts|AllLayouts" \
  scripts/map/traps.lua \
  scripts/map/pointsofinterest.lua \
  scripts/map/protected_resources.lua \
  scripts/map/boons.lua \
  scripts/map/maze_layouts.lua
~~~

### `0x51035111` 推荐顺序 / 最小闭环

先读 `worldgen_main.AddSingleSetPeice`，确认 layout 名如何进入 `level.set_pieces`。
再读 `Level:ChooseSetPieces`，确认它如何进入 task。
接着读 `Story:InsertAdditionalSetPieces`，确认它如何进入 `countstaticlayouts`。
最后读 `Node:ConvertGround` 到 `ReserveAndPlaceLayout`，确认实体坐标如何写入 `entities`。

### `0x51035112` 推荐顺序 / 人工核对清单

- `GetRandomFromLayouts` 是否写在 `worldgen_main.lua`。
- `map/layout.lua` 是否只负责 graph layout 算法。
- `map/static_layout.lua` 是否把 Tiled 数据转换成 layout table。
- `map/layouts.lua` 是否通过 `StaticLayout.Get` 注册大量 static layout。
- `object_layout` 是否才是 layout 名到实体坐标的执行器。
- `LayoutForDefinition` 是否覆盖 `layouts`、`traps`、`protected_resources`、`boons`、`maze_layouts` 和 `pointsofinterest`。
- static layout 是否在 worldgen 阶段写入 `savedata.ents`。
