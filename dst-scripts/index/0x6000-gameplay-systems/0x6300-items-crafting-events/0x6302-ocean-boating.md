# `0x63020000` 海洋与船

海洋与船页把船看成可移动平台、物理实体和配件集合。
不要把船当作普通地面，也不要只从 `boat.lua` 的 prefab 名称阅读。

## `0x63021111` 本页定位 / 要回答的运行时问题 / 船如何承载玩家和物品 / 验证点

核心链路是 `boat.lua` 装配 `walkableplatform` 与 `boatphysics`。
`walkableplatform` 维护玩家和物品是否在平台上。
`boatphysics` 维护速度、转向、桅杆推力、锚和其他拖拽源。
玩家自身通过 `walkableplatformplayer` 检测上船和下船。

## `0x63021211` 本页定位 / 与世界模拟页的边界 / 本页只追实体和组件 / 不展开 Map/ocean 生成

海洋 tile、世界生成和洞穴/海面布局在 `0x500000` 区域解释。
本页只解释玩家已经在运行时世界中和船、海洋配件、海洋制作物交互时的 Lua 链路。

## `0x63022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/recipes.lua` | `boat_item` / `anchor_item` | 海洋物品配方 |
| `scripts/prefabs/boat.lua` | `create_common_pre` / `create_master_pst` | 船体装配 |
| `scripts/components/walkableplatform.lua` | `SetEntitiesOnPlatform` | 平台承载关系 |
| `scripts/components/walkableplatformplayer.lua` | `TestForPlatform` | 玩家上下平台 |
| `scripts/entityscript.lua` | `GetCurrentPlatform` | 实体所属平台 |
| `scripts/components/boatphysics.lua` | `BoatPhysics` | 船速、转向和阻力 |
| `scripts/components/boatcrew.lua` | `RemoveMember` | 船员集合、pirate ship 数据和 sleep 清理 |
| `scripts/components/boatleak.lua` | `SetState` | 船体漏水修补状态 |
| `scripts/components/mast.lua` | `SetBoat` / `CalcMaxVelocity` | 桅杆推力 |
| `scripts/components/anchor.lua` | `SetBoat` | 锚绑定船 |
| `scripts/components/boatdrag.lua` | `drag` / `sailforcemodifier` | 拖拽源参数 |
| `scripts/prefabs/anchor.lua` | `SGanchor` | 锚 prefab 与状态机 |
| `scripts/prefabs/mast.lua` | `mast` component | 桅杆 prefab 与升级件 |

### `0x63022111` 船体锚点 / `scripts/prefabs/boat.lua` / 搜索信号

`create_common_pre` 添加 network、physics、`walkableplatform`、`healthsyncer`、`waterphysics`、
`reticule` 和 `boatringdata`。
`create_master_pst` 在 master sim 上添加 `hull`、`repairable`、`boatring`、`hullhealth`、
`boatphysics`、`boatdrifter`、`health` 和 `SGboat`。

### `0x63022211` 平台锚点 / `walkableplatform` 与 `walkableplatformplayer` / 搜索信号

`WalkablePlatform:SetEntitiesOnPlatform` 周期性扫描平台半径内实体，并调用
`EntityScript:AddPlatformFollower`。
`WalkablePlatformPlayer:TestForPlatform` 在 server 侧用 `inst:GetCurrentPlatform()`，在 client 侧用
`TheWorld.Map:GetPlatformAtPoint(...)`。

## `0x63023000` 运行流程

~~~mermaid
flowchart TD
    A["recipes.lua: boat_item / anchor_item / mast_item"]
    A --> B["builder.lua: SpawnPrefab(product)"]
    B --> C["boat.lua: create_common_pre"]
    C --> D["walkableplatform"]
    C --> E["boatphysics"]
    D --> F["walkableplatformmanager"]
    D --> G["walkableplatformplayer: TestForPlatform"]
    E --> H["mast.lua: AddMast / CalcMaxVelocity"]
    E --> I["anchor.lua + boatdrag.lua: AddBoatDrag"]
    H --> J["boatphysics: GetMaxVelocity / OnUpdate"]
    I --> J
~~~

### `0x63023111` 船体生成 / 从制作产物到 Prefab / 边界条件

`recipes.lua` 中 `boat_item`、`boat_grass_item`、`anchor_item`、`mast_item` 等仍走普通制作链。
制作产物进入世界后，deploy kit 或 prefab 自己负责把实际 boat、anchor、mast 放到目标点。
因此海洋物品要同时核对 recipe、deploy kit 和最终 prefab。

### `0x63023211` 平台承载 / `AddPlatformFollower` / 边界条件

`EntityScript:GetCurrentPlatform` 在 master sim 上读 `self.platform`，在 client 上读引擎层
`entity:GetPlatform()`。
保存位置时，`EntityScript:GetSaveRecord` 会把平台相对坐标和 `walkableplatform:GetUID()` 写入记录。
所以船上实体的保存、移动和下船都不能按普通 world position 简化。

### `0x63023311` 船体运动 / `BoatPhysics` / 验证点

`BoatPhysics` 维护 `velocity_x`、`velocity_z`、`masts`、`magnets` 和 `boatdraginstances`。
`ApplyRowForce` 处理划船力。
`GetMaxVelocity` 汇总桅杆和磁力源。
`GetBoatDrag` 与 `GetTotalAnchorDrag` 汇总锚和拖拽组件。
`SetHalting` 是 `walkableplatform` 在坏位置或障碍场景下使用的安全刹车，不是通用玩法接口。

### `0x63023321` 船员与修补 / Boatcrew 与 BoatLeak / 验证点

`Boatcrew:RemoveMember` 在最后一个成员离开时会从 `piratespawner` 移除 ship data。
它也会从船实体移除 `vanish_on_sleep` 与 `boatcrew`。
`boatleak.lua` 的 `repaired_tape` 状态会使用 Winona tape 修补音效。

## `0x63024111` 结构细节 / 锚和桅杆 / 配件如何影响船 / 需要核对的函数

`components/mast.lua` 的 `Mast:SetBoat` 会在原船上 `RemoveMast`，再在目标船上 `AddMast`。
`Mast:CalcMaxVelocity` 和 `Mast:GetCurrentSailForce` 决定对 `BoatPhysics` 的速度贡献。
`components/anchor.lua` 会用 `inst:GetCurrentPlatform()` 找到船。
`prefabs/anchor.lua` 添加 `anchor`、`boatdrag` 和 `SGanchor`，其中 `boatdrag` 负责拖拽、最大速度修正和帆力修正。

## `0x63024211` 结构细节 / 平台玩家 / `player_common.lua` / 需要核对的组件

玩家在 `scripts/prefabs/player_common.lua` 中添加 `walkableplatformplayer`。
上船时 `GetOnPlatform` 会设置 `Transform:SetIsOnPlatform(true)`，并把玩家注册到
`platform.components.walkableplatform:AddPlayerOnPlatform`。
离船时 `GetOffPlatform` 会移除玩家、停止船镜头和音乐检测，并清空 `self.platform`。

## `0x63024311` 结构细节 / 禁止船上建造 / `NoBoats_testfn` / 搜索信号

`scripts/recipes.lua` 的 `NoBoats_testfn` 会检查目标点是否在船或不可用平台上。
部分 scaffold、Winters Feast table、deconstruction recipe 会使用它。
这说明“海洋限制”并不只在 boat 组件里，还会进入 recipe placement test。

## `0x63025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "boat_item|anchor_item|mast_item|NoBoats_testfn" \
  scripts/recipes.lua

rg -n "create_common_pre|create_master_pst|walkableplatform|boatphysics|SGboat" \
  scripts/prefabs/boat.lua

rg -n "SetEntitiesOnPlatform|AddEntityToPlatform|GetOnPlatform|TestForPlatform|GetCurrentPlatform" \
  scripts/components/walkableplatform.lua \
  scripts/components/walkableplatformplayer.lua \
  scripts/entityscript.lua

rg -n "AddMast|RemoveMast|AddBoatDrag|ApplyRowForce|GetMaxVelocity|SetHalting" \
  scripts/components/boatphysics.lua \
  scripts/components/mast.lua \
  scripts/components/anchor.lua \
  scripts/components/boatdrag.lua

rg -n "RemoveMember|RemoveShipData|repaired_tape|ChangeToRepaired" \
  scripts/components/boatcrew.lua \
  scripts/components/boatleak.lua
~~~

### `0x63025111` 推荐顺序 / 最小闭环

先从 `scripts/prefabs/boat.lua` 看船体装配。
再读 `walkableplatform` 和 `walkableplatformplayer`，确认玩家和物品如何挂到平台。
最后读 `boatphysics`、`mast`、`anchor` 与 `boatdrag`，确认配件如何改变速度和刹车状态。
