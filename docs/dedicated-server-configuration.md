# DST 专用服务器集群架构与配置

本文说明当前 Podman 部署的集群拓扑、配置文件职责和必须保持的运行边界。

## 当前部署架构

推荐部署为每个集群一个 Pod、每个分片一个容器。

所有分片容器共享 Pod 网络命名空间，并把同一个宿主机集群目录挂载到 `/cluster`。

主分片容器运行 `dst-server master`，在一个 daemon 中组合集群 Controller 与主分片 Agent。

次分片容器运行 `dst-server serve <shard>`，每个 Agent 只管理自己的 DST 子进程。

次分片 Agent 通过 Pod 内部 Unix socket 注册到 Controller。

Controller 等待配置中声明的全部 Agent 后，统一准备配置和 Mod，再启动所有分片。

公开的集群 RPC socket 位于 `/cluster/.dst-server.sock`，并以 `0600` 权限创建。

## 集群目录

宿主机目录名不受 DST 限制，因为容器始终把选定目录直接挂载到 `/cluster`。

下面以常见的森林和洞穴集群为例：

```text
cluster/
├── cluster.ini
├── cluster_token.txt
├── adminlist.txt
├── blocklist.txt
├── whitelist.txt
├── console
├── mods/
│   ├── dedicated_server_mods_setup.lua
│   ├── modsettings.lua
│   └── ugc/
├── Master/
│   ├── server.ini
│   ├── modoverrides.lua
│   ├── worldgenoverride.lua
│   ├── leveldataoverride.lua
│   └── save/
└── Caves/
    ├── server.ini
    ├── modoverrides.lua
    ├── worldgenoverride.lua
    ├── leveldataoverride.lua
    ├── console
    └── save/
```

`cluster.ini` 和 `cluster_token.txt` 必须直接位于集群根目录。

每个分片目录必须包含 `server.ini`，且整个集群必须恰好有一个主分片。

`mods` 是唯一不被视为分片的子目录，其他子目录都会按分片发现并要求存在 `server.ini`。

世界覆盖文件是可选配置，准备阶段会补齐缺失的权限名单和 Mod 支持文件。

Agent 激活分片时会创建对应的 `console` FIFO。

主分片的 `console` 位于集群根目录，次分片的 `console` 位于各自的分片目录。

受管配置文件和目录不能是符号链接。

### 文件职责

| 文件 | 作用 |
| --- | --- |
| `cluster.ini` | 保存整个集群共享的网络、分片、玩法和访问设置。 |
| `cluster_token.txt` | 保存 Klei 集群令牌。 |
| `adminlist.txt` | 保存管理员 Klei User ID，每行一个。 |
| `blocklist.txt` | 保存封禁标识符，每行一个。 |
| `whitelist.txt` | 保存白名单 Klei User ID，每行一个。 |
| `<shard>/server.ini` | 保存分片身份、玩家端口和 Steam query 端口。 |
| `<shard>/worldgenoverride.lua` | 保存该分片的世界生成与运行时世界设置。 |
| `<shard>/leveldataoverride.lua` | 保存可选的完整关卡基线。 |
| `<shard>/modoverrides.lua` | 保存该分片启用的 Mod 及其选项。 |
| `mods/dedicated_server_mods_setup.lua` | 声明需要下载的 Workshop 项或合集。 |
| `mods/modsettings.lua` | 保存整个游戏安装共享的原生 Mod 行为。 |
| `mods/ugc` | 保存 v2/UGC Mod 内容。 |

## `cluster.ini`

以下选项是当前部署最常用的集群级设置。

未写入的值继续由游戏默认值决定。

| Section | Option | 默认值 | 作用 |
| --- | --- | --- | --- |
| `MISC` | `max_snapshots` | `6` | 保留的最大快照数量。 |
| `MISC` | `console_enabled` | `true` | 允许执行服务端 Lua 命令。 |
| `MISC` | `mods_enabled` | `true` | 启用 Mod 加载逻辑。 |
| `SHARD` | `shard_enabled` | `false` | 启用多分片协议，多分片集群必须为 `true`。 |
| `SHARD` | `bind_ip` | `127.0.0.1` | 主分片用于分片协议的监听地址。 |
| `SHARD` | `master_ip` | 无 | 次分片连接主分片时使用的地址。 |
| `SHARD` | `master_port` | `10888` | 分片协议使用的共享 UDP 端口。 |
| `SHARD` | `cluster_key` | 无 | 分片间共享的认证密钥。 |
| `NETWORK` | `cluster_name` | 无 | 服务器列表中显示的集群名称。 |
| `NETWORK` | `cluster_description` | 空 | 服务器列表中显示的集群描述。 |
| `NETWORK` | `cluster_password` | 无 | 玩家加入集群时使用的密码。 |
| `NETWORK` | `offline_cluster` | `false` | 创建不依赖在线服务的局域网集群。 |
| `NETWORK` | `lan_only_cluster` | `false` | 只接受同一局域网中的玩家。 |
| `NETWORK` | `tick_rate` | `15` | 每秒向客户端发送更新的次数，配置范围为 `1–60`。 |
| `NETWORK` | `autosaver_enabled` | `true` | 在每天结束时自动保存。 |
| `NETWORK` | `whitelist_slots` | `0` | 为白名单玩家保留的槽位数量。 |
| `NETWORK` | `cluster_language` | `en` | 服务器列表使用的游戏 locale code。 |
| `NETWORK` | `connection_timeout` | `8000` | RakNet 连接超时，单位为毫秒。 |
| `NETWORK` | `internet_broadcasting_enabled` | `true` | 向互联网广播房间。 |
| `NETWORK` | `idle_timeout` | `1800` | 无玩家活动时的超时，单位为秒。 |
| `GAMEPLAY` | `max_players` | `16` | 集群允许的最大玩家数量。 |
| `GAMEPLAY` | `pvp` | `false` | 允许玩家间伤害。 |
| `GAMEPLAY` | `game_mode` | `survival` | 内置模式为 `survival`、`lavaarena` 和 `quagmire`，Mod 可注册其他值。 |
| `GAMEPLAY` | `pause_when_empty` | `false` | 无玩家时暂停世界。 |
| `GAMEPLAY` | `vote_enabled` | `true` | 启用玩家投票。 |

同一 Pod 内的分片通常使用 `master_ip = 127.0.0.1`。

多分片集群的所有分片必须使用同一个非空 `cluster_key` 和同一个 `master_port`。

`master_port` 不能与任何分片的玩家端口或 Steam query 端口重复。

`game_mode` 接受任意非空字符串，因此可以使用 Mod 注册的自定义模式。

读取旧配置中的 `endless` 或 `wilderness` 时，SDK 会发出 `FutureWarning`，但仍原样写回以保证旧文件 round-trip。

新配置应保留 `game_mode = survival`，并通过世界配置表达模式。

无尽森林的 `worldgen_preset` 和 `settings_preset` 均为 `ENDLESS`。

无尽洞穴保留 `DST_CAVE` preset，关键 override 为：

```lua
overrides = {
    basicresource_regrowth = "always",
    ghostsanitydrain = "none",
    portalresurection = "always",
    resettime = "none",
}
```

荒野森林的 `worldgen_preset` 和 `settings_preset` 均为 `WILDERNESS`。

荒野洞穴保留 `DST_CAVE` preset，关键 override 为：

```lua
overrides = {
    spawnmode = "scatter",
    basicresource_regrowth = "always",
    ghostsanitydrain = "none",
    ghostenabled = "none",
    resettime = "none",
}
```

## `server.ini`

以下选项决定单个分片的身份和端口。

| Section | Option | 默认值 | 作用 |
| --- | --- | --- | --- |
| `SHARD` | `is_master` | `true` | 标记唯一的主分片。 |
| `SHARD` | `name` | 无 | 设置次分片在日志和分片协议中的名称。 |
| `SHARD` | `id` | 无 | 设置分片 ID，主分片只能为 `1`，次分片从 `2` 开始。 |
| `SHARD` | `bind_ip` | 无 | 覆盖集群级分片监听地址。 |
| `SHARD` | `master_ip` | 无 | 覆盖集群级主分片地址。 |
| `SHARD` | `master_port` | 无 | 覆盖集群级分片协议端口。 |
| `SHARD` | `cluster_key` | 无 | 覆盖集群级分片认证密钥。 |
| `STEAM` | `master_server_port` | `27016` | Steam 服务器列表、A2S 查询和 heartbeat 使用的 UDP 端口。 |
| `NETWORK` | `server_port` | `10999` | 玩家连接该分片时使用的 UDP 端口。 |
| `ACCOUNT` | `encode_user_path` | `false` | 对用户存档路径进行编码。 |

多分片集群必须在每个 `server.ini` 中显式写出 `is_master`。

每个次分片必须有名称，所有显式分片 ID 必须唯一。

同一个 Pod 中所有 `server_port` 和 `master_server_port` 都必须唯一。

Pod 发布宿主机端口时，容器仍监听 `server.ini` 中的内部端口，并通过 `-external_port` 公告外部玩家端口。

旧 `server.ini` 中的 `authentication_port` 仅为读取兼容而接受；SDK 会丢弃该值，且不会生成或映射此端口。

## 世界配置

每个分片独立读取自己的世界配置。

`leveldataoverride.lua` 是可选的完整关卡基线，存在时会整体替换此前的关卡数据。

`worldgenoverride.lua` 在关卡基线之后加载，并同时控制世界生成和运行时世界设置。

`worldgenoverride.lua` 必须包含 `override_enabled = true` 才会生效。

标准森林分片可以使用：

```lua
return {
    override_enabled = true,
    worldgen_preset = "SURVIVAL_TOGETHER",
    settings_preset = "SURVIVAL_TOGETHER",
    overrides = {},
}
```

标准洞穴分片可以使用：

```lua
return {
    override_enabled = true,
    worldgen_preset = "DST_CAVE",
    settings_preset = "DST_CAVE",
    overrides = {},
}
```

`lavaarena` 和 `quagmire` 需要每个分片同时具有完整的事件关卡基线。

配置 SDK 的内置事件 preset 会生成所需的 `leveldataoverride.lua` 和世界 override。

游戏可能在存档时重写已启用的世界 override，因此应停止所有分片后再编辑或保存这些文件。

## Mod 配置

`dedicated_server_mods_setup.lua` 只声明下载内容，不会启用 Mod。

`modoverrides.lua` 在单个分片中启用或禁用 Mod，并保存该分片的配置选项。

`modsettings.lua` 保存强制启用、错误处理和本地 Mod 警告等安装级行为。

准备阶段会把各分片显式启用的 `workshop-<id>` 补入共享下载清单。

每个配置 revision 首次准备时，Controller 会统一更新一次所需 Mod，然后才启动分片。

普通分片启动和自动重试都使用 `-skip_update_server_mods`，不会重复更新 Mod。

显式更新 Mod 时必须先停止整个集群，再依次调用 `update_mods()` 和 `start()`。

## 配置 SDK 与运行时写入

`ClusterConfig` 表示完整配置树，并可通过 `load(path)` 和 `save(path)` 读取或生成上述文件。

`ForestOverrides`、`CaveOverrides` 和事件 override 为官方世界键提供强类型校验。

Mod 私有选项使用经过 Lua literal 校验的普通映射，不需要为每个 Mod 建立模型。

`RoomPreset` 是可组合的稀疏房间片段，最终通过 `build()` 生成普通 `ClusterConfig`。

直接保存会输出 canonical 文件内容，因此不会保留原注释或排版。

直接保存按文件执行同目录原子替换，但文件系统不提供整个配置树的跨文件事务。

运行中的 Pod 应通过 `ClusterClient.read_configuration()` 取得配置及 revision。

`ClusterClient.save_configuration()` 使用 expected revision 防止覆盖外部并发修改。

部署中的配置只允许在所有 DST 子进程停止后写入。

运行时写入不能改变分片目录集合、主分片身份、`server_port` 或 `master_server_port`，因为这些值决定 Quadlet 拓扑和 Pod 端口映射。

需要改变部署拓扑时，应重新生成配置和 Quadlet，再重启对应 Pod。

token 只接受可打印的非空格 ASCII，并且 `cluster.ini`、`server.ini` 与 token 文件以 `0600` 权限创建。

## 参考资料

- [Klei：Dedicated Server Settings Guide](https://forums.kleientertainment.com/forums/topic/64552-dedicated-server-settings-guide/)
- [Klei：Dedicated Server Command Line Options Guide](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)
- [Klei：Understanding Shards and Migration Portals](https://forums.kleientertainment.com/forums/topic/59174-understanding-shards-and-migration-portals/)
- [Klei：`leveldataoverride.lua` 与 `worldgenoverride.lua`](https://forums.kleientertainment.com/forums/topic/150248-leveldataoverride-worldgenoverride-and-world-settings-picker-mod-oh-my/)
