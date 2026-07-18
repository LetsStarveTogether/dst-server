# `0x22020000` 存档与读档

存档页按世界快照、实体记录、组件持久化和 shard index 收尾阅读。

读档页按 `ShardGameIndex` 取数、升级存档、初始化世界和实体恢复阅读。

## `0x22021111` 本页定位 / 要回答的运行时问题 / 世界为什么不是直接序列化 Lua 对象 / 验证点

`SaveGame` 遍历 `Ents`，但只保存满足 `persists`、`prefab`、`Transform` 和 parent 条件的实体。

实体通过 `GetSaveRecord` 输出记录。

组件通过 `OnSave` 输出自己的持久化表。

## `0x22021211` 本页定位 / 读档的入口边界 / `ShardGameIndex` / 验证点

`gamelogic.lua` 负责决定加载已有世界还是生成新世界。

实际读取世界数据通过 `ShardGameIndex:GetSaveData()` 或 `ShardGameIndex:GetSaveDataFile()` 完成。

## `0x22022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `SaveGame` | server 侧世界保存入口 |
| `scripts/mainfunctions.lua` | `SerializeWorldSession` | 把分块后的世界数据交给 `TheNet` |
| `scripts/networking.lua` | `SerializeUserSession` | 保存玩家会话并可附带 `player_classified` |
| `scripts/entityscript.lua` | `GetSaveRecord` | 输出单个实体的保存记录 |
| `scripts/entityscript.lua` | `GetPersistData` | 调用组件 `OnSave` 收集持久化表 |
| `scripts/entityscript.lua` | `SetPersistData` | 调用组件 `OnLoad` 恢复持久化表 |
| `scripts/gamelogic.lua` | `DoLoadWorld` | 从 shard index 读取世界并初始化游戏 |
| `scripts/saveindex.lua` | `SaveIndex:Save` | 保留的索引保存入口，只调用 callback |
| `scripts/shardindex.lua` | `ShardIndex:Save` | 保存 shard 的 world、server 和 session 索引 |
| `scripts/shardsaveindex.lua` | `ShardSaveIndex:GetShardIndex` | 管理 slot 到 shard index 的缓存 |

### `0x22022111` 保存锚点 / `scripts/mainfunctions.lua` / 搜索信号

搜索 `SaveGame` 后继续看 `TheNet:StartWorldSave()` 和 `TheNet:EndWorldSave()`。

这两个调用标记世界保存过程的开始和结束。

### `0x22022211` 实体锚点 / `scripts/entityscript.lua` / 搜索信号

搜索 `GetSaveRecord` 可以看到位置、平台和 prefab 信息如何进入实体记录。

搜索 `GetPersistData` 可以看到组件 `OnSave` 如何合并到 `data[k]`。

### `0x22022311` 读档锚点 / `scripts/gamelogic.lua` / 搜索信号

搜索 `DoLoadWorld` 可以看到 `ShardGameIndex:GetSaveData(onload)`。

`onload` 会执行 `UpgradeSaveFile`、`LoadAssets` 和 `DoInitGame`。

## `0x22023000` 运行流程

~~~mermaid
flowchart TD
    A["SaveGame"]
    A --> B["filter Ents"]
    B --> C["EntityScript:GetSaveRecord"]
    C --> D["EntityScript:GetPersistData"]
    D --> E["DataDumper per save section"]
    E --> F["SerializeWorldSession"]
    F --> G["ShardGameIndex:Save"]
    G --> H["ShardGameIndex:WriteTimeFile"]
~~~

### `0x22023111` 保存阶段 / 实体和地图 / 边界条件

`SaveGame` 会保存地图编码、道路、拓扑、世界组件、`world_network` 和可选 `shard_network`。

它也会把实体引用补回到对应保存记录的 `id`。

### `0x22023211` 玩家会话阶段 / `SerializeUserSession` / 边界条件

玩家会话保存调用 `player:GetSaveRecord()`。

server 侧会把角色 prefab 写入 metadata。

`player_classified` 存在时会把其 entity 传给 `TheNet:SerializeUserSession()`。

### `0x22023311` 读档阶段 / `DoLoadWorld` / 边界条件

读档不是从 `SaveGame` 反向返回。

它从 `gamelogic.lua` 的 `LoadSlot` 进入，再通过 `ShardGameIndex` 读取存档并调用 `DoInitGame`。

## `0x22024111` 结构细节 / `GetPersistData` / 组件 `OnSave` / 需要核对的字段

组件返回非空 table 时会进入 `data[component_name]`。

组件返回 refs 时会追加到引用列表。

实体自身的 `OnSave` 可以在组件之后追加数据和引用。

## `0x22024211` 结构细节 / `SetPersistData` / 组件 `OnLoad` / 需要核对的字段

`add_component_if_missing` 会让读档路径补加缺失组件。

`OnPreLoad` 在组件 `OnLoad` 前执行。

`LoadPostPass` 是第二阶段恢复引用的入口。

`prefabs/gravestone.lua` 会在 `onloadpostpass` 中把 `savedata.mounddata.data` 交给 `inst.mound:LoadPostPass(...)`。

这类嵌套实体恢复要看 owner prefab 的 post pass，而不只看子实体自己的 `OnLoad`。

## `0x22024311` 结构细节 / `SaveIndex` / Shard 索引文件 / 需要核对的字段

`gamelogic.lua` 在读档链路里创建 `ShardGameIndex = ShardIndex()`。

`ShardIndex:Save` 通过 `TheSim:SetPersistentStringInClusterSlot` 或 `TheSim:SetPersistentString` 写出 `shardindex`。

`SaveIndex:Save` 是保留的索引保存入口，只调用 callback，不写索引文件。

世界主体数据通过 `SerializeWorldSession` 保存，保存后再更新 worldgen overrides、shard index 和时间文件。

## `0x22025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "SaveGame|GetSaveRecord|GetPersistData|SetPersistData|LoadPostPass|DoLoadWorld|ShardIndex:Save|SaveIndex:Save" \
  scripts/mainfunctions.lua \
  scripts/entityscript.lua \
  scripts/prefabs/gravestone.lua \
  scripts/gamelogic.lua \
  scripts/saveindex.lua \
  scripts/shardindex.lua \
  scripts/shardsaveindex.lua \
  scripts/networking.lua
~~~

### `0x22025111` 推荐顺序 / 最小闭环

先追 `SaveGame` 的实体循环。

再跳到 `EntityScript:GetPersistData` 看组件 `OnSave`。

最后读 `DoLoadWorld`，确认读档从 `ShardGameIndex` 回到 `DoInitGame`。
