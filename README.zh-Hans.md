# 饥荒专服容器镜像

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

![STEAM 商店头图](https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/322330/header_schinese.jpg?t=1736195686)

## 镜像地址 quay.io/wh2099/dst-server

## 关键特性

- 饥荒联机版（Don't Starve Together）
- 专用服务器（Dedicated Server）
- 容器镜像（Container Image）
- STEAM 平台（Steam Platform）
- 64 位 Linux 系统（64-bit Linux System）
- **极简纯净（[KISS 原则](https://zh.wikipedia.org/wiki/KISS%E5%8E%9F%E5%88%99)）**

## 首次安装

1. 请确保服务器已经安装了容器运行时环境，比如 [Podman](https://docs.podman.io/en/latest/index.html)
2. 创建用于存储 **存档数据** 的路径

    ```shell
    mkdir -p "${HOME}/cluster"
    ```

    - 可自定义，与启动容器命令中的内容同步修改即可

3. 上传配置文件夹

   参照官方说明 [Configure and download the server settings](https://forums.kleientertainment.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/#:~:text=3.%20Configure%20and%20download%20the%20server%20settings)，将下载的压缩文件解压，获得*配置文件夹*，将*配置文件夹* **整个** 上传到*存储路径*（上一步提到的）
   - 配置文件夹具体名称不做要求，只要确保*存储路径*下的子文件夹唯一即可（常见名称：“Cluster_1”、“MyDediServer”）
   - 想自定义世界的话，可以用客户端存档中的配置文件覆盖服务端的对应文件（参见后文 [配置文件说明](#配置文件说明)）
   - 请注意 cluster_token.txt 为专服独有，且必须通过 [官方管理页面](https://accounts.klei.com/account/game/servers?game=DontStarveTogether) 获取

4. 拉取镜像并启动容器

    ```shell
    sudo podman run --name dst -d --network host -v "${HOME}/cluster:/cluster" quay.io/wh2099/dst-server:latest
    ```

## 维护

### 基础命令

本质上就是常用的容器控制命令

- 关闭（会自动存档） `podman stop dst`
- 启动（非首次） `podman start dst`
- 重启 `podman restart dst`
- 查看日志 `podman logs dst`

### 游戏控制台命令

可通过集群（分片）文件夹下的命名管道文件 `console` 向服务端发送控制台命令，如：

```shell
echo 'c_announce("服务器需要维护，请大家提前做好准备")' > "Cluster_1/console"
```

部分常用的控制台命令：

- 重新加载世界 `c_reset()`
- 重新生成世界 `c_regenerateworld()`
- 手动存档 `c_save()`
- 关闭世界而不保存 `c_shutdown(false)`
- 发送公告 `c_announce("这里是公告内容")`
- 查看玩家 `c_listallplayers()`
- 解锁科技 `c_freecrafting()`
- ……

### 集群架构
>
> 饥荒联机版专用服务器的常用架构是分片集群。每个世界都是独立的分片，同时有唯一的分片作为主控，主控及其控制的所有分片共同构成一个集群。以最常见的“森林 + 洞穴”为例，其本质是双分片集群，森林与洞穴都是独立启动的进程，森林为主控。

所谓的“多层世界”一般指分片数量 >2 的集群

本项目在启动服务端时，会自动识别配置目录（即挂载到容器内 `/cluster` 的目录）结构，不需要手动指定具体的集群及分片名称。只需确保配置目录符合以下规则即可：

- 配置目录下只能有唯一的集群配置文件夹，名称无限定（常见为 `Cluster_1`）
- 集群配置文件夹下必须存在 `cluster.ini`（集群配置文件）
- 集群配置文件夹下必须存在 `cluster_token.txt` （集群认证令牌）
- 集群配置文件夹下只可存在两种文件夹：
  - `mods` （模组文件夹，非必需，启动后自动生成）
  - 分片配置文件夹，名称无限定（常见为 `Master`、`Caves`）
- 分片配置文件夹下必须存在 `server.ini`（分片配置文件）

请注意，本项目将启动所有存在配置文件夹的分片，故而多层世界无需多层启动，整合所有分片配置到集群下即可

## TODO

- [ ] Python 入口
  - [x] 服务端作为受控子进程
  - [ ] IPC
    - [x] 通过标准输入输出
    - [ ] `-cloudserver`
  - [ ] 事件消息
    - [ ] 游戏天数变动
    - [ ] 玩家进入
    - [ ] 玩家离开
    - [ ] 玩家死亡
    - [ ] 玩家发言

## 配置文件说明

*专服配置文件结构及变量含义，注释中的赋值均为默认值。*

干货内容，看不懂跳过就好

```text
Cluster_1  # 以集群方式提供服务，地面和洞穴是两个独立的服务器进程
├── cluster.ini  # 集群配置
├── cluster_token.txt  # 集群认证码
├── adminlist.txt  # 管理员名单，克雷的 ID（KU_XXXXXXXX），一行一个
├── blocklist.txt  # 封禁名单，SteamID64，一行一个
├── whitelist.txt  # 特权名单，克雷的 ID（KU_XXXXXXXX），一行一个
├── Master  # 主服务器进程（地面）
│   ├── server.ini  # 服务器配置
│   ├── modoverrides.lua  # mod 的设置
│   ├── leveldataoverride.lua  # 世界（地形、生物等）的设置
│   └── save  # 存档
│       └── ...
├── Caves  # 洞穴服务器
│   ├── server.ini  # 服务器配置
│   ├── modoverrides.lua  # mod 的设置
│   ├── leveldataoverride.lua  # 世界（地形、生物等）的设置
│   └── save  # 存档
│       └── ...

mods  # mod 相关，在游戏服务端安装目录下
└── dedicated_server_mods_setup.lua  # mod 加载设定
```

### cluster.ini

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

### server.ini

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

### dedicated_server_mods_setup.lua

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

## 参考信息

1. [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)
2. [Dedicated Server Quick Setup Guide - Linux](https://forums.kleientertainment.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/)
