# `0x22030000` 网络运行时

网络页把 server 权威组件、replica、classified、会话序列化和 shard 同步放在同一条链路里读。

本页只解释 Lua 可见的网络结构，不尝试展开 C++ 网络管理器。

## `0x22031111` 本页定位 / 要回答的运行时问题 / 为什么 Client 读 `inst.replica` / 验证点

client 侧动作判断经常读取 `inst.replica`。

server 侧权威状态仍然保存在真实 component 中。

`entityreplica.lua` 用 `_component` 和 `__component` tag 决定 client 是否创建 replica component。

## `0x22031211` 本页定位 / Classified 的位置 / 私有或高频状态 / 验证点

`mainfunctions.lua` 和 `networking.lua` 都会检查 `player.player_classified`。

classified 不是普通 component。

它常作为网络实体附着到玩家相关的 replica 读取路径。

## `0x22032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `ReplicateEntity` | 由 guid 找 `Ents` 并调用实体复制 |
| `scripts/entityscript.lua` | `self.replica` | 实体初始化时创建 replica 容器 |
| `scripts/entityscript.lua` | `actionreplica` | 用 net byte array 同步动作组件标记 |
| `scripts/entityreplica.lua` | `ReplicateComponent` | 加载 `components/*_replica.lua` |
| `scripts/entityreplica.lua` | `ReplicateEntity` | client 初始反序列化后复制所有标记组件 |
| `scripts/entityreplica.lua` | `AddReplicableComponent` | mod 可扩展可复制组件清单 |
| `scripts/netvars.lua` | `net_event` | Lua 侧网络变量说明和事件包装 |
| `scripts/netvars.lua` | `GetIdealUnsignedNetVarForCount` | 按计数范围选择整数 netvar |
| `scripts/networking.lua` | `SerializeUserSession` | 玩家保存时附带 classified entity |
| `scripts/networking.lua` | `SerializeWorldSession` | 世界保存时交给 `TheNet` 的会话序列化 |
| `scripts/shardnetworking.lua` | `Shard_UpdateWorldState` | shard world settings 与状态同步 |
| `scripts/shardnetworking.lua` | `Shard_WorldSave` | 跨 shard 触发世界保存 |
| `scripts/shardnetworking.lua` | shard transaction helpers | vote、boss、merm 与 shard transaction 同步 |

### `0x22032111` 主锚点 / `scripts/entityreplica.lua` / 搜索信号

先搜索 `REPLICATABLE_COMPONENTS`。

再搜索 `ReplicateComponent`，确认 replica 文件名由 `name .. "_replica"` 组成。

### `0x22032211` 实体锚点 / `scripts/entityscript.lua` / 搜索信号

搜索 `self.replica = { _ = {}, inst = self }`。

这个表使用 metatable 访问内部 `_` 容器。

### `0x22032311` 动作锚点 / `actionreplica` / 搜索信号

搜索 `actioncomponents`、`inherentactions` 和 `modactioncomponents`。

这些字段通过 net byte array 同步动作候选所需的组件标记。

### `0x22032411` Netvar 锚点 / `scripts/netvars.lua` / 搜索信号

搜索 `netvar:set` 和 `netvar:value` 的注释。

该文件明确要求 server 和 client 对同一实体声明一致的 netvar，否则实体反序列化会失败。

## `0x22033000` 运行流程

~~~mermaid
flowchart TD
    A["server component"]
    A --> B["EntityScript:ReplicateComponent"]
    B --> C["add _component tag"]
    C --> D["client deserializes tags"]
    D --> E["EntityScript:ReplicateEntity"]
    E --> F["require components/name_replica"]
    F --> G["inst.replica.name"]
~~~

### `0x22033111` Server 标记阶段 / `_component` / 边界条件

`ReplicateComponent` 只接受 `REPLICATABLE_COMPONENTS` 中的名称。

server 侧会添加 `_"..name` tag。

如果实体已经有 `__"..name` tag，会移除该 tag 并提前返回。

### `0x22033211` Client 构造阶段 / `components/*_replica.lua` / 边界条件

client 初始反序列化 tag 后触发 `EntityScript:ReplicateEntity()`。

它遍历 `REPLICATABLE_COMPONENTS`，只为带 `_name` 或 `__name` tag 的组件创建 replica。

### `0x22033311` Classified 附着阶段 / `TryAttachClassifiedToReplicaComponent` / 边界条件

classified 可以附着到已经 pre-replicated 或 unreplicated 的组件。

如果 replica component 不存在，附着会失败并返回 `false`。

### `0x22033411` Shard 同步阶段 / `shardnetworking.lua` / 边界条件

shard 同步不通过普通 replica component 表达。

`Shard_UpdateWorldState` 接收世界设置与状态，并把它们同步到本 shard。

`Shard_WorldSave` 用于跨 shard 触发世界保存。

同文件还处理 portal 状态、投票、boss、merm 和 shard transaction 等跨世界消息。

## `0x22034111` 结构细节 / Replica 容器 / `self.replica._` / 需要核对的字段

真实 replica 对象放在 `self.replica._[name]`。

外部通常通过 `inst.replica.inventory` 这样的形式读取。

## `0x22034211` 结构细节 / 动作复制 / `actionreplica` / 需要核对的字段

`actionreplica` 保存动作组件、固有动作和 mod 动作组件的网络数组。

动作收集页需要同时读 `componentactions.lua` 和这里的 net 字段。

## `0x22034311` 结构细节 / 会话序列化 / `SerializeUserSession` / 需要核对的字段

玩家会话保存会调用 `player:GetSaveRecord()`。

server 侧会把 `player.player_classified.entity` 传入 `TheNet:SerializeUserSession()`。

## `0x22034411` 结构细节 / Netvar 类型选择 / `GetIdealUnsignedNetVarForCount` / 需要核对的字段

计数小于等于 `7` 时返回 `net_tinybyte`。

计数小于等于 `63` 时返回 `net_smallbyte`。

超过 `net_uint` 范围时返回 `nil`。

## `0x22034511` 结构细节 / Shard 消息 / Connected Shards 与 Transaction / 需要核对的字段

`shardnetworking.lua` 维护 connected shard、portal 和 server worldgen data。

它不是 component replica 的替代层，而是世界之间的消息总线。

排查洞穴、地表或多 shard 行为时，应同时读 `networking.lua` 和 `shardnetworking.lua`。

## `0x22035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "REPLICATABLE_COMPONENTS|ReplicateComponent|ReplicateEntity" \
  scripts/entityreplica.lua \
  scripts/entityscript.lua \
  scripts/netvars.lua \
  scripts/mainfunctions.lua \
  scripts/networking.lua
rg -n "actionreplica|player_classified|SerializeUserSession" \
  scripts/entityreplica.lua \
  scripts/entityscript.lua \
  scripts/netvars.lua \
  scripts/mainfunctions.lua \
  scripts/networking.lua
rg -n "SerializeWorldSession|Shard_UpdateWorldState|Shard_WorldSave|ShardPortals|Shard_CreateTransaction" \
  scripts/networking.lua \
  scripts/shardnetworking.lua
~~~

### `0x22035111` 推荐顺序 / 最小闭环

先读 `entityreplica.lua` 顶部的组件白名单。

再读 `EntityScript:ReplicateComponent` 的 tag 写入。

最后读 `EntityScript:ReplicateEntity` 的 client 构造路径。

如果问题跨地表和洞穴，再追加 `shardnetworking.lua` 的 world state、portal 和 transaction 路径。
