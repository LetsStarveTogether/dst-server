# DST 专用服务器集群架构与配置文件

本文说明 DST 专用服务器的分片架构、配置文件和主要选项。

## 集群架构

> 饥荒联机版专用服务器通常采用分片集群。
> 每个世界都是独立分片，其中唯一的主控分片及其控制的其他分片共同构成一个集群。
> 最常见的“森林 + 洞穴”是双分片集群：两个世界分别运行在独立进程中，森林担任主控。

所谓的“多层世界”一般指分片数量 >2 的集群

本项目把挂载到容器内 `/cluster` 的目录本身作为集群目录，不需要手工指定集群和分片名称。只需确保目录符合以下规则即可：

- 集群目录下必须直接存在 `cluster.ini`（集群配置文件）
- 集群目录下必须直接存在 `cluster_token.txt`（集群认证令牌）
- 集群目录下只可存在两种子目录：
  - `mods` （模组文件夹，非必需，启动后自动生成）
  - 分片配置文件夹，名称无限定（常见为 `Master`、`Caves`）
- 分片配置文件夹下必须存在 `server.ini`（分片配置文件）

宿主机上的集群目录名称不受限制，启动容器时把该目录直接挂载到 `/cluster` 即可。

本项目会启动集群目录下的所有分片，多层世界不需要启动多次容器。

## 配置文件说明

以下内容说明专服配置文件结构及变量含义，注释中的赋值均为默认值。

如果只需要启动服务器，可以跳过本节的详细配置说明。

```text
Cluster_1  # 以集群方式提供服务，地面和洞穴是两个独立的服务器进程
├── cluster.ini  # 集群配置
├── cluster_token.txt  # 集群认证码
├── adminlist.txt  # 管理员名单，克雷的 ID（KU_XXXXXXXX），一行一个
├── blocklist.txt  # 封禁名单，SteamID64，一行一个
├── whitelist.txt  # 特权名单，克雷的 ID（KU_XXXXXXXX），一行一个
├── mods  # 本项目管理的 mod 目录，并映射到游戏安装目录
│   ├── dedicated_server_mods_setup.lua  # mod 下载清单
│   └── ugc  # v2/UGC mod
├── Master  # 主服务器进程（地面）
│   ├── server.ini  # 服务器配置
│   ├── modoverrides.lua  # mod 的设置
│   ├── worldgenoverride.lua  # 可选，用户维护的世界生成与世界设置
│   ├── leveldataoverride.lua  # 可选，游戏管理的完整关卡数据
│   └── save  # 存档
│       └── ...
├── Caves  # 洞穴服务器
│   ├── server.ini  # 服务器配置
│   ├── modoverrides.lua  # mod 的设置
│   ├── worldgenoverride.lua  # 可选，用户维护的世界生成与世界设置
│   ├── leveldataoverride.lua  # 可选，游戏管理的完整关卡数据
│   └── save  # 存档
│       └── ...
```

### 世界配置覆盖文件

每个分片都会独立读取自己的世界配置。
需要自定义的每个分片目录都应分别放置 `worldgenoverride.lua`。

`leveldataoverride.lua` 可以缺失，且主要由游戏管理。
它保存已经展开的完整关卡定义，用于客户端向集群服务端传递设置。
它存在时会整体替换分片已有或默认的关卡数据，因此不能只包含局部配置。
它缺失时，服务端会回退到传入配置或默认关卡数据，并继续加载 `worldgenoverride.lua`。
因此，正常手工配置专服时不需要该文件。

虽然名称沿用至今，`worldgenoverride.lua` 实际上同时控制世界生成和运行时世界设置。
它会在 `leveldataoverride.lua` 之后加载，因此其预设和显式 `overrides` 具有最终优先级。
如果 `worldgen_preset` 和 `settings_preset` 都能成功解析，两者会组成完整配置并整体替换之前的基线。
否则，服务端会把可用的预设数据和显式 `overrides` 合并到当前基线。
必须设置 `override_enabled = true`，否则该文件会被忽略。

标准的“森林 + 洞穴”集群可以缺失两个 `leveldataoverride.lua`，只使用以下两个文件：

`Master/worldgenoverride.lua`：

```lua
return {
    override_enabled = true,
    worldgen_preset = "SURVIVAL_TOGETHER",
    settings_preset = "SURVIVAL_TOGETHER",
    overrides = {},
}
```

`Caves/worldgenoverride.lua`：

```lua
return {
    override_enabled = true,
    worldgen_preset = "DST_CAVE",
    settings_preset = "DST_CAVE",
    overrides = {},
}
```

已启用的 `worldgenoverride.lua` 可能在存档时被重写以同步当前 `overrides`，因此应先停止分片再编辑。
该文件只会配置已存在的分片，不会创建或启用分片。
洞穴分片仍需要自己的目录和有效的 `server.ini`，并且必须在 `cluster.ini` 中启用 `[SHARD] / shard_enabled`。

### `cluster.ini`

```ini
[MISC]
; 要保留的最大快照数量。
; 这些快照在每次保存时都会被创建，并在 "主机游戏 "屏幕的 "回滚 "标签中可用。
; max_snapshots = 6

; 允许在服务器运行的命令提示符或终端中输入 lua 命令。
; console_enabled = true


[SHARD]
; 启用服务器分片。
; 对于多级服务器，这必须被设置为 "true"。
; 对于单级服务器，它可以被省略。
; shard_enabled = false

; 这是主服务器将监听的网络地址，供其他分片服务器连接使用。
; 如果你的集群中的所有服务器都在同一台机器上，则将其设置为 127.0.0.1 ；
; 如果你的集群中的服务器在不同的机器上，则设置为 0.0.0.0 。
; 这只需要为主服务器设置，可以在 cluster.ini 或主服务器的 server.ini 中设置。
; 可在 server.ini 中重写
; bind_ip = 127.0.0.1

; 非主控分片在试图连接到主控分片时将使用这个 IP 地址。
; 如果集群中的所有服务器都在同一台机器上，将其设置为 127.0.0.1 。
; 可在 server.ini 中重写
; master_ip =


; 这是主服务器将监听的 UDP 端口，非主分片在试图连接到主分片时将使用这个端口。
; 这应该通过在 cluster.ini 中的条目为所有分片设置相同的值，或者完全省略以使用默认值。
; 这必须与运行在与主控分片相同机器上的任何分片上的 server_port 设置不同。
; 可在 server.ini 中重写
; master_port = 10888

; 这是一个用于验证从属服务器与主服务器的密码。
; 如果你在不同的机器上运行需要相互连接的服务器，这个值在每台机器上必须是相同的。
; 对于在同一台机器上运行的服务器，你可以只在 cluster.ini 中设置。
; 可在 server.ini 中重写
; cluster_key =


[STEAM]
; 当设置为 "true "时，服务器将只允许属于 steam_group_id 设置中指定的 steam 组的玩家连接。
; steam_group_only = false

; steam_group_only / steam_group_admins 设置相关的 steam 组 ID。
; steam_group_id = 0

; 当这个设置为 "true "时，在 steam_group_id 中指定的 steam 组的管理员也将在服务器上拥有管理员身份。
; steam_group_admins = false


[NETWORK]
; 创建一个离线集群。
; 该服务器不会被公开列出，只有本地网络上的玩家能够加入，任何与 steam 有关的功能都会失效。
; offline_cluster = false

; 这是服务器每秒钟向客户提供更新数据的次数。
; 增加这个次数可以提高精度，但会消耗更大的网络带宽。
; 建议将其保持在默认值 15 。
; 建议你只在局域网游戏中改变这个选项，并使用一个能被 60 除以的数字（15、20、30）。
; tick_rate = 15

; 为白名单上的玩家保留的空位数量。
; 要将一个玩家列入白名单，请将他们的 Klei UserId 添加到 whiteelist.txt 文件中（将此文件与 cluster.ini 放在同一个目录中）。
; 仅可用于主分片的 cluster.ini
; whitelist_slots = 0

; 这是玩家加入你的服务器时必须输入的密码。
; 留空或省略它表示没有密码。
; 仅可用于主分片的 cluster.ini
; cluster_password =

; 你的服务器集群的名称。
; 这是将显示在服务器列表中的名称。
; 仅可用于主分片的 cluster.ini
; cluster_name =

; 集群描述。
; 这将显示在 "浏览游戏 "界面中的服务器信息区域。
; 仅可用于主分片的 cluster.ini
; cluster_description =

; 当设置为 "true "时，服务器将只接受来自同一局域网内机器的连接。
; 仅可用于主分片的 cluster.ini
; lan_only_cluster = false

; 当这个选项被设置为 false 时，游戏将不再在每天结束时自动保存。
; 游戏仍然会在关机时保存，并且可以使用 c_save() 手动保存。
; autosaver_enabled = true

; 集群的语言
; 中文 zh
; cluster_language = en

[GAMEPLAY]
; 可以同时连接到集群的最大玩家数量。
; 仅可用于主分片的 cluster.ini
; max_players = 16

; 玩家间战斗（队友伤害）
; pvp = false

; 集群的游戏模式。
; 这个字段相当于 "创建游戏 "界面中的 "游戏模式 "字段。
; 常见有效值如下（不包含括号及括号中内容）：
; survival  （生存）
; endless  （无尽）
; wilderness  （荒野）
; 对于熔炉、暴食等 mod，这里可能需要设置独立的值，请参考对应 mod 说明。
; game_mode = survival

; 当没有玩家连接时，暂停服务器。
; pause_when_empty = false

; 设置为 "true"，以启用投票功能。
; vote_enabled = true
```

### `server.ini`

```ini
[SHARD]
; 设置一个分片为集群的主分片。
; 每个集群必须有一个主服务器。
; 在主服务器的 server.ini 中设置为 true ;
; 在其他所有的 server.ini 中设置为 false 。
; is_master =

; 这是将在日志文件中显示的分片名称。
; 它对于主服务器来说是被忽略的，主服务器的名字总是叫 [SHDMASTER] 。
; name =

; 这个字段是为非主服务器自动生成的，并在内部用来唯一地识别一个服务器。
; 如果任何人的角色目前处于在这个服务器所管理的世界中，改变这个字段或删除它可能会产生问题。
; id =


[STEAM]
; steam 使用的内部端口。
; 请确保你在同一台机器上运行的每台服务器都是不同的。
; authentication_port = 8766

; steam 使用的内部端口。
; 请确保你在同一台机器上运行的每台服务器都是不同的。
; master_server_port = 27016


[NETWORK]
; 该服务器将监听连接的 UDP 端口。
; 如果你正在运行一个多级集群，对同一台机器上的多个服务器，这个端口各不相同。
; 这个端口必须在 10998 和 11018 之间（含），以便同一局域网的玩家在他们的服务器列表中看到它。
; 在某些操作系统上，低于 1024 的端口限制为只能特权用户使用。
; server_port = 10999


[ACCOUNT]
; 是否对用户存档路径进行编码。
; 若设为 true，直接以 Klei ID 作为路径，需要文件系统支持区分大小写。
; 若设为 false，玩家数据使用编码后的文件夹名，不依赖文件系统的大小写特性。
; encode_user_path = false
```

### `dedicated_server_mods_setup.lua`

```lua
-- 两个减号表示本行内容为注释，不会被执行
-- 有两个函数用于安装模组，ServerModSetup 和 ServerModCollectionSetup。
-- 该脚本将在启动时执行，下载指定的 mod 到 mods 目录。
-- ServerModSetup 参数为 模组创意工坊编号 的 字符串。
    -- 模组或合计对应的创意工坊页面，其网址末尾的数字就是编号。
    -- 示例模组 https://steamcommunity.com/sharedfiles/filedetails/?id=351325790
 -- ServerModSetup("351325790")
    -- 示例合集 https://steamcommunity.com/sharedfiles/filedetails/?id=2594933855
 -- ServerModCollectionSetup("2594933855")
```

## 补充说明

`cluster.ini` 保存整个集群共用的配置，`server.ini` 保存单个分片的配置。

一个选项同时出现在两个文件中时，`server.ini` 覆盖 `cluster.ini`，适合给特定分片设置不同端口或 tick rate。

同一主机上的每个分片必须使用不同的 `server_port`、`authentication_port` 和 `master_server_port`。

旧资料中的 `settings.ini` 已经拆分为 `cluster.ini` 和 `server.ini`，新部署不应再创建它。

`[NETWORK] / cluster_intention` 用于设置服务端列表中的玩法倾向，常见值为 `cooperative`、`competitive`、`social` 和 `madness`。

跨主机部署时，各主机的 `cluster.ini` 必须保持集群级参数一致；主分片监听 `bind_ip = 0.0.0.0`，其他分片通过 `master_ip` 连接主分片。

## 参考资料

- [Klei Forum：Dedicated Server Settings Guide](https://forums.kleientertainment.com/forums/topic/64552-dedicated-server-settings-guide/)
- [Klei Forum：Understanding Shards and Migration Portals](https://forums.kleientertainment.com/forums/topic/59174-understanding-shards-and-migration-portals/)
- [Klei Forum：2022 Updated Dedicated Server Quick Setup Guide - Linux](https://forums.kleientertainment.com/forums/topic/140715-2022-updated-dedicated-server-quick-setup-guide-linux/)
- [Klei Forum：`worldgenoverride.lua` with the post-Caves settings](https://forums.kleientertainment.com/forums/topic/53014-worldgenoverridelua-with-the-new-post-caves-settings/)
- [Klei Forum：`worldgenoverride.lua` settings for the March QoL update](https://forums.kleientertainment.com/forums/topic/127830-worldgenoverridelua-settings-for-march-qol-update/)
- [Klei Forum：`leveldataoverride.lua` 与 `worldgenoverride.lua`](https://forums.kleientertainment.com/forums/topic/150248-leveldataoverride-worldgenoverride-and-world-settings-picker-mod-oh-my/)
