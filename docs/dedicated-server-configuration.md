# DST 专用服务器集群架构与配置文件

本文说明 DST 专用服务器的分片架构、配置文件和主要选项。

## 集群架构

> 饥荒联机版专用服务器通常采用分片集群。
> 每个世界都是独立分片，其中唯一的主控分片及其控制的其他分片共同构成一个集群。
> 最常见的"森林 + 洞穴"是双分片集群：两个世界分别运行在独立进程中，森林担任主控。

所谓的"多层世界"一般指分片数量 >2 的集群

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
├── adminlist.txt  # 管理员标识符名单，一行一个，空行忽略
├── blocklist.txt  # 封禁标识符名单，一行一个，空行忽略
├── whitelist.txt  # 特权标识符名单，一行一个，空行忽略
├── mods  # 本项目管理的 mod 目录，并映射到游戏安装目录
│   ├── dedicated_server_mods_setup.lua  # mod 下载清单
│   ├── modsettings.lua  # 游戏的强制启用与 Mod 行为设置，可由 SDK 生成
│   └── ugc  # v2/UGC mod
├── Master  # 主服务器进程（地面）
│   ├── server.ini  # 服务器配置
│   ├── modoverrides.lua  # mod 的设置
│   ├── worldgenoverride.lua  # 可选，用户维护的世界生成与世界设置
│   ├── leveldataoverride.lua  # 可选，游戏管理或事件启动所需的完整关卡数据
│   └── save  # 存档
│       └── ...
├── Caves  # 洞穴服务器
│   ├── server.ini  # 服务器配置
│   ├── modoverrides.lua  # mod 的设置
│   ├── worldgenoverride.lua  # 可选，用户维护的世界生成与世界设置
│   ├── leveldataoverride.lua  # 可选，游戏管理或事件启动所需的完整关卡数据
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

`QUAGMIRE` 和 `LAVAARENA` 是例外。
服务端会先读取完整关卡数据，再读取用户 override；缺少事件基线时会在应用事件 preset 前因找不到默认世界而终止。
SDK 的两个内置事件 preset 因此生成强类型的完整基线，随后仍由 `worldgenoverride.lua` 应用官方事件 preset。
熔炉和暴食不存在独立的 `forgegenoverride.lua`；它们分别使用 `LAVAARENA` 和 `QUAGMIRE` 的 Level、Worldgen 与 MOD 配置。

虽然名称沿用至今，`worldgenoverride.lua` 实际上同时控制世界生成和运行时世界设置。
它会在 `leveldataoverride.lua` 之后加载，因此其预设和显式 `overrides` 具有最终优先级。
如果 `worldgen_preset` 和 `settings_preset` 都能成功解析，两者会组成完整配置并整体替换之前的基线。
否则，服务端会把可用的预设数据和显式 `overrides` 合并到当前基线。
必须设置 `override_enabled = true`，否则该文件会被忽略。

标准的"森林 + 洞穴"集群可以缺失两个 `leveldataoverride.lua`，只使用以下两个文件：

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

; 使用替代垃圾回收实现。
; use_alternate_gc = false

; 启用全部 Mod 加载逻辑。
; 设为 false 会跳过 modoverrides.lua 和 modsettings.lua 的处理。
; mods_enabled = true


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
; 命令行 -master_port 可覆盖该值；常规部署应优先写入配置文件。
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
; cluster.ini 接受 1–60；较低值会由服务端量化为实际网络更新频率。
; tick_rate = 15

; 为白名单上的玩家保留的空位数量。
; 要将一个玩家列入白名单，请将他们的 Klei UserId 添加到 whitelist.txt 文件中（将此文件与 cluster.ini 放在同一个目录中）。
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

; 集群语言必须使用游戏登记的 locale code。
; 可用值为 en、fr、es、mex、tr、de、it、pt、pl、ru、ko、zh、zht、zhr。
; cluster_language = en

; RakNet 连接超时，单位为毫秒，只接受非负整数。
; connection_timeout = 8000

; 是否向互联网广播房间。
; internet_broadcasting_enabled = true

; 无玩家活动时的服务器空闲超时，单位为秒，只接受非负整数。
; idle_timeout = 1800

; 自定义 DNS 字符串。
; build 747465 的 Linux 专服只会告警并忽略非空值，此项主要用于完整读取和回写官方配置。
; override_dns =

[GAMEPLAY]
; 可以同时连接到集群的最大玩家数量。
; 仅可用于主分片的 cluster.ini
; max_players = 16

; 玩家间战斗（队友伤害）
; pvp = false

; 运行时游戏模式；内置值为 survival、lavaarena 和 quagmire，Mod 可注册其他值。
; endless 和 wilderness 已废弃；SDK 读取时告警但原样回写。
; 游戏将旧值转为 survival 并注入以下覆盖，显式 worldgenoverride.lua 最终优先：
; endless: basicresource_regrowth=always, ghostsanitydrain=none,
;          portalresurection=always, resettime=none
; wilderness: spawnmode=scatter, basicresource_regrowth=always,
;             ghostsanitydrain=none, ghostenabled=none, resettime=none
; 新建无尽/荒野配置保持 survival；地表改用 worldgen_preset="ENDLESS", settings_preset="ENDLESS"
; （WILDERNESS 同理）；洞穴保留 DST_CAVE preset，并在 overrides 中显式写入对应设置。
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

; bind_ip、master_ip、master_port 和 cluster_key 可以在这里覆盖 cluster.ini 的同名 [SHARD] 选项。


[STEAM]
; 旧 Steamworks 认证端口；当前专服仍解析但不监听。
; 玩家认证改由 Steamworks 内部连接完成，没有合并到 master_server_port。
; SDK 读取旧配置时丢弃该项，不会生成或映射此端口。
; authentication_port = 8766

; Steam 服务器列表、A2S 查询和 heartbeat 使用的 UDP 端口。
; 同一台机器上的每个服务器必须不同。
; 只有需要公网 Steam A2S 或服务器浏览器查询时才需要发布此端口。
; master_server_port = 27016


[NETWORK]
; 该服务器将监听连接的 UDP 端口。
; 如果你正在运行一个多级集群，对同一台机器上的多个服务器，这个端口各不相同。
; 这个端口必须在 10998 和 11018 之间（含），以便同一局域网的玩家在他们的服务器列表中看到它。
; 在某些操作系统上，低于 1024 的端口限制为只能特权用户使用。
; -external_port 不改变该监听端口，只覆盖向 Klei 大厅公告的端口。
; 容器内外端口不同时，应把宿主机映射端口传给 -external_port。
; server_port = 10999


[ACCOUNT]
; 是否对用户存档路径进行编码。
; 原生 INI 缺省值为 false，但当前 Linux 专服创建新世界后会自动改为 true。
; 已有世界不会经过新世界自动启用逻辑，需要稳定启用时应显式设置 true。
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

## Python 配置 SDK

配置模型由 `dst_server.cluster` 导出，并同时支持预览全部生成内容、保存单个分片和级联保存整个集群。

下面的示例生成一个带洞穴、世界覆盖和 Workshop Mod 的完整集群：

```python
from pathlib import Path
from pydantic import SecretStr

from dst_server.cluster import (
    CaveOverrides,
    Cluster,
    ClusterConfig,
    ClusterSettings,
    CustomPreset,
    ForestOverrides,
    ModOverride,
    ModOverrides,
    ModSettings,
    ShardConfig,
    ShardSettings,
    WorkshopDownloads,
    WorldgenOverride,
)


cluster = ClusterConfig(
    settings=ClusterSettings(
        cluster_name="SDK example",
        master_ip="127.0.0.1",
        cluster_key=SecretStr("replace-with-a-random-secret"),
    ),
    shards={
        "Master": ShardConfig(
            settings=ShardSettings(is_master=True, id=1),
            world=WorldgenOverride.forest(
                overrides=ForestOverrides(world_size="huge", day="longday"),
            ),
            mods=ModOverrides(
                entries={
                    "workshop-351325790": ModOverride(
                        enabled=True,
                        configuration_options={"option_name": "value"},
                    ),
                },
            ),
        ),
        "Caves": ShardConfig(
            settings=ShardSettings(
                is_master=False,
                name="Caves",
                id=2,
                master_server_port=27017,
                server_port=11000,
            ),
            world=WorldgenOverride.cave(
                overrides=CaveOverrides(world_size="medium"),
            ),
        ),
    },
    token=SecretStr("replace-with-cluster-token"),
    adminlist="KU_12345678\n",
    whitelist="KU_87654321\n",
    blocklist="76561198000000000\n",
    downloads=WorkshopDownloads(collections=frozenset({2594933855})),
    mod_settings=ModSettings(disable_local_mod_warning=True),
)

preview: dict[Path, str] = cluster.files()
written: tuple[Path, ...] = cluster.save(Path("/srv/dst/Cluster_1"))
```

`ShardConfig.save(path)` 生成一个分片的 `server.ini`、`modoverrides.lua`、可选的 `worldgenoverride.lua` 和 `leveldataoverride.lua`。

`ClusterConfig.save(path)` 会先重新校验整个拓扑和端口，再生成集群文件、全部分片文件、Mod 下载清单、权限名单与 UGC 目录。

`adminlist`、`whitelist` 和 `blocklist` 是原样写入对应文本文件的换行分隔字符串；未显式设置时缺失文件生成为空、已有文件保持不变，显式传入空字符串则清空文件。

权限名单允许 LF 与空行，但拒绝会被原生解析器保留或截断的 CR 和 NUL。

集群 token 仅接受游戏不会过滤的可打印非空格 ASCII 字符，即 `[!-~]`。

模型暴露当前游戏默认值，但 INI 渲染只写调用方显式提供的字段，因此省略值继续由游戏决定，显式传入默认值则仍会落盘。

`WorldgenOverride.forest(overrides=ForestOverrides(...))` 和 `WorldgenOverride.cave(overrides=CaveOverrides(...))` 分别约束森林与洞穴的官方键和值，并补齐对应的官方预设。

省略的世界字段不会写入 `overrides`，显式提供的 `"default"` 会原样保留。

模型还单独覆盖六个不出现在官方配置 UI、但会被地图生成器消费的内部拓扑键。

布尔拓扑键是 `has_ocean`、`keep_disconnected_tiles`、`no_joining_islands` 和 `no_wormholes_to_disconnected_tiles`。

另外两个键是 `layout_mode` 和 `wormhole_prefab`。

`LevelDataOverride` 覆盖 Lua `Level` 的完整顶层元数据。
它按 `forest`、`cave`、`quagmire` 和 `lavaarena` 自动选择对应的 override 类型。

未知 MOD location 使用 `CustomWorldOverrides`，不需要为具体 MOD 建立专用模型，但仅接受可安全序列化的 Lua 标量。

MOD 注入的森林或洞穴世界选项可通过 `ForestOverrides`、`CaveOverrides` 或它们的子类增加更严格的字段类型。

Mod 注册的 task set、start location、preset 和 prefab 名称也应在该子类中显式扩展字段类型。

向 `forest()` 或 `cave()` 传入动态 preset ID 时必须使用 `CustomPreset("MY_MOD_PRESET")`，从而让静态检查区分内建值与运行期注册值。

世界 override 值只接受字符串、布尔值和有限数字标量，因为游戏无法可靠合并或回写嵌套 table。

Lua 数字中的整数限制为 `±(2**53-1)`，需要保持精确的更大编号应使用字符串。

`world=None` 表示不生成并保留已有 `worldgenoverride.lua`，显式的 `WorldgenOverride(enabled=False)` 才会写入禁用状态。

`level=None` 时 SDK 不生成、覆盖或删除 `leveldataoverride.lua`。
只有显式 `level`（包括两个内置事件 preset）会管理该文件。

读取器兼容 Klei 写出的 `KLEI     1` 文件头、Lua 字符串续行、V1 分组表和旧 `preset` 别名，保存时统一输出当前形式。

历史 `ServerModSetup("")` 空占位在读取时按空清单处理，保存时不再输出。

`configuration_options` 直接接收普通映射，不需要为每个 Mod ID 定义专属模型。

映射会递归校验为 Lua literal，并拒绝 `None`、非有限数字、超出安全范围的整数及其他不可序列化值。

`ModOverride.enabled=True` 会在当前分片启用 Mod，`False` 会禁用，省略则不发出启停操作。

专服启动时会先禁用普通 Mod，因此省略 `enabled` 本身不等于启用或保留历史启用状态。

`ModSettings` 完整覆盖五项原生设置。

前三项是 `force_enabled`、`debug_print` 和 `mod_errors`。

后两项是 `disable_mod_disabling` 和 `disable_local_mod_warning`。

前三项分别生成 `ForceEnableMod`、`EnableModDebugPrint` 和 `EnableModError` 调用。

后两项分别生成 `DisableModDisabling` 和 `DisableLocalModWarning` 调用。

`force_enabled` 会强制启用 Mod，并优先于 `ModOverride.enabled=False`。

省略某个 `configuration_options` 项会保留 Mod 自己的持久值或默认值。

游戏会优先从 `saved_server` 读取服务端持久 Mod 配置。

因此已有 `mod_config_data/modconfiguration_*` 可能遮蔽同名 override。

这是游戏原生优先级，SDK 不会静默删除这些持久数据。

已启用的 `workshop-<id>` 会自动进入下载清单，合集 ID 通过 `WorkshopDownloads.collections` 显式提供。

本地 Mod 不会被误当作 Workshop 项。

纯数字 `modoverrides.lua` 名称可能先匹配本地目录，因此 SDK 不会自动把它推断为 Workshop 下载项。

依赖游戏的纯数字 Workshop fallback 时，应同时在 `WorkshopDownloads.items` 中显式声明 ID。

更新已有目录时，未显式传入的 token、权限名单、`mod_settings` 与已有 `modsettings.lua` 会保留。

显式传入 `mod_settings=ModSettings()` 会用空文件清除 SDK 可表达的五项设置。

保留已有 `modsettings.lua` 时，SDK 也会合并保留 setup 文件中可静态识别的 item 和 collection，避免丢失强制启用 Mod 的下载依赖。

`dedicated_server_mods_setup.lua` 只声明下载内容，不会启用任何 Mod。

运行时 `mods.prepare()` 会原样保留已有 setup Lua，并在文件开头补充缺少的静态 `ServerModSetup` 调用；若文件带 shebang，则补充在首行之后。

`ClusterConfig.save()` 会生成完整的 canonical setup 文件。

下载项来自 `WorkshopDownloads`、`ModSettings.force_enabled` 与各分片显式启用的 Workshop Mod。

`files()` 返回包括权限名单在内的确定性配置文件映射，`ugc` 目录则由 `save()` 创建。

`ClusterSettings.load(path)` 和 `ShardSettings.load(path)` 可以严格读取已有 INI。

未知 section、未知 option 或非 `true`/`false` 的布尔值会被拒绝，避免与原生解释结果分叉。

`ShardConfig.load(path)` 会读取单个分片的 `server.ini`、可选世界 override 和 Mod override。

`ClusterConfig.load(path)` 会读取集群 INI、token、三份权限名单和全部已发现分片。

它还会读取根 `mods` 目录中的下载与 Mod settings 文件。

内建 location 和 preset 通常可自动判别。
歧义配置或需要更严格的 MOD 字段时，分别通过 `level_overrides_types` 和 `world_overrides_types` 指定 `WorldOverrides` 子类。

Level 与稀疏 Worldgen 的类型注册表彼此独立，SDK 不会用完整 Level 模型去猜测 Worldgen 字段。

非空 `configuration_options` 无需额外注册类型。

加载后的映射不可变；使用 `ModOverride.replace(configuration_options={...})` 替换选项后即可随配置树保存。

```python
path = Path("/srv/dst/Cluster_1")
loaded = ClusterConfig.load(
    path,
    level_overrides_types={
        "Master": ForestOverrides,
        "Caves": CaveOverrides,
    },
    world_overrides_types={
        "Master": ForestOverrides,
        "Caves": CaveOverrides,
    },
)
edited = loaded.replace(
    settings=loaded.settings.replace(pvp=True),
    adminlist="KU_12345678\nKU_87654321\n",
)
edited.save(path)
```

所有配置模型不可变，`replace()` 只复制显式字段、应用变更并重新执行完整类型与拓扑校验。

Lua 配置读取器只接受受管文件所需的静态 literal table 和白名单调用，动态表达式或未知调用会被拒绝。

读取后再保存保持配置语义和默认省略行为，但会输出 canonical 排序、引号和换行，不保留原注释或排版。

读取权限文件时会把 CRLF 规范化为 LF，孤立 CR 和 NUL 仍会被拒绝。

保存操作在写入前完成全量渲染，并对每个文件执行同目录原子替换，但文件系统无法为已有目录提供跨多个文件的事务。

保存前还会拒绝受管目录和文件的符号链接、路径别名与文件/目录冲突，避免半套配置或越界写入。

含密码或分片密钥的 `cluster.ini`、`server.ini` 与 token 文件以 `0600` 权限创建。

应只在所有分片停止后调用保存，因为游戏可能在存档期间重写世界覆盖文件。

保存调用执行期间不得由其他进程并发替换集群目录中的路径。

需要持久保留的 Mod 自定义 settings preset，最好让同一 ID 同时注册为 worldgen 与 settings preset。

也可以只使用显式 overrides，以避开当前游戏回写器对 settings-only preset 的已知限制。

### 房间级组合 Preset

`RoomPreset` 是整个房间配置树的稀疏片段，不是单个世界的 `worldgen_preset` 或 `settings_preset`。

它可以同时组合 `cluster.ini` 设置、分片拓扑与 `server.ini`、`worldgenoverride.lua` 和全部分片的 Mod。

`build()` 最终生成普通 `ClusterConfig`，因此保存、读取、编辑和运行接口完全相同。

内建片段保持正交：

| 片段 | 内容 |
| --- | --- |
| `FOREST`、`CAVES`、`FOREST_CAVES` | 标准地表、洞穴及双分片拓扑 |
| `ENDLESS_GENERATION` | 地表使用 `ENDLESS` 生成 preset |
| `ENDLESS_SETTINGS` | 地表使用 `ENDLESS` settings preset，洞穴显式加入四项无尽 override |
| `ENDLESS` | 组合上述两个无尽片段 |
| `LIGHTS_OUT_GENERATION` | 地表使用 `LIGHTS_OUT` 生成 preset，洞穴设为永夜 |
| `LIGHTS_OUT_SETTINGS` | 地表使用 `LIGHTS_OUT` settings preset |
| `FOREST_ONLY_NIGHT` | 地表显式设为永夜，用于和其他 settings preset 组合 |
| `SHARDED` | 启用分片并使用本机 master 地址；密钥仍由实例注入 |

`compose()` 从左到右合并，后面的片段只覆盖自己显式设置的字段。

Mod 按名称取并集，同名项由后面的片段整体替换，不会猜测如何深合并 Mod 私有选项。

同一分片的世界顶层字段和强类型 overrides 分别合并；不同世界类型不能合并。

下面用 "双分片 + 永夜生成 + 无尽玩法 + Mod" 组合出当前的永夜无尽房间：

```python
from pydantic import SecretStr

from dst_server.cluster import (
    ClusterSettings,
    ModOverride,
    ModOverrides,
    RoomPreset,
)
from dst_server.cluster.presets import (
    ENDLESS,
    FOREST_CAVES,
    FOREST_ONLY_NIGHT,
    LIGHTS_OUT_GENERATION,
    compose,
)


mods = RoomPreset(
    mods=ModOverrides(
        entries={"workshop-351325790": ModOverride(enabled=True)},
    )
)
preset = compose(
    FOREST_CAVES,
    ENDLESS,
    LIGHTS_OUT_GENERATION,
    FOREST_ONLY_NIGHT,
    mods,
)
cluster = preset.build(
    token=SecretStr("replace-with-cluster-token"),
    cluster_key=SecretStr("replace-with-a-random-secret"),
    settings=ClusterSettings(
        cluster_name="Lights Out Endless",
        max_players=6,
    ),
)
```

最终地表为 `worldgen_preset="LIGHTS_OUT"`、`settings_preset="ENDLESS"` 和 `day="onlynight"`。

洞穴仍保留 `DST_CAVE`，并组合无尽四项 override 与 `day="onlynight"`。

房间编号、名称规则、人数、端口槽、路径、镜像、Quadlet 和遥测端点属于部署策略，不进入游戏房间 preset。

## Python 集群运行时

`Cluster` 可以直接接收 `ClusterConfig`，在进程启动前级联保存全部必备配置，并把每个分片暴露为独立 `Server` 句柄。

省略 `config` 时，`Cluster` 会先执行完整的 `ClusterConfig.load()`。

自定义或歧义世界类型通过构造参数中的 `level_overrides_types` 和 `world_overrides_types` 注册表分别传入。

```python
import asyncio
from pathlib import Path

from dst_server.cluster import Cluster


async def run() -> None:
    path = Path("/srv/dst/Cluster_1")
    async with Cluster(
        path,
        config=cluster,
        log_handler=lambda shard, line: print(f"[{shard}] {line}"),
    ) as runtime:
        master = runtime["Master"]
        print(await master.game.world.state())
        print(await runtime.execute_all("print(TheWorld ~= nil)"))
        await runtime.save()


asyncio.run(run())
```

`servers` 和索引提供按分片的 `Server` 访问。

使用 `runtime[name].game` 操作类型化游戏 API。

使用 `runtime[name].execute()` 或 `execute_all()` 分别执行单分片或全分片原始命令。

`runtime[name].read_event()` 和 `runtime[name].read_game_event()` 分别读取该分片的生命周期事件和游戏事件，日志回调始终携带分片名。

`Cluster.save()` 只向 master 触发一次保存，预先武装并等待每个分片各自的 FD5 确认，再返回按分片组织的结果。

## 运行时权限控制

`PlayerClient.blocklist()` 和 `is_blocked()` 查询当前分片进程的运行时 blocklist。

`unban(userid)` 删除当前分片中匹配的 `userid` 或 `netid` 项，并且只在确实删除条目时返回 `True`。

blocklist API 不承诺自动同步其他分片；需要操作哪个分片，就使用哪个 `Server` 句柄。

`is_whitelisted()`、`whitelist()` 和 `unwhitelist()` 应通过主分片调用，因为原生白名单由主分片管理。

`whitelist()` 返回添加后的成员状态，`unwhitelist()` 返回删除后的非成员状态。

`is_admin(userid)` 只查询当前已连接玩家，返回 `True`、`False` 或玩家离线时的 `None`。

SDK 不提供运行时管理员增删；应编辑 `ClusterConfig.adminlist` 并在所有分片停止后保存。

## 补充说明

`cluster.ini` 保存整个集群共用的配置，`server.ini` 保存单个分片的配置。

`server.ini` 可以覆盖 `[SHARD]` 中的 `bind_ip`、`master_ip`、`master_port` 和 `cluster_key`。

它还保存各分片自己的玩家与 Steam query 端口。

`tick_rate` 是集群级选项，只能由 `cluster.ini` 或 `-tick` 命令行参数设置。

`cluster.ini` 接受 `1–60`，而命令行 `-tick` 仍只接受 `15–60`。

同一主机上的每个分片必须使用不同的 `server_port` 和 `master_server_port`。

旧资料中的 `settings.ini` 已经拆分为 `cluster.ini` 和 `server.ini`，新部署不应再创建它。

旧版 `[NETWORK] / cluster_intention` 已不再由当前创建界面写入，当前玩法倾向由世界覆盖计算，因此 SDK 不生成这个遗留字段。

跨主机部署时，各主机的 `cluster.ini` 必须保持集群级参数一致；主分片监听 `bind_ip = 0.0.0.0`，其他分片通过 `master_ip` 连接主分片。

## 参考资料

- [Klei Forum：Dedicated Server Settings Guide](https://forums.kleientertainment.com/forums/topic/64552-dedicated-server-settings-guide/)
- [Klei：Dedicated Server Command Line Options Guide](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)
- [Klei Game Update 128953：Steam 后端端口](https://forums.kleientertainment.com/forums/topic/51703-game-update-128953-332015/)
- [Klei Forum：Understanding Shards and Migration Portals](https://forums.kleientertainment.com/forums/topic/59174-understanding-shards-and-migration-portals/)
- [Klei Forum：2022 Updated Dedicated Server Quick Setup Guide - Linux](https://forums.kleientertainment.com/forums/topic/140715-2022-updated-dedicated-server-quick-setup-guide-linux/)
- [Klei Forum：`worldgenoverride.lua` with the post-Caves settings](https://forums.kleientertainment.com/forums/topic/53014-worldgenoverridelua-with-the-new-post-caves-settings/)
- [Klei Forum：`worldgenoverride.lua` settings for the March QoL update](https://forums.kleientertainment.com/forums/topic/127830-worldgenoverridelua-settings-for-march-qol-update/)
- [Klei Forum：`leveldataoverride.lua` 与 `worldgenoverride.lua`](https://forums.kleientertainment.com/forums/topic/150248-leveldataoverride-worldgenoverride-and-world-settings-picker-mod-oh-my/)
- [Valve：Steam Game Server API](https://partner.steamgames.com/doc/api/steam_gameserver)
- [Valve：当前 `steam_gameserver.h`](https://github.com/ValveSoftware/source-sdk-2013/blob/0759e2e8e179d5352d81d0d4aaded72c1704b7a9/src/public/steam/steam_gameserver.h)
