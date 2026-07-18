# `0x33010000` Prefab 装配契约

Prefab 装配契约解释 `Prefab(name, fn, assets, deps)` 如何变成可生成实体。

本页只覆盖运行机制、代表性样例和人工核对路线。

完整 prefab 文件清单由 `0x8000-reference/0x8200-runtime-catalogs/0x8201-prefab-catalog.md` 承担。

## `0x33011111` 本页定位 / 要回答的运行时问题 / 声明对象是什么 / 验证点

`Prefab` 对象不是实体。

它是注册给模拟层的装配描述。

它保存名字、实体构造函数、资产列表、生成依赖和资产解析策略。

真正的实体在 `SpawnPrefabFromSim` 执行 `prefab.fn(TheSim)` 时才出现。

## `0x33011121` 本页定位 / 要回答的运行时问题 / `fn` 的分界在哪里 / 验证点

大多数网络 prefab 的 `fn` 先创建实体、网络、动画、标签和客户端可读 netvars。

随后调用 `inst.entity:SetPristine()`。

`if not TheWorld.ismastersim then return inst` 之后才进入 server 权威组件、SG、Brain、存档和掉落逻辑。

这个分界决定客户端能直接展示什么，以及哪些状态必须通过 replica 或 classified 投影。

## `0x33011131` 本页定位 / 要回答的运行时问题 / Helper 文件承担什么 / 验证点

`scripts/prefabutil.lua` 可以返回 prefab 工厂。

`scripts/standardcomponents.lua` 主要提供 physics、burnable、freezable、hauntable 等装配 helper。

它们会被 prefab 文件调用，但不是 `Prefabs` 注册表本身。

## `0x33012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/prefabs.lua` | `Prefab` | 归一化 prefab 名、保存 `fn`/`assets`/`deps`、追加皮肤依赖 |
| `scripts/mainfunctions.lua` | `LoadPrefabFile` | 执行 prefab 文件 chunk，并收集其中返回的 `Prefab` 对象 |
| `scripts/mainfunctions.lua` | `RegisterPrefabsImpl` | 解析 asset 路径、记录 mod post-init、写入 `Prefabs` 并调用模拟层注册 |
| `scripts/mainfunctions.lua` | `SpawnPrefabFromSim` | 由模拟层回调 Lua，执行 `prefab.fn` 并发送 `entity_spawned` |
| `scripts/prefabutil.lua` | `MakePlacer`、`MakeDeployableKitItem` | 以 helper 形式生成常见 prefab |
| `scripts/standardcomponents.lua` | `MakeInventoryPhysics`、`MakeSnowCovered` | 给具体 prefab 复用标准组件和视觉状态 |
| `scripts/prefabs/rabbit.lua` | `fn` | 普通生物的 pristine/client return/server component 样例 |
| `scripts/prefabs/player_common.lua` | `MakePlayerCharacter` | 玩家 prefab 的资产合并、prereplica 标签和 classified 装配样例 |

### `0x33012111` 注册锚点 / `scripts/prefabs.lua` / 搜索信号

`Prefab` 构造器会去掉路径前缀，只保留最后一段作为 `self.name`。

它保存 `fn`、`assets`、`deps` 和 `force_path_search`。

如果 `PREFAB_SKINS[self.name]` 存在，构造器会把皮肤 prefab 追加进 `deps`。

因此 `deps` 不是手写列表的简单回显。

### `0x33012121` 注册锚点 / `scripts/mainfunctions.lua` / 搜索信号

`LoadPrefabFile` 使用 `loadfile(filename)` 得到 chunk。

它以 `{fn()}` 接收 prefab 文件返回的所有值。

只有 `type(val) == "table"` 且 `val:is_a(Prefab)` 的返回值会被注册。

`RegisterPrefabsImpl` 会解析 `asset.file`，保存 `PrefabPostInit`，写入 `Prefabs[prefab.name]`。

最后它调用 `TheSim:RegisterPrefab(prefab.name, prefab.assets, prefab.deps)`。

### `0x33012211` 生成锚点 / `SpawnPrefab` 到 `SpawnPrefabFromSim` / 搜索信号

Lua 侧 `SpawnPrefab(name, skin, skin_id, creator, skin_custom)` 会调用 `TheSim:SpawnPrefab`。

模拟层随后通过 `SpawnPrefabFromSim(name)` 回到 Lua。

`SpawnPrefabFromSim` 查 `Prefabs[name]`，执行 `prefab.fn(TheSim)`，再调用 `inst:SetPrefabName`。

`PrefabPostInit` 和 `PrefabPostInitAny` 在 `fn` 返回实体之后运行。

最后 `TheWorld:PushEvent("entity_spawned", inst)` 通知世界。

这意味着 mod post-init 不属于原始 prefab 文件的装配正文，但会改变最终实体。

## `0x33013000` 装配流程

~~~mermaid
flowchart TD
    A["prefab 文件返回 Prefab(...)"]
    A --> B["LoadPrefabFile 执行 chunk"]
    B --> C["RegisterPrefabsImpl"]
    C --> D["Prefabs[name] + TheSim:RegisterPrefab"]
    D --> E["SpawnPrefab -> TheSim:SpawnPrefab"]
    E --> F["SpawnPrefabFromSim -> prefab.fn(TheSim)"]
    F --> G["CreateEntity + AddNetwork + tags + netvars"]
    G --> H["entity:SetPristine"]
    H --> I{"TheWorld.ismastersim"}
    I -->|false| J["client 返回 pristine 实体"]
    I -->|true| K["server AddComponent/SetStateGraph/SetBrain"]
    K --> L["PrefabPostInit + entity_spawned"]
~~~

### `0x33013111` 声明阶段 / `Prefab(name, fn, assets, deps)` / 边界条件

`assets` 是注册阶段交给模拟层解析的资源声明。

`deps` 是生成前需要认识的 prefab 名称集合。

`fn` 是实体装配中心。

`force_path_search` 和 `search_asset_first_path` 只影响资产解析，不改变实体生命周期。

### `0x33013121` 声明阶段 / Prefab 文件返回值 / 边界条件

一个 prefab 文件可以返回多个值。

只有真实 `Prefab` 对象会进入注册路径。

helper table、局部函数和普通配置表不会被自动注册。

### `0x33013211` 生成阶段 / Pristine 前 / 边界条件

pristine 前适合放网络存在性、基础标签、动画 bank/build、inventory image override 和客户端可判断状态。

如果状态需要初始同步给远端客户端，它必须在 `SetPristine` 前以标签、netvar 或网络组件形式存在。

### `0x33013221` 生成阶段 / Pristine 后 / 边界条件

`SetPristine` 后的 master sim 分支适合放 server component、SG、Brain、保存/加载函数和权威事件监听。

client 分支返回后不能依赖 server-only component。

如果文档把 client 可见状态写成 server component 直接可读，应视为不一致。

## `0x33014111` 代表性样例 / 普通生物 / `scripts/prefabs/rabbit.lua` / 需要核对的字段

`rabbit` 的 `fn` 先添加 `Transform`、`AnimState`、`SoundEmitter`、`DynamicShadow` 和 `Network`。

它在 pristine 前添加动物标签、可烹饪标签、客户端 build override 和 inventory image override。

`MakeFeedableSmallLivestockPristine(inst)` 也在 pristine 前执行。

`SetPristine` 后，远端客户端直接返回。

master sim 分支才添加 `locomotor`、`drownable`、`eater`、`inventoryitem`、`health` 和 `combat`。

注释明确说明 `locomotor` 必须在 `SetStateGraph("SGrabbit")` 前构造。

## `0x33014211` 代表性样例 / 玩家 Prefab / `scripts/prefabs/player_common.lua` / 需要核对的字段

`MakePlayerCharacter` 合并基础玩家资产、基础 deps、`customassets` 和 `customprefabs`。

基础 deps 明确包含 `player_classified`、`inventory_classified`、`wonkey` 和 `spellbookcooldown`。

`SetInstanceFunctions` 把 `AttachClassified`、`DetachClassified` 和 `OnRemoveEntity` 挂到实例上。

这些函数需要 client 也可见，因为 classified 绑定会在 client 侧发生。

玩家在 pristine 前手工加入 `_health`、`_hunger`、`_sanity`、`_builder`、`_combat` 等 prereplica 标签。

进入 master sim 后，这些标签会被移除，再通过真实组件添加重新复制。

随后 master sim 生成 `player_classified`，并把它设为玩家实体的 child。

## `0x33014311` 代表性样例 / Helper 工厂 / `scripts/prefabutil.lua` / 需要核对的字段

`MakePlacer` 返回 `Prefab(name, fn)`，但它创建的是非网络 placement helper。

`MakeDeployableKitItem` 返回 `Prefab(name, function(inst) ... end, assets)` 风格的物品工厂。

这个工厂仍然遵守 `AddNetwork`、`SetPristine`、client return、master sim component 的分界。

## `0x33014321` 代表性样例 / Helper 工厂 / `scripts/standardcomponents.lua` / 需要核对的字段

`MakeInventoryPhysics`、`MakeCharacterPhysics` 等 helper 直接修改传入实体。

`MakeSnowCoveredPristine` 只设置 pristine 可见的动画 symbol 和标签。

`MakeSnowCovered` 在 master sim 且实体有 `Network` 时可能接入 `MakeLunarHailBuildup`。

这类 helper 要按调用位置判断 client/server 可见性，不能只按函数所在文件判断。

`MakeGolfObstaclePhysics(inst, rad, golfradoverride)` 接受高尔夫碰撞半径覆盖参数。

`GolfObstacle_OnEntityWake` 会用该覆盖值创建 boat limits 碰撞辅助实体。

`MakeFumaroleTool` 会设置 `inventoryitemtemperature.inherentinsulation`。
它也会监听 `temperaturedelta`，并按外部热源强度调整温度 modifier。

## `0x33015111` 覆盖口径 / 不在本页展开的内容 / 完整清单 / 边界条件

`scripts/prefabs/` 下的完整 Lua 文件覆盖在 `0x8201-prefab-catalog.md` 中校验。

本页不复制完整清单。

重复清单会让文档与 `git ls-files` 产生双重维护风险。

## `0x33015121` 覆盖口径 / 不在本页展开的内容 / 大型 Prefab 内部逻辑 / 边界条件

具体角色、boss、物品、FX 的内部 AI、掉落、技能和视觉逻辑进入对应专题。

本页只问它们如何成为 prefab 实体。

## `0x33016100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "Prefab = Class|LoadPrefabFile|RegisterPrefabsImpl|SpawnPrefabFromSim|function SpawnPrefab\\b" \
  scripts/prefabs.lua \
  scripts/mainfunctions.lua

rg -n "SetPristine|TheWorld\\.ismastersim|SetStateGraph|SetBrain|return Prefab" \
  scripts/prefabs/rabbit.lua \
  scripts/prefabs/player_common.lua \
  scripts/prefabutil.lua

rg -n "MakeGolfObstaclePhysics|GolfObstacle_OnEntityWake|MakeFumaroleTool|fumarole_UpdateTemperatureModifier" \
  scripts/standardcomponents.lua
~~~

### `0x33016111` 推荐顺序 / 最小闭环

先追 `scripts/prefabs.lua` 的 `Prefab` 构造器。

再追 `LoadPrefabFile` 到 `RegisterPrefabsImpl`。

然后用 `rabbit` 验证普通 prefab 的三段式装配。

最后用 `MakePlayerCharacter` 验证玩家 prereplica 标签和 classified 依赖。

### `0x33016121` 常见误读 / 验证点

不要把 `assets` 看成实体字段。

不要把 `deps` 看成组件列表。

不要把 `PrefabPostInit` 混进原始 prefab 文件正文。

不要假设所有 prefab 都有 SG 或 Brain。

不要把 `standardcomponents.lua` 当成注册表。
