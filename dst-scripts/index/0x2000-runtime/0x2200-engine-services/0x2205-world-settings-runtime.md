# `0x22050000` World Settings Runtime

本页覆盖世界设置如何从 worldgen overrides 进入运行时组件。

它重点解释 `worldsettings_overrides.lua`、`worldsettingsutil.lua`、`worldsettings` 和 `worldsettingstimer`。

## `0x22051111` 本页定位 / 要回答的运行时问题 / Worldgen 选项如何变成运行时行为 / 验证点

`gamelogic.lua` 加载 `worldsettings_overrides`。

世界初始化时先应用 `WorldSettings_Overrides.Pre`。

世界和实体加载后再应用 `WorldSettings_Overrides.Post`。

部分设置通过 `WorldSettings_Overrides.Sync` 推送 `ms_setworldsetting` 事件。

`components/worldsettings.lua` 接收事件并记录当前 setting value。

## `0x22052000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/gamelogic.lua` | `require("worldsettings_overrides")` | 加载运行时世界设置覆盖表 |
| `scripts/gamelogic.lua` | `WorldSettings_Overrides.Pre` | 世界 prefab 生成前应用覆盖 |
| `scripts/gamelogic.lua` | `WorldSettings_Overrides.Post` | 世界与实体加载后应用覆盖 |
| `scripts/worldsettings_overrides.lua` | `Pre` / `Post` / `Sync` | 定义世界设置覆盖阶段 |
| `scripts/worldsettings_overrides.lua` | `ms_setworldsetting` | 同步设置到世界组件 |
| `scripts/components/worldsettings.lua` | `WorldSettings:SetSetting` | 保存当前世界设置值 |
| `scripts/worldsettingsutil.lua` | `WorldSettings_ChildSpawner_PreLoad` | 把 childspawner 计时迁到 worldsettingstimer |
| `scripts/worldsettingsutil.lua` | `WorldSettings_Spawner_SpawnDelay` | 用 worldsettingstimer 驱动 spawner |
| `scripts/worldsettingsutil.lua` | `WorldSettings_Pickable_RegenTime` | 用 worldsettingstimer 驱动 pickable 再生 |
| `scripts/components/worldsettingstimer.lua` | `WorldSettingsTimer` | 保存、恢复和推进设置驱动 timer |
| `scripts/components/worldsettingstimer.lua` | `LongUpdate` | 离线或长帧更新 timer |

## `0x22053000` 运行流程

~~~mermaid
flowchart TD
    A["worldgen overrides"]
    A --> B["gamelogic loads savedata"]
    B --> C["WorldSettings_Overrides.Pre"]
    C --> D["Spawn world prefab"]
    D --> E["load entities and components"]
    E --> F["WorldSettings_Overrides.Post"]
    F --> G["WorldSettings_Overrides.Sync"]
    G --> H["TheWorld:PushEvent ms_setworldsetting"]
    H --> I["WorldSettings:SetSetting"]
    F --> J["worldsettingsutil helpers"]
    J --> K["worldsettingstimer timers"]
~~~

### `0x22053111` Pre 阶段 / 世界生成后、世界 Prefab 前 / 边界条件

Pre 阶段发生在世界 prefab 生成之前。

它适合影响世界初始化需要读取的基础配置。

阅读时从 `gamelogic.lua` 中遍历 `WorldSettings_Overrides.Pre` 的位置开始。

### `0x22053211` Post 阶段 / 世界与实体加载后 / 边界条件

Post 阶段发生在世界和实体已经加载之后。

它可以调用实体、组件和世界管理器。

这也是很多 `worldsettingsutil.lua` helper 接管组件 timer 数据的阶段。

### `0x22053311` Sync 阶段 / `ms_setworldsetting` / 边界条件

Sync 阶段通过 `TheWorld:PushEvent("ms_setworldsetting", ...)` 写入运行时设置值。

`components/worldsettings.lua` 监听该事件。

`WorldSettings:SetSetting(setting, value)` 只记录值，不直接执行所有具体玩法副作用。

具体副作用通常已经在 Pre 或 Post helper 中完成。

## `0x22054111` 结构细节 / `worldsettingstimer` / 统一 Timer 容器 / 需要核对的字段

`WorldSettingsTimer:AddTimer` 保存 timer 名称、最大时间、启用状态、callback 和 long update handler。

`StartTimer`、`PauseTimer`、`ResumeTimer` 和 `StopTimer` 控制运行状态。

`OnSave` 和 `OnLoad` 持久化 timer。

`LongUpdate(dt)` 用于离线或长帧推进。

## `0x22054211` 结构细节 / 组件计时迁移 / `worldsettingsutil.lua` / 需要核对的字段

`WorldSettings_ChildSpawner_PreLoad` 把 `childspawner` 计时数据迁入 `worldsettingstimer`。

`WorldSettings_Timer_PreLoad` 把 `timer` 组件数据迁入 `worldsettingstimer`。

`WorldSettings_Spawner_PreLoad` 把 `spawner` 下一次生成时间迁入 `worldsettingstimer`。

`WorldSettings_Pickable_PreLoad` 把 `pickable` 再生时间迁入 `worldsettingstimer`。

这些函数说明 world settings runtime 同时负责运行时设置和存档兼容数据的规整。

## `0x22055100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "worldsettings_overrides|WorldSettings_Overrides\\.(Pre|Post|Sync)" \
  scripts/gamelogic.lua

rg -n "ms_setworldsetting|SetSetting|Pre|Post|Sync" \
  scripts/worldsettings_overrides.lua \
  scripts/components/worldsettings.lua

rg -n "WorldSettings_.*PreLoad|WorldSettings_.*Spawn|WorldSettings_.*Regen|worldsettingstimer" \
  scripts/worldsettingsutil.lua \
  scripts/components/worldsettingstimer.lua
~~~

### `0x22055111` 推荐顺序 / 最小闭环

先读 `gamelogic.lua` 中 Pre 与 Post 的调用点。

再读 `worldsettings_overrides.lua` 的具体覆盖表。

最后读 `worldsettingsutil.lua` 和 `worldsettingstimer.lua`，确认 timer 如何迁移、保存和推进。
