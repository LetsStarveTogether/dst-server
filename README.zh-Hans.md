# 饥荒联机版专用服务器容器

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

默认镜像：`quay.io/wh2099/dst-server:latest`

本项目将一个 DST 集群运行成一个 Pod，并为每个分片提供一个长驻 Agent 容器。
主分片容器负责集群协调和公开 RPC endpoint。
每个 Agent 管理一个可重启的游戏进程。

## 快速开始

1. 安装 [Podman](https://docs.podman.io/en/latest/index.html)。
2. 在 [Klei 服务端管理页面](https://accounts.klei.com/account/game/servers?game=DontStarveTogether) 创建专服 token。
3. 导出 token，并生成房间配置和 Quadlet unit：

   ```shell
   export DST_SERVER_CLUSTER_TOKEN='replace-with-cluster-token'
   uv run python -m scripts.generate_rooms \
     --userns 'keep-id:uid=1000,gid=1000' \
     --cluster-root "${HOME}/.local/share/dst" \
     --quadlet-dir "${HOME}/.config/containers/systemd"
   ```

   token 以 secret 形式挂载时，改用 `--token-file /run/secrets/dst_cluster_token`。
   显式 token 文件优先于环境变量。

   在模块名后传入房间编号即可只生成指定房间，例如 `0 20 139`。
   不传房间编号时，脚本生成已声明的 `000–139` 房间。

   每个房间编号会在 `30000–32999` 中选择一个包含十个宿主机端口的槽位。
   每个房间最多支持四个分片，并且只发布实际使用的端口。
   同时运行的房间必须使用不同槽位。
4. 重载 rootless systemd 管理器并启动生成的 Pod：

   ```shell
   systemctl --user daemon-reload
   systemctl --user start dst-000-pod.service
   ```

以上 rootless 命令应由拥有集群目录的普通用户执行。
`--userns` 通过 Pod 的 `UserNS` 将该用户映射为容器内的 `steam` 用户（UID/GID `1000`）。
rootful 部署以 root 运行生成器：

```shell
uv run python -m scripts.generate_rooms \
  --volume-idmap 'uids=0-1000-1;gids=0-1000-1' \
  --cluster-root /srv/dst \
  --quadlet-dir /etc/containers/systemd
```

idmapped mount 使宿主文件保持 root 所有，容器内显示为 UID/GID `1000`。
其 systemctl 命令应省略 `--user`。
映射选项默认留空，生成器不会自动选择。
具体配置与运行要求见 [容器用户与目录权限](docs/configuration.md#容器用户与目录权限)。
rootful 容器复用宿主机 DNS over TLS 的配置见 [容器 DNS](docs/configuration.md#容器-dns)。
Mod 下载默认使用游戏服务端原生更新器，最多尝试五次，共用 30 分钟总期限。
设置 `DST_SERVER_MOD_UPDATER=steamcmd` 可在启动时使用独立 SteamCMD 后端。
SDK 用法、兼容性验证与后端选型见 [Mod 更新器](docs/mods.md)。

生成的配置默认使用 `:latest`，测试服使用 `--image quay.io/wh2099/dst-server:beta`。
两个标签均跟随对应渠道更新，生成器不解析或固定镜像摘要、游戏版本号。
容器配置使用 `Pull=always`，每次启动检查远端镜像；`TimeoutStartSec=1800` 为下载预留 30 分钟。
`systemctl daemon-reload` 只重载配置，运行中的房间在下次重启时使用更新后的镜像。

## 日常维护

```shell
systemctl --user status dst-000-pod.service
journalctl --user -u dst-000-forest.service -f
systemctl --user restart dst-000-pod.service
systemctl --user stop dst-000-pod.service
```

停止房间不会隐式保存。
需要最新快照时，应在停止前调用集群保存 RPC。

每个 Agent 还会创建 `console` FIFO 作为恢复接口。
主分片 FIFO 位于集群根目录，次分片 FIFO 位于对应分片目录：

```shell
echo 'c_announce("服务器即将维护。")' > "${HOME}/.local/share/dst/000/console"
echo 'c_save()' > "${HOME}/.local/share/dst/000/cave/console"
```

FIFO 可以执行任意服务端 Lua，必须与游戏进程处于同一信任边界。

## 存档文件

集群的整体配置布局见 [配置文件](docs/configuration.md#配置文件)。
每个分片都有自己的 `<shard>/save/`，世界 ID 标识该分片生成的世界，记录在 `shardindex.session_id` 中。
森林和洞穴分别保存世界与人物状态；世界 ID 不表示玩家的一次登录。
下面是启用玩家路径编码时的原版典型布局，文件和目录按需创建，数字文件只列一份快照：

```text
<shard>/save/
├── shardindex
├── shardindex_time
├── session/
│   └── <session-id>/
│       ├── 0000000001
│       ├── 0000000001.meta
│       └── <encoded-user-id>/
│           ├── 0000000001
│           ├── 0000000001.meta
│           └── savelocation
├── profile
├── modindex
├── boot_modindex
├── cached_userid
├── server_temp/
│   └── server_save
├── client_temp/
├── event_match_stats/
├── world_presets/
└── mod_config_data/
```

| 路径（相对 `save/`） | 保存的内容 |
| --- | --- |
| `shardindex` | 分片存档索引：`world` 保存位置、预设和覆盖等世界选项，`server` 保存服务设置，`session_id` 指向世界目录，`enabled_mods` 保存启用的 Mod 及配置，`version` 记录索引格式版本。 |
| `shardindex_time` | `created` 和 `saved` 分别记录创建与最近保存的现实时间，使用 Unix 秒数。 |
| `session/<session-id>/<snapshot>` | 世界快照主体：地图地块、道路、尺寸、拓扑、持久实体、世界与网络组件状态、Mod 记录，以及可选的在线玩家清单。 |
| `session/<session-id>/<snapshot>.meta` | 世界摘要，包含 `clock` 和 `seasons`，用于读取天数、昼夜阶段、季节等信息。 |
| `session/<session-id>/<encoded-user-id>/<snapshot>` | 人物快照主体：角色、位置、年龄、皮肤和组件状态，例如生命、饥饿、理智、物品栏、装备、背包内容及已学配方。 |
| `session/<session-id>/<encoded-user-id>/<snapshot>.meta` | 人物摘要，原版仅写入 `character = player.prefab`。 |
| `session/<session-id>/<encoded-user-id>/savelocation` | 原生二进制位置历史，按快照记录玩家所在的分片，供读取人物存档时定位分片。 |

索引的字段由 [ShardIndex](dst-scripts/scripts/shardindex.lua) 定义。
世界主体与摘要在 [SaveGame](dst-scripts/scripts/mainfunctions.lua) 中生成。
人物主体与摘要由 [SerializeUserSession](dst-scripts/scripts/networking.lua) 保存。
世界主体内部的 `meta` 记录构建版本、随机种子、世界类型和存档版本等，与独立的世界 `.meta` 摘要不同。

数字文件名是快照序号，不是游戏天数；界面天数根据 `clock.cycles + 1` 计算，同一天可以产生多份快照。
人物目录中的快照可能不连续，也不保证每份世界快照都有同号的人物文件。
保存世界时会保存当时的 `AllPlayers`，人物生成等流程也会单独保存人物。
恢复时由原生 `TheNet:GetUserSessionFile` 选择人物文件；[保存槽读取流程](dst-scripts/scripts/saveindex.lua) 还会查询玩家所在分片。

| 辅助路径 | 用途与内容 |
| --- | --- |
| `profile` | 当前运行环境的档案和偏好，逻辑内容为 JSON，包含控制设置、启动信息、收藏 Mod、预设和提示状态等。 |
| `modindex` | Mod 管理状态的 Lua 表，包含已知 Mod、启用或临时禁用状态、API 版本等。 |
| `boot_modindex` | Mod 启动过程标记，内容为 `loading` 或 `done`，用于判断前次加载是否完成。 |
| `cached_userid` | 原生引擎维护的服务器账号 Klei ID 缓存；它保存身份标识，不是专服令牌或人物快照。 |
| `server_temp/server_save` | 世界初始化时生成的精简临时地图副本，写入前去掉实体、快照、地块、导航等数据，不能替代完整世界快照。 |
| `client_temp/` | 原生引擎管理的客户端临时目录，具体文件由引擎按需维护。 |
| `event_match_stats/` | 活动玩法统计，逻辑内容为 CSV，包含胜负、回合、时间和分数等；原版写入分支排除 Dedicated。 |
| `world_presets/` | 用户保存的预设，`.wsp` 保存世界设置，`.wgp` 保存生成设置，内容包含基础预设、覆盖项、名称、描述和版本。 |
| `mod_config_data/` | Mod 配置选项及所选值，常见文件名为 `modconfiguration_<modname>`；Mod 也可能在这里保存额外数据。 |

这些 Mod 管理文件和目录属于原版的 Mod 支持系统，纯净服也可能创建。
实体和组件通过 `OnSave()` 提供保存数据，Mod 可以扩展世界或人物字段，也可以另写持久文件。
表中的 Lua、JSON 和 CSV 指逻辑内容，磁盘文件还可能带有 KLEI 封装、压缩或末尾空字节，具体取决于写入接口。

SDK 默认使用 `encode_user_path = true`，并始终在 `server.ini` 中显式写入当前设置值。
显式 `false` 会保留；启用编码时，在线玩家的目录名使用 Klei ID 对应的 12 位编码。
已有存档切换路径方式时，需要同步人物目录、`server.ini` 和 `shardindex.server.encode_user_path`。
迁移时保留人物目录内的全部快照、`.meta` 与 `savelocation`。
路径编码不改变账号身份，因此不会转换 `cached_userid` 等文件中的 Klei ID。

## 运行时架构

主分片容器运行 `dst-server master`。
次分片容器运行 `dst-server serve <shard>`，并通过 Pod 内部 socket 向主分片注册。

所有容器将同一个集群目录挂载到 `/cluster`，并使用镜像内 `/install` 的游戏安装。
主分片在 `/cluster/.dst-server.sock` 提供仅所有者可访问的集群 RPC。

控制器会等待配置中的完整 shard roster，再准备或启动任何游戏进程。
集群启动会先更新所需的服务端 Mod，再启动游戏进程。
集群 `restart()` 会先停止全部游戏进程，更新共享 Mod 后再启动。
显式刷新 Mod 可依次调用 `stop()`、`update_mods()` 和 `start()`；最后一步复用刚刚成功的更新。
接管运行中的 Agent 或恢复单个分片时，会复用已安装的 Mod。

每个 Agent 独立监督自己的游戏进程，并对短暂故障进行有界重试。
重试预算耗尽或 Agent 丢失时，控制器会停止其余游戏进程。
重试预算耗尽并 fail-close 后，Agent daemon 和公开 RPC 会保持可用，以便诊断和恢复。
主分片容器丢失时 RPC 会暂时断开，直到 systemd 重启主分片及其绑定的次分片。

各 Agent 每 60 秒通过 Podman 的 systemd 通知 socket 发送一次原生 watchdog 通知。
连续五分钟未收到通知时，systemd 会重启容器；无需心跳文件或定时启动检查进程。
生命周期状态和故障边界见 [运行时架构](docs/runtime.md)。

## 集群 RPC

通过 Cap'n Proto Unix socket 管理已部署的 Pod：

```python
import asyncio

from dst_server.rpc import ClusterClient, rpc_runtime


async def main() -> None:
    async with rpc_runtime():
        async with await ClusterClient.connect("/cluster/.dst-server.sock") as cluster:
            status = await cluster.status()
            print(status)
            print(await cluster.shard(status.master).status())


asyncio.run(main())
```

宿主机客户端通过挂载的集群路径访问同一个 socket。
该 API 提供集群和分片生命周期操作、配置 revision、游戏查询、管理操作、保存、原始 Lua 和事件。
修改状态的调用在结果不确定时不会自动重放。

自行持有单个游戏进程的应用可以使用进程内 `Server` API。

## 遥测

默认 `critical` profile 会安装 management RPC 和关键游戏事件。
通过 `DST_SERVER_TELEMETRY_PROFILE=off|critical|history` 选择事件 profile。
标准 `OTEL_EXPORTER_OTLP_*` 环境变量只配置导出传输，不会启用事件 Hook。

安装 `dst-server[otel]` 可启用 OTLP 导出，安装 `dst-server[klei]` 可查询 Klei 构建和 Lobby 服务。
数据与故障边界见 [游戏事件与 OpenTelemetry](docs/telemetry.md)。

## Lua 注解

`dst-annotations` 可以从 DST component 或 `modutil.lua` 生成兼容 LSP 的 Lua 定义。

```console
dst-annotations dst-scripts/scripts/components --output components_def.lua
```

## 文档

- [使用指南：配置、运行机制、遥测与 Mod 更新](docs/README.md)
- [存档导出](docs/configuration.md#导出存档)
- [DST Lua 源码索引](dst-scripts/index/README.md)
