# `0x52010000` 天气与季节

本页按网络世界、世界状态、季节、温度、地表天气和实体反应的顺序阅读。
关键判断是 `world_network` 负责公共时序，`forest_network` 负责地表天气，`worldstate` 只汇总事件后的可观察状态。

## `0x52011111` 本页定位 / 运行时问题 / 世界状态不是天气源头 / 验证点

`scripts/components/worldstate.lua` 不主动计算季节、降水或温度。
它监听 `clocktick`、`seasontick`、`temperaturetick`、`weathertick` 和若干 dirty 事件，再更新 `TheWorld.state` 指向的 `self.data`。

## `0x52011121` 本页定位 / 运行时问题 / 网络世界先于地表天气 / 验证点

`scripts/prefabs/world_network.lua` 给 `TheWorld.net` 装配 `clock`、`worldtemperature` 和 `seasons`。
`scripts/prefabs/forest_network.lua` 再通过 `custom_postinit` 装配 `weather`。

## `0x52012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/prefabs/world.lua` | `MakeWorld` | 创建 `TheWorld`、添加 `worldstate`、暴露 `inst.state` |
| `scripts/prefabs/world_network.lua` | `MakeWorldNetwork` | 创建 `TheWorld.net`、添加 `clock`、`worldtemperature`、`seasons` |
| `scripts/prefabs/forest_network.lua` | `custom_postinit` | 给地表网络世界添加 `weather` |
| `scripts/components/clock.lua` | `OnUpdate` | 推进日夜、月相和周期事件 |
| `scripts/components/seasons.lua` | `OnSeasonDirty` | 生成 `seasontick` 和季节长度变化 |
| `scripts/components/worldtemperature.lua` | `OnSeasonTick` | 合成世界温度并广播 `temperaturetick` |
| `scripts/components/weather.lua` | `OnUpdate` | 计算地表湿度、降水、雪覆盖、闪电和月雹 |
| `scripts/components/worldstate.lua` | `SetVariable` | 把事件数据收束到 `TheWorld.state` |
| `scripts/components/temperature.lua` | `OnUpdate` | 个体温度读取环境、装备、热源和湿度惩罚 |
| `scripts/components/moisture.lua` | `OnUpdate` | 个体湿度读取降水、防水、干燥和水域状态 |

### `0x52012111` 主锚点 / `scripts/prefabs/world_network.lua` / 搜索信号

搜索 `AddComponent("clock")`、`AddComponent("worldtemperature")` 和 `AddComponent("seasons")`。
这三个组件挂在 `TheWorld.net` 上，而不是挂在 `TheWorld.components` 上。

### `0x52012121` 主锚点 / `scripts/prefabs/forest_network.lua` / 搜索信号

搜索 `AddComponent("weather")`。
这说明地表降水逻辑属于 `forest_network`，洞穴降水不要从本页的 `weather.lua` 推断。

### `0x52012131` 主锚点 / `scripts/components/worldstate.lua` / 搜索信号

搜索 `inst:ListenForEvent("weathertick"` 和 `SetVariable("precipitation"`。
这能确认 `worldstate` 的职责是状态投影和 watcher 通知。

## `0x52013000` 运行流程

~~~mermaid
flowchart TD
    A["prefabs/world.lua\nAddComponent(worldstate)"]
    B["prefabs/world_network.lua\nclock + seasons + worldtemperature"]
    C["prefabs/forest_network.lua\nweather"]
    D["components/clock.lua\nclocktick / phasechanged"]
    E["components/seasons.lua\nseasontick"]
    F["components/worldtemperature.lua\ntemperaturetick"]
    G["components/weather.lua\nweathertick / precipitationchanged"]
    H["components/worldstate.lua\nSetVariable"]
    I["EntityScript:WatchWorldState\nentity / component reactions"]

    A --> H
    B --> D
    B --> E
    B --> F
    C --> G
    D --> H
    E --> F
    E --> G
    E --> H
    F --> G
    F --> H
    G --> H
    H --> I
~~~

### `0x52013111` 世界装配阶段 / `MakeWorld` 创建 `TheWorld.state` / 验证动作

在 `scripts/prefabs/world.lua` 搜索 `inst:AddComponent("worldstate")`。
紧随其后的 `inst.state = inst.components.worldstate.data` 说明大多数调用点读取的是同一张表。

### `0x52013121` 世界装配阶段 / `MakeWorldNetwork` 创建时序组件 / 验证动作

在 `scripts/prefabs/world_network.lua` 搜索 `inst:AddComponent("seasons")`。
同时确认 `clock` 和 `worldtemperature` 也在同一个 network prefab 初始化。

### `0x52013211` 时序广播阶段 / `clock.lua` 推送日夜事件 / 验证动作

搜索 `_world:PushEvent("clocktick"`、`_world:PushEvent("phasechanged"` 和 `ms_cyclecomplete`。
`seasons.lua` 监听 `ms_cyclecomplete` 来推进季节。

### `0x52013221` 时序广播阶段 / `seasons.lua` 推送季节事件 / 验证动作

搜索 `_world:PushEvent("seasontick"`。
同一组件还监听 `ms_setseason`、`ms_advanceseason` 和 `ms_setseasonlength`。

### `0x52013231` 时序广播阶段 / `worldtemperature.lua` 合成环境温度 / 验证动作

搜索 `OnSeasonTick`、`OnClockTick` 和 `_world:PushEvent("temperaturetick"`。
温度来自季节进度、日夜阶段和噪声，而不是由 `weather.lua` 单独决定。

### `0x52013311` 地表天气阶段 / `weather.lua` 监听季节、温度和阶段 / 验证动作

搜索 `inst:ListenForEvent("seasontick"`、`inst:ListenForEvent("temperaturetick"` 和 `inst:ListenForEvent("phasechanged"`。
这些事件改变降水概率、降水类型、雪覆盖和闪电节奏。

### `0x52013321` 地表天气阶段 / `weather.lua` 写回世界状态事件 / 验证动作

搜索 `_world:PushEvent("weathertick"`、`preciptypedirty`、`snowcovereddirty` 和 `wetdirty`。
这些信号最终被 `worldstate.lua` 接收。

### `0x52013411` 实体反应阶段 / `EntityScript:WatchWorldState` / 验证动作

在 `scripts/entityscript.lua` 搜索 `WatchWorldState`。
实体和组件通过 `TheWorld.components.worldstate:AddWatcher` 订阅 `israining`、`issnowing`、`iswet`、`temperature` 等变量。

### `0x52013421` 实体反应阶段 / `temperature.lua` 与 `moisture.lua` / 验证动作

在 `scripts/components/temperature.lua` 搜索 `GetLocalTemperature` 和 `moisture`。
在 `scripts/components/moisture.lua` 搜索 `GetMoistureRate`、`GetWaterproofness` 和 `OnUpdate`。

## `0x52014111` 结构细节 / `worldstate.data` 字段族 / 时钟字段 / 字段核对

`time`、`timeinphase`、`cycles`、`phase`、`isday`、`isdusk`、`isnight`、`moonphase` 和月相布尔值由 `clock.lua` 事件驱动。

## `0x52014121` 结构细节 / `worldstate.data` 字段族 / 季节字段 / 字段核对

`season`、`isautumn`、`iswinter`、`isspring`、`issummer`、`seasonprogress` 和剩余天数由 `seasons.lua` 事件驱动。

## `0x52014131` 结构细节 / `worldstate.data` 字段族 / 天气字段 / 字段核对

`temperature` 来自 `worldtemperature.lua`。
`moisture`、`pop`、`precipitationrate`、`snowlevel`、`lunarhaillevel`、`wetness` 和 `iswet` 来自 `weather.lua` 的事件。

## `0x52014211` 结构细节 / Server 与 Client 边界 / 权威逻辑在 Master Simulation / 边界条件

`clock.lua`、`seasons.lua`、`worldtemperature.lua` 和 `weather.lua` 都含有 server 侧事件处理和 netvar dirty 通知。
阅读时先追 `TheWorld.ismastersim` 下的修改路径，再看 dirty 事件如何同步给客户端表现。

## `0x52014221` 结构细节 / Server 与 Client 边界 / 洞穴天气不走 `weather.lua` / 边界条件

洞穴网络世界使用 `scripts/components/caveweather.lua`。
不要把地表 `weather.lua` 的雪覆盖、月雹和闪电规则直接套到洞穴。

## `0x52015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n \
  -e "AddComponent\\(\"(worldstate|clock|worldtemperature|seasons|weather)\"\\)" \
  -e "PushEvent\\(\"(clocktick|seasontick|temperaturetick|weathertick)" \
  -e "PushEvent\\(\"(precipitationchanged|wetchanged)\"" \
  -e "ListenForEvent\\(\"(clocktick|seasontick|temperaturetick|weathertick)" \
  -e "ListenForEvent\\(\"(precipitationchanged|wetchanged)" \
  scripts/prefabs/world.lua \
  scripts/prefabs/world_network.lua \
  scripts/prefabs/forest_network.lua \
  scripts/components/clock.lua \
  scripts/components/seasons.lua \
  scripts/components/worldtemperature.lua \
  scripts/components/weather.lua \
  scripts/components/worldstate.lua
~~~

### `0x52015111` 最小阅读顺序 / 验证闭环

先读 `world_network.lua` 的组件装配。
再读 `clock.lua` 的 `clocktick`。
接着读 `seasons.lua` 的 `seasontick`。
然后读 `worldtemperature.lua` 和 `weather.lua` 的输出事件。
最后回到 `worldstate.lua` 验证 `TheWorld.state` 字段。

### `0x52015121` 抽样实体查询 / 验证闭环

~~~bash
rg -n "WatchWorldState\\(\"(israining|issnowing|iswet|temperature|season|iswinter|isspring)" \
  scripts/prefabs \
  scripts/components
~~~

这条查询用于确认天气和季节状态如何被实体、组件和 UI 消费。
