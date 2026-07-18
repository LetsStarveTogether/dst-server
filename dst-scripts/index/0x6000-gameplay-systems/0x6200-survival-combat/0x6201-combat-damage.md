# `0x62010000` 战斗与伤害

战斗链路要把动作、移动、StateGraph、武器、combat 和 health 分开读。
扣血只发生在 `Health:DoDelta`，但能否命中由更早的动作和距离条件决定。

## `0x62011000` 本页定位

本页解释一次攻击从输入动作到生命变化的最小闭环。
重点是避免把 `ACTIONS.ATTACK`、SG 攻击动画和真实伤害结算混为一谈。

### `0x62011100` 要回答的运行时问题

攻击动作在哪里被声明。
角色如何移动到攻击距离。
动画窗口何时调用 combat。
武器伤害、护甲吸收、特殊伤害和死亡事件在哪里结算。

#### `0x62011110` 源码阅读目标

确认 `ACTIONS.ATTACK.fn` 可以直接调用 `Combat:DoAttack`。
确认玩家 SG 是常见动画路径，不是所有伤害的唯一入口。

##### `0x62011111` 验证点

`Combat:DoAttack` 计算 weapon、projectile、AOE、反伤和命中。
目标的 `Combat:GetAttacked` 再处理护甲、闪避、伤害类型和 `Health:DoDelta`。
`Health:SetVal` 在生命归零时推送 `entity_death` 和 `death`。

## `0x62012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/actions.lua` | `ACTIONS.ATTACK.fn` | 攻击动作执行入口 |
| `scripts/components/locomotor.lua` | `LocoMotor:PushAction` | 追向目标与抵达后推送动作 |
| `scripts/stategraphs/SGwilson.lua` | `ActionHandler(ACTIONS.ATTACK, ...)` | 玩家攻击状态选择 |
| `scripts/components/combat.lua` | `Combat:DoAttack` | 攻击执行和伤害计算 |
| `scripts/components/combat.lua` | `Combat:GetAttacked` | 目标受击和防御处理 |
| `scripts/components/health.lua` | `Health:DoDelta` | 生命变化与死亡事件 |
| `scripts/components/weapon.lua` | `Weapon:GetDamage` | 武器伤害与特殊伤害 |
| `scripts/standardcomponents.lua` | `DoFireDamage` | 通用火焰和状态 helper 的伤害入口 |
| `scripts/stategraphs/SGknight.lua` | `DoJoustAoe` | 骑士冲锋 AOE 与坐骑目标记录 |
| `scripts/prefabs/ruinsnightmare_horn_attack.lua` | `OnUpdate` | 梦魇 horn attack AOE 目标记录 |

### `0x62012110` 主锚点 / `scripts/actions.lua`

`ACTIONS.ATTACK.fn` 先检查若干 SG 状态标签。
普通路径会调用 `act.doer.components.combat:DoAttack(act.target)` 并返回成功。

#### `0x62012111` 搜索信号

搜索 `ACTIONS.ATTACK.fn`、`propattack`、`thrusting`、`helmsplitting` 和 `DoAttack`。

### `0x62012120` 主锚点 / `scripts/components/locomotor.lua`

攻击动作可能先进入 `LocoMotor:PushAction`。
如果距离不足，locomotor 会用 `GoToEntity` 接近目标，再在抵达后推送 buffered action。

#### `0x62012121` 搜索信号

搜索 `PushAction`、`GoToEntity`、`PushBufferedAction`、`PreviewBufferedAction` 和 `in_cooldown`。

### `0x62012130` 主锚点 / `scripts/stategraphs/SGwilson.lua`

玩家攻击动作处理器会根据武器和状态选择 `attack`、`slingshot_shoot`、`blowdart`、`throw` 等状态。
这些状态负责动画窗口和本地连击体验。

#### `0x62012131` 搜索信号

搜索 `ActionHandler(ACTIONS.ATTACK`、`abouttoattack`、`slingshot_shoot`、`blowdart` 和 `throw`。

### `0x62012140` 主锚点 / `scripts/components/combat.lua`

`DoAttack` 决定武器、projectile、电击 stimuli、AOE、反伤和目标受击。
目标组件的 `GetAttacked` 才处理防御、护甲、闪避、planar 和特殊伤害。

#### `0x62012141` 搜索信号

搜索 `DoAttack`、`CalcDamage`、`CalcReflectedDamage`、`GetAttacked`、`onattackother` 和 `onhitother`。

### `0x62012150` 主锚点 / `scripts/components/health.lua`

`Health:DoDelta` 是生命变化入口。
`Health:SetVal` 判断死亡，并推送 world 级 `entity_death` 与实体级 `death`。

#### `0x62012151` 搜索信号

搜索 `DoDelta`、`SetVal`、`entity_death`、`death`、`healthdelta` 和 `CanFadeOut`。

### `0x62012160` 主锚点 / `scripts/standardcomponents.lua`

非武器伤害也可能从标准 helper 进入组件。
例如通用燃烧逻辑会调用 `health:DoFireDamage`，而不是经过 `ACTIONS.ATTACK`。

#### `0x62012161` 搜索信号

搜索 `DoFireDamage`、`DropTarget`、`perishable` 和 `farming_manager`。

## `0x62013000` 运行流程

~~~mermaid
flowchart TD
    A["ACTIONS.ATTACK"]
    A --> B{"距离足够"}
    B -->|否| C["LocoMotor:GoToEntity"]
    C --> D["PushBufferedAction"]
    B -->|是| D
    D --> E["SGwilson attack state"]
    E --> F["ACTIONS.ATTACK.fn"]
    F --> G["Combat:DoAttack"]
    G --> H["Weapon:GetDamage / projectile / AOE"]
    H --> I["target Combat:GetAttacked"]
    I --> J["armor / dodge / damage type / planar"]
    J --> K["Health:DoDelta"]
    K --> L["healthdelta / death / entity_death"]
~~~

### `0x62013110` 动作到移动 / 距离和抵达

攻击不一定立即执行。
当 action 需要接近目标时，`locomotor` 保存 buffered action 并移动到可执行距离。

#### `0x62013111` 边界条件

校验攻击失败时先看距离、目标有效性和 cooldown。
不要直接跳到 `Health:DoDelta`。

### `0x62013210` SG 到 Combat / 动画窗口

玩家常通过 SG 状态进入攻击动画。
状态标签影响本地预测、连击、远程武器和特殊攻击分支。

#### `0x62013211` 边界条件

非玩家实体或 projectile 也可能调用 `Combat:DoAttack` 或 `Combat:GetAttacked`。
因此 SG 不是战斗结算的唯一入口。

### `0x62013310` Combat 到 Health / 攻击方和受击方分工

攻击方的 `DoAttack` 计算基础伤害并调用目标 `combat:GetAttacked`。
受击方的 `GetAttacked` 执行防御、护甲、闪避和特殊伤害结算。
最后由 `health:DoDelta` 改变生命。

#### `0x62013311` 验证点

`GetAttacked` 调用 `health:DoDelta(-damage, ...)`。
`DoDelta` 推送 `healthdelta`。
`SetVal` 在死亡时推送 `entity_death` 和 `death`。

### `0x62013410` AOE 目标记录 / 坐骑与重复命中

部分 AOE 会先把已命中的实体写入 `targets` 表，避免一轮扫描重复处理。
`SGknight.lua` 的 joust AOE 和 `ruinsnightmare_horn_attack.lua` 都依赖坐骑目标记录变量。
读这类逻辑时要同时看 rider mount 分支和普通目标分支，确认写入的是实际命中实体或其 mount。

## `0x62014110` 结构细节 / 武器与伤害 / `weapon.lua` 的伤害接口

`Weapon:SetDamage` 写入武器基础伤害。
`Weapon:GetDamage` 会考虑函数型伤害、`damagetypebonus` 和特殊伤害表。
`Weapon:OnAttack` 是命中后的武器回调。

### `0x62014111` 需要核对的字段

用 `spear.lua` 验证固定伤害。
用 `hambat.lua` 验证 `SetOnAttack(UpdateDamage)`。
用 `slingshotammo.lua` 验证 projectile 与特殊弹药分支。

## `0x62014210` 结构细节 / 死亡与掉落边界 / Health 只负责生命和死亡事件

`health.lua` 不直接决定所有掉落。
掉落通常由 prefab 或 `lootdropper` 监听死亡相关状态和事件。
`LootDropper:ClearChanceLoot()` 提供清空 chance loot 的 helper。
`houndbone.lua` 用 `bonetype` 保存骨头外观，并在类型变化时重建 chance loot。

### `0x62014211` 边界条件

如果要解释战利品，必须跳到具体 prefab 的 `lootdropper` 设置。
不要把 `Health:SetVal` 误写成掉落执行点。

## `0x62015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "ACTIONS\\.ATTACK\\.fn|DoAttack|propattack|thrusting|helmsplitting" \
  scripts/actions.lua

rg -n "PushAction|GoToEntity|PushBufferedAction|PreviewBufferedAction|in_cooldown" \
  scripts/components/locomotor.lua

rg -n "ActionHandler\\(ACTIONS\\.ATTACK|slingshot_shoot|blowdart|throw|abouttoattack" \
  scripts/stategraphs/SGwilson.lua

rg -n "DoAttack|CalcDamage|GetAttacked|CalcReflectedDamage|onhitother|onattackother" \
  scripts/components/combat.lua

rg -n "SetDamage|GetDamage|OnAttack|LaunchProjectile" \
  scripts/components/weapon.lua \
  scripts/prefabs/spear.lua \
  scripts/prefabs/hambat.lua \
  scripts/prefabs/slingshotammo.lua

rg -n "DoDelta|SetVal|entity_death|healthdelta|death" \
  scripts/components/health.lua

rg -n "DoFireDamage|DropTarget|perishable|farming_manager|ClearChanceLoot|bonetype" \
  scripts/standardcomponents.lua \
  scripts/components/lootdropper.lua \
  scripts/prefabs/houndbone.lua

rg -n "DoJoustAoe|ruinsnightmare_horn_attack|components\\.rider|targets\\[" \
  scripts/stategraphs/SGknight.lua \
  scripts/prefabs/ruinsnightmare_horn_attack.lua
~~~

### `0x62015110` 推荐顺序

先读 `ACTIONS.ATTACK.fn`。
再读 `SGwilson.lua` 的攻击 ActionHandler。
然后转到 `combat.lua` 的 `DoAttack` 与 `GetAttacked`。
最后用 `health.lua` 验证生命变化和死亡事件。

#### `0x62015111` 最小闭环

用普通矛攻击作为样例。
从 `spear.lua` 的 `SetDamage` 追到 `Combat:CalcDamage`，再追到目标 `Combat:GetAttacked` 和
`Health:DoDelta`。
