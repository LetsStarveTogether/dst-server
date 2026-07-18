# `0x33020000` Replica 与 Classified

Replica 与 Classified 页解释 server component 如何投影只读状态、预测状态和私有可见状态。

源码中没有 scripts/replica 目录。

可复制组件的实现放在 `scripts/components/*_replica.lua`。

classified 则是 `scripts/prefabs/*_classified.lua` 下的一组隐藏网络 prefab。

## `0x33021111` 本页定位 / 要回答的运行时问题 / Component 如何获得 Replica / 验证点

`EntityScript:AddComponent(name)` 先加载 server component 模块。

然后它调用 `self:ReplicateComponent(name)`。

最后它才构造 server component 实例，并把实例保存到 `self.components[name]`。

如果 `name` 在 `REPLICATABLE_COMPONENTS` 中，`ReplicateComponent` 会加载 `components/name_replica.lua`。

server 和 client 都可能拥有 replica 对象。

因此 replica 不是纯客户端对象。

## `0x33021121` 本页定位 / 要回答的运行时问题 / 标签如何驱动客户端重建 / 验证点

master sim 上的 `ReplicateComponent` 会给实体加 `_"..name` 标签。

client 初始反序列化标签后，`EntityScript:ReplicateEntity()` 会扫描 `_name` 和 `__name` 标签。

扫描命中后，它再调用 `ReplicateComponent(name)` 补建 replica。

`ValidateReplicaComponent(name, cmp)` 也依赖 `_name` 标签过滤可见 replica。

## `0x33021131` 本页定位 / 要回答的运行时问题 / Classified 在哪里介入 / 验证点

classified 是隐藏网络 prefab。

它通常带 `Network`、`CLASSIFIED` 标签和 netvars。

它也可以像 `writeable_classified` 一样只承载私有可见性和绑定关系。

classified 不是所有 replica 的必经层。

它常用于玩家私有、高频、大量字段，或只应对特定 owner 可见的容器与物品状态。

## `0x33022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/entityscript.lua` | `AddComponent`、`RemoveComponent` | 添加或移除 server component，并触发复制/取消复制 |
| `scripts/entityreplica.lua` | `REPLICATABLE_COMPONENTS` | 固定列出内建可复制组件 |
| `scripts/entityreplica.lua` | `ReplicateComponent` | 添加 `_name` 标签并构造 `components/name_replica.lua` |
| `scripts/entityreplica.lua` | `UnreplicateComponent` | 移除 `_name` 标签并留下 `__name` 标记 |
| `scripts/entityreplica.lua` | `TryAttachClassifiedToReplicaComponent` | 把 classified 交给已有 replica 的 `AttachClassified` |
| `scripts/netvars.lua` | `net_event`、`GetIdealUnsignedNetVarForCount` | 解释 netvar 类型和事件脉冲语义 |
| `scripts/components/health_replica.lua` | `AttachClassified`、`SetCurrent` | player classified 状态读取样例 |
| `scripts/components/inventory_replica.lua` | `AttachClassified` | inventory classified 状态表样例 |
| `scripts/components/container_replica.lua` | `Network:SetClassifiedTarget` | 容器按 opener 暴露 classified 的样例 |
| `scripts/prefabs/player_classified.lua` | `OnEntityReplicated` | 玩家私有状态总线和 replica 批量绑定 |
| `scripts/prefabs/inventory_classified.lua` | `OnEntityReplicated` | inventory replica 的大型私有状态表 |

### `0x33022111` Replica 锚点 / `scripts/entityreplica.lua` / 搜索信号

`REPLICATABLE_COMPONENTS` 包含 19 个组件。

这些组件与 `scripts/components/*_replica.lua` 一一对应。

| 组 | 组件 |
| --- | --- |
| 动作与制作 | `builder`、`combat`、`constructionsite` |
| 容器与物品 | `container`、`inventory`、`inventoryitem`、`stackable`、`writeable` |
| 装备与工具 | `equippable`、`fishingrod`、`oceanfishingrod` |
| 角色状态 | `health`、`hunger`、`moisture`、`rider`、`sanity`、`sheltered` |
| 关系与命名 | `follower`、`named` |

`AddReplicableComponent(name)` 允许 mod 把额外组件加入同一集合。

### `0x33022121` Replica 锚点 / `_name` 与 `__name` / 搜索信号

`ReplicateComponent` 在 master sim 添加 `_name` 标签。

如果实体已经有 `__name` 标签，它会删除 `__name` 并返回。

这个分支表示组件带 prereplicated 或 unreplicated 标记，不应直接构造 replica。

`UnreplicateComponent` 在 master sim 上移除 `_name`，再添加 `__name`。

`PrereplicateComponent` 等价于先复制再取消复制。

玩家 prefab 就利用手工 `_health`、`_hunger`、`_sanity` 等标签提前暴露 replica 形状。

### `0x33022211` Classified 锚点 / 绑定协议 / 搜索信号

常见 classified prefab 会保存 `_parent`。

client 侧 `OnEntityReplicated` 会调用父实体的 `TryAttachClassifiedToReplicaComponent`。

如果目标 replica 暂时不存在，classified 会临时挂回 `parent.<name>_classified`。

对应 replica 构造时会检查这个字段并重新 `AttachClassified`。

这个双向兜底解释了为什么绑定不要求严格的构造顺序。

### `0x33022221` Classified 锚点 / 可见性协议 / 搜索信号

`Network:SetClassifiedTarget(target)` 决定 classified 的目标可见性。

`inventory_replica.lua`、`container_replica.lua` 和 `inventoryitem_replica.lua` 会根据 opener、owner 或 item 状态切换 target。

`player_classified` 直接挂在玩家实体下，承担本地玩家和服务器写入的状态总线。

物品专用 classified 则常由具体 item prefab 生成并绑定到 owner 或目标实体。

## `0x33023000` 运行流程

~~~mermaid
flowchart TD
    A["server prefab fn"]
    A --> B["inst:AddComponent(name)"]
    B --> C["EntityScript:ReplicateComponent(name)"]
    C --> D["添加 _name 标签"]
    C --> E["构造 components/name_replica.lua"]
    E --> F["server component 可写 inst.replica.name"]
    D --> G["网络标签同步"]
    F --> H["netvars 或 classified 同步"]
    G --> I["client ReplicateEntity"]
    I --> J["client 构造 name_replica"]
    H --> K{"是否有 classified"}
    K -->|否| L["replica 直接读 netvars/标签/server component"]
    K -->|是| M["OnEntityReplicated 或构造兜底 AttachClassified"]
    M --> N["replica 读取 classified 字段"]
~~~

### `0x33023111` 普通 Replica 路径 / Server 上也有 Replica / 边界条件

`AddComponent` 在构造 server component 前复制组件。

因此 server component 的构造函数和 setter 可以立即写 `inst.replica.<name>`。

`components/health.lua` 会把 health 当前值、最大值和 penalty 写入 `inst.replica.health`。

`components/combat.lua` 也会通过 `inst.replica.combat` 走共享判断。

这类写法不是 client-only API。

### `0x33023121` 普通 Replica 路径 / Replica 可以没有 Classified / 边界条件

部分 `_replica.lua` 文件直接声明 netvars。

例如 `combat_replica.lua`、`moisture_replica.lua`、`named_replica.lua` 和 `stackable_replica.lua`。

部分 replica 主要读 server component 或标签。

例如 `health_replica.lua` 在本地 host 上优先读 `inst.components.health`。

没有 classified 不代表没有 replica。

### `0x33023211` Classified 路径 / `player_classified` / 边界条件

`player_classified` 添加 `Transform`、`MapExplorer`、`Network` 和 `CLASSIFIED` 标签。

它声明 health、hunger、sanity、builder、combat、rider、SG、locomotor、HUD、camera、frontend 等字段。

`OnEntityReplicated` 会先调用父实体 `AttachClassified`。

随后它会尝试把 classified 附到多个 replica component。

玩家相关 server component 也会直接写 `inst.player_classified` 字段。

### `0x33023221` Classified 路径 / Inventory 与 Container / 边界条件

`inventory_classified` 和 `container_classified` 是大型槽位状态表。

`inventory_replica.lua` 在 server 上会生成 `inventory_classified` 并监听 active item、slot 和 equip 事件。

`container_replica.lua` 在 server 上会生成 `container_classified` 并按 opener 切换可见目标。

两者都通过 classified 把高频槽位状态投影到可见客户端。

### `0x33023231` Classified 路径 / Item 与 Construction / 边界条件

`inventoryitem_replica.lua` 会生成 `inventoryitem_classified`。

它用 classified 传递 image、atlas、owner、deploy mode、moisture、temperature、recharge 等物品状态。

`constructionsite_replica.lua` 会生成 `constructionsite_classified`。

它用 classified 传递建造槽位数量，并按 builder 设置 classified target。

`writeable_replica.lua` 会生成 `writeable_classified`，但该 classified 本身没有 netvar 字段。

## `0x33024111` Classified 谱系 / 完整列表 / `scripts/prefabs/*_classified.lua` / 覆盖清单

`scripts/prefabs` 下有 15 个 `*_classified.lua` 文件。

这些文件都位于 `scripts/prefabs`。

| 文件 | 主要模式 |
| --- | --- |
| `scripts/prefabs/player_classified.lua` | 玩家私有状态总线 |
| `scripts/prefabs/inventory_classified.lua` | inventory replica 槽位与 RPC 转发状态表 |
| `scripts/prefabs/container_classified.lua` | container replica 槽位与 opener 可见状态表 |
| `scripts/prefabs/inventoryitem_classified.lua` | item image、owner、deploy、耐久和温度状态 |
| `scripts/prefabs/constructionsite_classified.lua` | construction slot count 状态 |
| `scripts/prefabs/writeable_classified.lua` | writeable replica 私有绑定信号 |
| `scripts/prefabs/container_closed_receiveitem_classified.lua` | closed container 接收物品的短生命周期事件 |
| `scripts/prefabs/attunable_classified.lua` | attunable player/source 关系 |
| `scripts/prefabs/pet_hunger_classified.lua` | 宠物 hunger 和 HUD 状态 |
| `scripts/prefabs/woby_commands_classified.lua` | Woby 命令和背包状态 |
| `scripts/prefabs/wx78_classified.lua` | WX78 能量、模块、护盾和 UI 状态 |
| `scripts/prefabs/lucy_classified.lua` | Lucy 私有说话状态 |
| `scripts/prefabs/shadow_battleaxe_classified.lua` | shadow battleaxe 私有说话状态 |
| `scripts/prefabs/voidcloth_scythe_classified.lua` | voidcloth scythe 私有说话状态 |
| `scripts/prefabs/wagpunkhat_classified.lua` | wagpunk hat 私有说话状态 |

## `0x33024211` Classified 谱系 / 不同模式 / 大型状态表 / 验证点

`player_classified`、`inventory_classified` 和 `container_classified` 是大型状态表。

它们包含大量 netvars、dirty event 和转发函数。

文档应把它们当作状态总线，而不是普通 FX prefab。

## `0x33024221` Classified 谱系 / 不同模式 / 组件附属状态 / 验证点

`inventoryitem_classified`、`constructionsite_classified` 和 `writeable_classified` 由对应 replica 管理。

这类 classified 的生命周期跟随父实体或父 replica。

client 侧可能先收到 classified，再等 replica 构造时重新 attach。

## `0x33024231` Classified 谱系 / 不同模式 / 专用私有状态 / 验证点

`pet_hunger_classified`、`woby_commands_classified`、`wx78_classified` 和武器说话 classified 都是专用状态。

它们不说明所有 replica 的通用结构。

阅读时应回到对应 prefab 或 component 的生成点。

## `0x33025111` Netvars 与事件 / `scripts/netvars.lua` / 类型口径 / 验证点

`netvars.lua` 注释列出 `net_bool`、`net_byte`、`net_shortint`、`net_ushortint`、`net_float` 和 `net_hash` 等类型。

`net_hash` 可以接收字符串并转为 hash。

`net_entity` 保存实体实例。

`GetIdealUnsignedNetVarForCount` 会按最大数量选择合适的 unsigned netvar 类型。

## `0x33025211` Netvars 与事件 / `net_event` / 事件脉冲 / 验证点

`net_event` 是 `net_bool` 的包装。

`push()` 会切换布尔值来触发同名 dirty event。

它适合一次性事件，不适合长期状态。

因此 `buildevent`、`attackedpulseevent`、`learnrecipeevent` 这类字段应解释为事件脉冲。

## `0x33026111` 覆盖口径 / 完整覆盖在哪里 / Reference 清单 / 边界条件

`components/*_replica.lua` 的完整路径由 `0x8202-component-catalog.md` 覆盖。

`prefabs/*_classified.lua` 的完整路径由 `0x8201-prefab-catalog.md` 覆盖。

本页只维护 19 个内建 replica 名称和 15 个 classified 文件的概念清单。

## `0x33026211` 覆盖口径 / 不应写入的结论 / 常见误读 / 验证点

不要写 scripts/replica，因为源码没有这个目录。

不要把 classified 写成所有 replica 的必经层。

不要把 replica 写成纯 client 对象。

不要忽略 `__name` 标签。

不要把 `net_event` 当作普通布尔状态。

## `0x33027100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "function EntityScript:AddComponent|ReplicateComponent|ReplicateEntity|TryAttachClassified" \
  scripts/entityscript.lua \
  scripts/entityreplica.lua

rg -n "AttachClassified|OnEntityReplicated|net_|SetClassifiedTarget|TryAttachClassifiedToReplicaComponent" \
  scripts/components/health_replica.lua \
  scripts/components/inventory_replica.lua \
  scripts/components/container_replica.lua \
  scripts/prefabs/player_classified.lua \
  scripts/prefabs/inventory_classified.lua \
  scripts/prefabs/container_classified.lua
~~~

### `0x33027111` 推荐顺序 / 最小闭环

先读 `EntityScript:AddComponent`，确认复制发生在 server component 构造前。

再读 `entityreplica.lua`，确认 `_name`、`__name` 和 `components/name_replica.lua` 的关系。

然后用 `health.lua` 到 `health_replica.lua` 验证 server 写 replica 的路径。

再用 `inventory_replica.lua` 到 `inventory_classified.lua` 验证大型 classified 状态表。

最后用 `container_replica.lua` 验证 `Network:SetClassifiedTarget` 的可见性切换。
