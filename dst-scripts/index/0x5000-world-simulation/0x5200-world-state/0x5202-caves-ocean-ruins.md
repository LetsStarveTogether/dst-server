# `0x52020000` 洞穴海洋与遗迹

本页按特殊世界装配、洞穴天气、地震与梦魇时钟、地表海洋表现、遗迹生成与重置的顺序阅读。
核心边界是洞穴特殊规则集中在 `cave_network.lua` 与 `cave.lua`，海洋运行时主要在地表 `forest.lua` 和通用 `world.lua`。

## `0x52021111` 本页定位 / 运行时问题 / 洞穴与地表是不同 World Prefab / 验证点

`scripts/prefabs/forest.lua` 返回 `MakeWorld("forest", ...)`。
`scripts/prefabs/cave.lua` 返回 `MakeWorld("cave", ...)` 并传入 `cave` tag。

## `0x52021121` 本页定位 / 运行时问题 / 洞穴天气不等于地表天气 / 验证点

`scripts/prefabs/cave_network.lua` 添加 `caveweather`、`quaker` 和 `nightmareclock`。
它没有添加 `weather`。

## `0x52021131` 本页定位 / 运行时问题 / 海洋不是单个组件 / 验证点

海洋涉及 `world.lua` 的 `waterphysics` 和 `walkableplatformmanager`。
地表 `forest.lua` 再添加 `WaveComponent`、`wavemanager` 和 `oceanicemanager`。

## `0x52022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/prefabs/world.lua` | `MakeWorld` | 添加通用地图、`waterphysics`、`walkableplatformmanager` 和客户端 `oceancolor` |
| `scripts/prefabs/forest.lua` | `common_postinit` | 初始化波浪渲染、地表海洋表现和地表组件 |
| `scripts/prefabs/forest.lua` | `master_postinit` | 添加 `worldwind`、`oceanicemanager` 和地表生态管理器 |
| `scripts/prefabs/cave.lua` | `master_postinit` | 添加洞穴 spawner、`caveins`、`riftspawner` 和遗迹相关管理器 |
| `scripts/prefabs/cave_network.lua` | `custom_postinit` | 添加 `caveweather`、`quaker` 和 `nightmareclock` |
| `scripts/components/caveweather.lua` | `OnUpdate` | 洞穴降水、酸雨和湿度广播 |
| `scripts/components/quaker.lua` | `StartQuake` | 地震预警、落石、音效和强制地震事件 |
| `scripts/components/nightmareclock.lua` | `OnUpdate` | 梦魇相位、梦魇计时和事件广播 |
| `scripts/components/wavemanager.lua` | `OnUpdate` | 客户端波浪 prefab 生成 |
| `scripts/components/oceancolor.lua` | `OnPhaseChanged` | 客户端海洋颜色与纹理混合 |
| `scripts/components/oceanicemanager.lua` | `CreateIceAtPoint` | 海冰创建、破裂、对象落水修正 |
| `scripts/components/dockmanager.lua` | `DestroyDockAtPoint` | dock 移除后的对象落水修正 |
| `scripts/components/walkableplatform.lua` | `DestroyObjectsOnPlatform` | 平台销毁时对象清理 |
| `scripts/components/vaultroom.lua` | `_getunloadaction` | Vault 房间卸载时实体保存策略 |
| `scripts/map/tasks/ruins.lua` | `AddTask` | 遗迹任务层配置 |
| `scripts/map/rooms/cave/ruins.lua` | `AddRoom` | 遗迹房间、分布和静态布局入口 |
| `scripts/prefabs/ruinsrespawner.lua` | `MakeFn` | 遗迹物件重置和 `resetruins` 事件处理 |

### `0x52022111` 主锚点 / `scripts/prefabs/cave_network.lua` / 搜索信号

搜索 `AddComponent("caveweather")`、`AddComponent("quaker")` 和 `AddComponent("nightmareclock")`。
这三个组件定义洞穴运行时环境的主要信号源。

### `0x52022121` 主锚点 / `scripts/prefabs/forest.lua` / 搜索信号

搜索 `AddWaveComponent`、`AddComponent("wavemanager")` 和 `AddComponent("oceanicemanager")`。
这能区分海洋表现、波浪生成和海冰管理。

### `0x52022131` 主锚点 / `scripts/map/rooms/cave/ruins.lua` / 搜索信号

搜索 `AddRoom`、`ruins_statue` 和 `cave_hole`。
遗迹内容从 worldgen 房间布置进入，运行时再由 prefab 和 respawner 接管。

## `0x52023000` 运行流程

~~~mermaid
flowchart TD
    A["prefabs/world.lua\ncommon world services"]
    B["prefabs/cave.lua\ncave world tag + cave master components"]
    C["prefabs/cave_network.lua\ncaveweather + quaker + nightmareclock"]
    D["components/caveweather.lua\nweathertick / precipitationchanged"]
    E["components/quaker.lua\nwarnquake / startquake / endquake"]
    F["components/nightmareclock.lua\nnightmarephasechanged"]
    G["components/worldstate.lua\ncave and nightmare fields"]
    H["map/tasks/ruins.lua\nruins task graph"]
    I["map/rooms/cave/ruins.lua\nroom contents"]
    J["prefabs/ruinsrespawner.lua\nresetruins"]
    K["prefabs/forest.lua\nwaves + ocean ice"]
    L["components/wavemanager.lua\nocean visuals"]
    M["components/oceanicemanager.lua\nocean ice runtime"]

    A --> B
    B --> C
    C --> D
    C --> E
    C --> F
    D --> G
    F --> G
    H --> I
    I --> J
    A --> K
    K --> L
    K --> M
~~~

### `0x52023111` 洞穴装配阶段 / `cave.lua` 添加洞穴世界组件 / 验证动作

在 `scripts/prefabs/cave.lua` 搜索 `master_postinit`。
确认 `caveins`、`hounded`、`retrofitcavemap_anr`、`riftspawner` 和 `miasmamanager`。
同时确认 `ruinsshadelingspawner` 与 `vaultroommanager` 都在 Master Simulation 侧添加。

### `0x52023121` 洞穴装配阶段 / `cave_network.lua` 添加网络环境组件 / 验证动作

在 `scripts/prefabs/cave_network.lua` 搜索 `SetTemperatureMod`。
洞穴网络世界不仅添加特殊组件，还给 `worldtemperature` 设置洞穴温度修正。

### `0x52023211` 洞穴天气阶段 / `caveweather.lua` 接收季节、温度和阶段事件 / 验证动作

搜索 `inst:ListenForEvent("seasontick"`、`inst:ListenForEvent("temperaturetick"` 和 `inst:ListenForEvent("phasechanged"`。
洞穴降水仍依赖公共时序事件，但计算逻辑在 `caveweather.lua` 内。

### `0x52023221` 洞穴天气阶段 / `caveweather.lua` 广播天气状态 / 验证动作

搜索 `_world:PushEvent("weathertick"`、`preciptypedirty`、`wetdirty` 和 `moistureceildirty`。
这些事件会回到 `worldstate.lua` 的天气字段。

### `0x52023311` 地震与梦魇阶段 / `quaker.lua` 管理地震生命周期 / 验证动作

搜索 `WarnQuake`、`StartQuake`、`EndQuake`、`ms_miniquake` 和 `ms_forcequake`。
组件向世界推送 `warnquake`、`startquake` 和 `endquake`，具体落石由同文件的 debris 逻辑处理。

### `0x52023321` 地震与梦魇阶段 / `nightmareclock.lua` 管理梦魇相位 / 验证动作

搜索 `nightmarephasechanged` 和 `nightmareclocktick`。
`worldstate.lua` 将这些事件投影为 `nightmarephase`、`isnightmarecalm`、`isnightmarewarn`、`isnightmarewild` 和 `isnightmaredawn`。

### `0x52023411` 海洋运行阶段 / `world.lua` 提供通用水体能力 / 验证动作

搜索 `AddComponent("walkableplatformmanager")` 和 `AddComponent("waterphysics")`。
这些组件由通用世界添加，所以不要把海洋全部归到地表 prefab。

### `0x52023421` 海洋运行阶段 / `forest.lua` 启用海洋表现 / 验证动作

搜索 `AddWaveComponent`、`Map:DoOceanRender(true)` 和 `AddComponent("wavemanager")`。
`wavemanager` 只在非 dedicated server 添加，主要服务客户端表现。

### `0x52023431` 海洋运行阶段 / `oceanicemanager.lua` 管理海冰 / 验证动作

搜索 `worldmapsetsize`、`CreateIceAtPoint`、`DestroyIceAtPoint` 和 `icefloebreak`。
海冰逻辑会修正 floater、fishable 和平台上的对象，不能只按 tile 状态理解。

### `0x52023511` 遗迹生成与重置阶段 / `tasks/ruins.lua` 与 `rooms/cave/ruins.lua` / 验证动作

在 `scripts/map/tasks/ruins.lua` 搜索 `AddTask`。
在 `scripts/map/rooms/cave/ruins.lua` 搜索 `AddRoom`。
前者决定遗迹任务结构，后者决定房间内 prefab、分布和静态布局。

### `0x52023521` 遗迹生成与重置阶段 / `ruinsrespawner.lua` / 验证动作

搜索 `resetruins`、`objectspawner` 和 `onprefabswaped`。
遗迹物件重置不是 worldgen 重新跑一遍，而是运行时 respawner 处理对象再生。

## `0x52024111` 结构细节 / 洞穴状态字段 / `worldstate.lua` 的 Cave 字段 / 字段核对

`cavephase`、`iscaveday`、`iscavedusk`、`iscavenight` 和 `cavemoonphase` 来自 cave 分支事件。
`iscavefullmoon` 与 `iscavenewmoon` 也由 cave 分支的 phase 和 moonphase 事件更新。

## `0x52024121` 结构细节 / 洞穴状态字段 / `worldstate.lua` 的 Nightmare 字段 / 字段核对

`nightmarephase`、`nightmaretime`、`nightmaretimeinphase` 和四个 `isnightmare*` 布尔值来自 `nightmareclock.lua`。

## `0x52024211` 结构细节 / 海洋组件分层 / 通用世界层 / 边界条件

`waterphysics`、`walkableplatformmanager` 和客户端 `oceancolor` 从 `world.lua` 装配。
这些能力不只服务地表海洋，也会影响所有世界的水体和平台规则。

## `0x52024221` 结构细节 / 海洋组件分层 / 地表表现层 / 边界条件

`WaveComponent`、`wavemanager` 和 `Map:DoOceanRender(true)` 在 `forest.lua`。
它们是地表海洋表现入口，不是洞穴运行时入口。

## `0x52024231` 结构细节 / 海洋组件分层 / 地表海冰层 / 边界条件

`oceanicemanager` 在 `forest.lua` 的 `master_postinit` 添加。
它监听地图尺寸并管理海冰生命周期。

## `0x52024311` 结构细节 / 遗迹内容分层 / Worldgen 层 / 边界条件

`map/tasks/ruins.lua` 和 `map/rooms/cave/ruins.lua` 描述生成结构。
这些文件决定遗迹在哪里出现和出现什么。

## `0x52024321` 结构细节 / 遗迹内容分层 / Runtime 层 / 边界条件

`prefabs/ruinsrespawner.lua` 和具体遗迹 prefab 描述重置后的行为。
不要把 `resetruins` 理解成重新生成整张洞穴地图。

## `0x52024411` 结构细节 / Cave、Ocean、Vault 边界 / 验证点

`prefabs/cave.lua` 的 cave gel spawn 会排除 topology id 中包含 `Vault` 的区域。
`vaultroom.lua` 在虚拟房间卸载时会把拥有 migration pet 的 owner 当成需要保留的实体。
`minotaur.lua` 会在 `ATRIUM_KEY_FOUND` 后关闭 minimap revealable。
`trap_fumarole.lua` 在物品态、掉落态和开始漂浮时都会禁用 minimap icon。
`moonstorm_spark.lua` 移除了 spore density crowding 检查，生命周期主要回到 `perishable` 与 spark task。
`componentutil.lua`、`dockmanager.lua`、`oceanicemanager.lua` 和 `walkableplatform.lua` 都会检查 `childdeployblocker`。
带该 tag 的 child 会被当成需要处理的场景阻挡实体。

## `0x52025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n \
  -e "AddComponent\\(\"(caveweather|quaker|nightmareclock|caveins)\"\\)" \
  -e "AddComponent\\(\"(wavemanager|oceanicemanager|waterphysics|walkableplatformmanager)\"\\)" \
  -e "PushEvent\\(\"(weathertick|nightmarephasechanged|nightmareclocktick)" \
  -e "PushEvent\\(\"(warnquake|startquake|endquake|icefloebreak)\"" \
  -e "AddWaveComponent|AddTask|AddRoom|resetruins" \
  scripts/prefabs/world.lua \
  scripts/prefabs/forest.lua \
  scripts/prefabs/cave.lua \
  scripts/prefabs/cave_network.lua \
  scripts/components/caveweather.lua \
  scripts/components/quaker.lua \
  scripts/components/nightmareclock.lua \
  scripts/components/wavemanager.lua \
  scripts/components/oceanicemanager.lua \
  scripts/map/tasks/ruins.lua \
  scripts/map/rooms/cave/ruins.lua \
  scripts/prefabs/ruinsrespawner.lua

rg -n "Vault|migrationpetowner|ATRIUM_KEY_FOUND|childdeployblocker|SetEnabled\\(false\\)|MUSHSPORE|crowding" \
  scripts/prefabs/cave.lua \
  scripts/components/vaultroom.lua \
  scripts/prefabs/minotaur.lua \
  scripts/prefabs/trap_fumarole.lua \
  scripts/prefabs/moonstorm_spark.lua \
  scripts/componentutil.lua \
  scripts/components/dockmanager.lua \
  scripts/components/oceanicemanager.lua \
  scripts/components/walkableplatform.lua
~~~

### `0x52025111` 最小阅读顺序 / 验证闭环

先读 `cave_network.lua` 的三个环境组件。
再读 `caveweather.lua`、`quaker.lua` 和 `nightmareclock.lua` 的输出事件。
然后回到 `worldstate.lua` 验证 cave 与 nightmare 字段。
最后分支阅读 `forest.lua` 的海洋表现和 `map/rooms/cave/ruins.lua` 的遗迹生成。

### `0x52025121` 遗迹运行时抽样查询 / 验证闭环

~~~bash
rg -n "resetruins|ruinsrespawner|ruins_statue|ruins_shadeling|nightmarephase" \
  scripts/prefabs \
  scripts/map/rooms/cave/ruins.lua
~~~

这条查询用于区分遗迹生成数据、遗迹 prefab 和梦魇状态消费点。
