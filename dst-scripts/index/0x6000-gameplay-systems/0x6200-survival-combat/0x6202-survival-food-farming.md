# `0x62020000` 生存食物与农场

生存页要分成食用、饥饿衰减、腐败修正和农场种植几条链路。
食物不会直接流向农场管理器，农场也不是食用动作的后续步骤。

## `0x62021000` 本页定位

本页解释玩家吃东西如何改变 `health`、`hunger` 和 `sanity`。
同时解释农场从种子部署到土壤、植物压力和营养循环的源码入口。

### `0x62021100` 要回答的运行时问题

吃东西时数值来自哪里。
腐败如何影响食物效果。
饥饿值何时反过来扣生命。
农场种子如何变成作物，并如何读取世界级 farming manager。

#### `0x62021110` 源码阅读目标

把 `ACTIONS.EAT`、`Eater:Eat`、`Edible:Get*`、`Hunger:DoDelta` 和 `Health:DoDelta` 串起来。
把 `PLANTSOIL`、`farmplantable`、`farm_plants` 和 `farming_manager` 串起来。

##### `0x62021111` 验证点

`ACTIONS.EAT.fn` 调用 `eater:Eat`。
`Eater:Eat` 同时计算 health、hunger、sanity 三类 delta。
`Perishable:IsStale` 和 `Perishable:IsSpoiled` 会被 `Edible:Get*` 读取。
农场营养通过 `FarmingManager:CycleNutrientsAtPoint` 更新。

## `0x62022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/actions.lua` | `ACTIONS.EAT.fn` | 食用动作入口 |
| `scripts/components/eater.lua` | `Eater:Eat` | 吃东西并分发数值变化 |
| `scripts/components/edible.lua` | `Edible:GetHunger` | 食物数值与腐败修正 |
| `scripts/components/perishable.lua` | `Perishable:IsSpoiled` | 腐败阶段判定 |
| `scripts/components/inventoryitemtemperature.lua` | `UpdateTemperature` | 背包物品温度趋近与隔热修正 |
| `scripts/components/inventoryitem.lua` | `EnableTemperature` | 物品温度组件的外层入口 |
| `scripts/components/hunger.lua` | `Hunger:DoDec` | 饥饿衰减与饥饿伤害 |
| `scripts/components/sanity.lua` | `Sanity:DoDelta` | 食物理智变化与持续理智速率 |
| `scripts/prefabs/desiccant.lua` | `OnUpdateExternallyControlled` | 干燥剂温度与吸湿状态连接 |
| `scripts/components/farmplantable.lua` | `FarmPlantable:Plant` | 种子种入土壤 |
| `scripts/components/farmplanttendable.lua` | `FarmPlantTendable:TendTo` | 作物照料状态 |
| `scripts/prefabs/farm_plants.lua` | `MakePlant` | 作物 prefab 与压力系统 |
| `scripts/components/farming_manager.lua` | `CycleNutrientsAtPoint` | 世界级土壤营养循环 |
| `scripts/standardcomponents.lua` | `AddTileNutrients` | 通用农场营养 helper |

### `0x62022110` 主锚点 / `scripts/actions.lua`

`ACTIONS.EAT.fn` 从 `act.target` 或 `act.invobject` 取对象。
如果对象有 `edible` 且 doer 有 `eater`，就调用 `act.doer.components.eater:Eat(obj, act.doer)`。

#### `0x62022111` 搜索信号

搜索 `ACTIONS.EAT.fn`、`components.edible`、`components.eater` 和 `souleater`。

### `0x62022120` 主锚点 / `scripts/components/eater.lua`

`Eater:Eat` 先用 `PrefersToEat` 校验饮食规则。
随后读取 `Edible:GetHealth`、`Edible:GetHunger` 和 `Edible:GetSanity`，并调用对应组件的 `DoDelta`。

#### `0x62022121` 搜索信号

搜索 `Eater:Eat`、`PrefersToEat`、`foodmemory`、`custom_stats_mod_fn`、`oneat` 和 `OnEaten`。

### `0x62022130` 主锚点 / `scripts/components/edible.lua`

`Edible:GetSanity`、`Edible:GetHunger` 和 `Edible:GetHealth` 负责把基础食物数值转换成实际效果。
这里会考虑腐败、spice、foodaffinity 和自定义 getter。

#### `0x62022131` 搜索信号

搜索 `GetSanity`、`GetHunger`、`GetHealth`、`IsStale`、`IsSpoiled` 和 `foodaffinity`。

### `0x62022140` 主锚点 / `scripts/components/hunger.lua`

`Hunger:DoDelta` 只改饥饿当前值。
`Hunger:DoDec` 才负责随时间消耗饥饿，并在饥饿为零时调用 `health:DoDelta(..., "hunger")`。

#### `0x62022141` 搜索信号

搜索 `DoDelta`、`DoDec`、`hungerrate`、`hurtrate` 和 `overridestarvefn`。

### `0x62022150` 主锚点 / `scripts/components/sanity.lua`

食物可以通过 `Eater:Eat` 调用 `sanity:DoDelta`。
理智组件自身还在 `Sanity:Recalc` 中汇总装备、潮湿、光照、光环、幽灵和外部 modifier。

#### `0x62022151` 搜索信号

搜索 `Sanity:DoDelta`、`Sanity:Recalc`、`dapperness`、`sanityaura` 和 `externalmodifiers`。

### `0x62022160` 主锚点 / `scripts/components/farmplantable.lua`

`FarmPlantable:Plant` 要求目标有 `soil` tag。
它生成作物 prefab，设置位置，推送 `on_planted`，再移除种子。

#### `0x62022161` 搜索信号

搜索 `FarmPlantable:Plant`、`HasTag("soil")`、`SpawnPrefab` 和 `on_planted`。

### `0x62022170` 主锚点 / `scripts/components/farmplanttendable.lua`

`FarmPlantTendable:TendTo` 只有在 `tendable` 为真且 `ontendtofn` 成功时才清除可照料状态。
`farm_plants.lua` 在成长阶段切换时用 `SetTendable(stage_data.tendable)` 更新这个状态。

#### `0x62022171` 搜索信号

搜索 `FarmPlantTendable:TendTo`、`SetTendable`、`ontendtofn` 和 `tendable_farmplant`。

## `0x62023000` 运行流程

~~~mermaid
flowchart TD
    A["ACTIONS.EAT"]
    A --> B["Eater:Eat"]
    B --> C["Edible:GetHealth/GetHunger/GetSanity"]
    C --> D["Perishable stale/spoiled 修正"]
    C --> E["foodaffinity / foodmemory / spice"]
    E --> F["Health:DoDelta"]
    E --> G["Hunger:DoDelta"]
    E --> H["Sanity:DoDelta"]
    I["PLANTSOIL / DEPLOY"]
    I --> J["FarmPlantable:Plant"]
    J --> K["farm_plants MakePlant"]
    K --> L["farmplantstress / growable / farmsoildrinker"]
    L --> M["FarmingManager:CycleNutrientsAtPoint"]
~~~

### `0x62023110` 食用链路 / 数值结算顺序

`Eater:Eat` 先计算三类 delta。
`custom_stats_mod_fn` 可以在写入组件前统一调整。
最后分别调用 `health`、`hunger` 和 `sanity` 的 `DoDelta`。

#### `0x62023111` 边界条件

不能把 `edible` 当作直接改状态的组件。
`edible` 只提供数值和 `OnEaten` 回调，真正状态写入由 eater 调组件完成。

### `0x62023210` 饥饿伤害链路 / Hunger 与 Health 的连接

`Hunger:DoDec` 在饥饿值大于零时扣 hunger。
当 hunger 为零且没有 `ignore_damage` 时，它调用 `health:DoDelta(-hurtrate * dt, true, "hunger")`。

#### `0x62023211` 验证点

饥饿造成的生命损失原因是 `"hunger"`。
这条链路不经过 `Eater:Eat`。

### `0x62023310` 农场链路 / 种子到植物

`ACTIONS.PLANTSOIL.fn` 会调用 `seed.components.farmplantable:Plant(act.target, act.doer)`。
`FarmPlantable:Plant` 创建 plant，并把种子 prefab 作为 `on_planted` 事件数据传给植物。

#### `0x62023311` 边界条件

农场作物的压力不是一个单独字段。
`farm_plants.lua` 同时挂 `farmplantstress`、`farmsoildrinker` 和 `farmplanttendable`。

### `0x62023410` 物品温度链路 / 干燥剂与外部热源

`inventoryitemtemperature.lua` 把目标温度计算拆成 `GetTargetDeltaTemperature()` 和 `GetTargetTemperature()`。
`DoDelta()` 与 `UpdateTemperature()` 会按 inherent winter/summer insulation 缩放升温和降温速率。
外部 heater 会写入 `externalheaterpower`，fumarole 工具和 trap 会用它调整 `fumaroletool_mod` 温度 modifier。
`desiccant.lua` 启用物品温度后，用 `DESSICANT_MIN_TEMPERATURE`、`DESSICANT_THRESHOLD_TEMPERATURE` 和 owner 湿度决定是否由系统控制含水量。
owner 不干时，过热的干燥剂会被压回 `DESICCANT_HELD_TEMPERATURE`。

## `0x62024110` 结构细节 / 食物数据结构 / Edible、Perishable 与 Eater 的分工

`edible` 保存食物类型和基础数值。
`perishable` 保存新鲜度、腐败阶段和腐败事件。
`eater` 保存角色饮食规则和吸收倍率。
`sanity` 接收食物带来的一次性 delta，也负责装备、光照和光环带来的持续变化。
`gelblob_storage.lua` 会记录物品进入 storage 前是否正在 perishing，并在取出时只恢复原本正在腐败的物品。

### `0x62024111` 需要核对的字段

检查 `healthabsorption`、`hungerabsorption`、`sanityabsorption`。
检查 `degrades_with_spoilage`、`stale_hunger`、`spoiled_hunger`。
检查 `foodmemory` 是否改变重复食用倍率。

## `0x62024210` 结构细节 / 农场数据结构 / Plant Def 与世界 Manager

`farm_plant_defs.lua` 定义作物数据。
`farm_plants.lua` 读取 plant def 并创建每种植物 prefab。
`farming_manager.lua` 维护 tile 上的营养和湿度。
`farmplanttendable` 保存作物是否可照料。
`standardcomponents.lua` 提供会调用 `farming_manager:AddTileNutrients` 的通用 helper。

### `0x62024211` 验证点

`CycleNutrientsAtPoint` 先把世界坐标转成 tile 坐标。
如果没有 farming overlay，它会返回 soil depleted。
如果 `test_only` 为真，它只返回是否 depleted。

## `0x62025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "ACTIONS\\.EAT\\.fn|ACTIONS\\.PLANTSOIL\\.fn|farmplantable:Plant" \
  scripts/actions.lua

rg -n "Eater:Eat|PrefersToEat|custom_stats_mod_fn|foodmemory|OnEaten" \
  scripts/components/eater.lua

rg -n "GetHealth|GetHunger|GetSanity|IsStale|IsSpoiled|foodaffinity" \
  scripts/components/edible.lua \
  scripts/components/perishable.lua \
  scripts/prefabs/gelblob_storage.lua

rg -n "DoDelta|DoDec|overridestarvefn|hurtrate" \
  scripts/components/hunger.lua

rg -n "Sanity:DoDelta|Sanity:Recalc|dapperness|sanityaura|externalmodifiers" \
  scripts/components/sanity.lua

rg -n "FarmPlantable:Plant|FarmPlantTendable:TendTo|SetTendable|MakePlant" \
  scripts/components/farmplantable.lua \
  scripts/components/farmplanttendable.lua \
  scripts/prefabs/farm_plants.lua

rg -n "farmplantstress|CycleNutrientsAtPoint|AddSoilMoistureAtPoint|AddTileNutrients" \
  scripts/prefabs/farm_plants.lua \
  scripts/components/farming_manager.lua \
  scripts/standardcomponents.lua

rg -n "GetTargetDeltaTemperature|GetInsulation|externalheaterpower|SetNoWetTemperaturePenalty|DESSICANT|DESICCANT" \
  scripts/components/inventoryitemtemperature.lua \
  scripts/components/inventoryitem.lua \
  scripts/prefabs/desiccant.lua \
  scripts/prefabs/trap_fumarole.lua \
  scripts/standardcomponents.lua \
  scripts/tuning.lua
~~~

### `0x62025110` 推荐顺序

先沿 `ACTIONS.EAT.fn` 验证一次食用。
再沿 `Hunger:DoDec` 验证饥饿伤害。
最后沿 `ACTIONS.PLANTSOIL.fn` 验证种子、土壤、作物和世界管理器。

#### `0x62025111` 最小闭环

食物样例用任意带 `edible` 的食物。
农场样例用带 `farmplantable` 的种子和 `farm_soil`。
每条链路都要落到一个组件状态变化或事件推送。
