# 饥荒联机版专用服务器容器

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

![Steam 商店头图](https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/322330/header_schinese.jpg?t=1736195686)

镜像地址：`quay.io/wh2099/dst-server`

镜像既可运行单个分片，也可以 embedded 多进程方式运行完整集群，并保留常用的 console FIFO。

推荐的 systemd 部署为每个集群一个 Pod、一个独立的一次性预处理容器，以及每个分片一个 worker 容器。

## 快速开始

1. 安装 [Podman](https://docs.podman.io/en/latest/index.html) 等容器运行时。
2. 在 [Klei 服务端管理页面](https://accounts.klei.com/account/game/servers?game=DontStarveTogether) 创建专服 token。
3. 通过 secret manager 将 token 作为 `DST_SERVER_CLUSTER_TOKEN` 提供给生成器。
   然后生成整套强类型房间配置及 Quadlet unit：

   ```shell
   uv run python -m scripts.generate_rooms \
     --cluster-root "${HOME}/.local/share/dst" \
     --quadlet-dir "${HOME}/.config/containers/systemd"
   ```

   也可以通过 `--token-file /run/secrets/dst_cluster_token` 读取挂载的 secret 文件。
   显式指定的 token 文件优先于环境变量。

   在模块名后传入房间编号即可只生成指定房间，例如 `0 20 139`。

   脚本按照声明的范围生成 `000–139`：纯净生存、纯净无尽、半纯生存、半纯无尽、挂皮肤、永夜生存、永夜无尽、岛屿冒险、云霄国度、冒险、暴食和熔炉。

   脚本直接使用房间编号作为端口槽位。
   `RoomPortAllocation` 仍支持在 `000–299` 内通过编程设置偏移量。

   对于分片序号 `S`，外部玩家端口是 `30000 + 600 × S + slot`。
   Steam query 端口是 `30300 + 600 × S + slot`。

   例如房间 `007` 的 Master 使用 `30007/30307`，Caves 使用 `30607/30907`。

   4 个分片会使用 `30000–32399`，位于 Kubernetes
   [默认 NodePort 范围](https://kubernetes.io/zh-cn/docs/concepts/services-networking/service/#type-nodeport)。

   每个槽位最多支持 4 个分片；NodePort 是集群级资源，仍需避免与其他 Service 的手工或自动分配冲突。

   所有对外端口都由 Pod 发布，各分片容器共享 Pod 网络命名空间并使用生成的内部端口。
   每个 worker 通过 `--external-port` 传入已发布的玩家端口，因此 `server.ini` 保持常用内部端口时，Klei 大厅仍会公告可访问的宿主机端口。

所有镜像都以 `:<游戏版本号>` 为主标签。
`:latest` 和 `:beta` 保留为滚动渠道别名。

4. 重载 rootless systemd 用户管理器并启动生成的 Pod：

   ```shell
   systemctl --user daemon-reload
   systemctl --user start dst-room-000-pod.service
   ```

rootful 部署应将 `--quadlet-dir` 设为 `/etc/containers/systemd`，并将 `--cluster-root` 设为 `/srv/dst`。
此时应在 systemctl 命令中省略 `--user`。

对仍使用 `podman0` 和 `10.88.0.0/16` 原生默认值的全新 rootful 主机，安装仓库提供的配置：

```shell
sudo install -Dm0644 deploy/podman/podman.json /etc/containers/networks/podman.json
```

如果目标文件已存在或主机使用了不同网络值，应保留原值并只把 `dns_enabled` 改为 `true`，不要覆盖整个文件。
仓库文件保留 Podman 默认的 `podman0` 和 `10.88.0.0/16`，只为默认 bridge 开启 Aardvark DNS。
Podman 官方也使用[导出并持久化默认网络](https://github.com/podman-container-tools/podman/blob/main/docs/tutorials/basic_networking.md#default-network)的方式修改这个内存网络。
已有使用默认网络的容器或 Pod 应先停止并在安装后重新创建。
从旧共享 Quadlet 网络迁移时，先停止房间和 `dst-server-network.service`，删除旧 `dst-server.network` 源文件，再重载 systemd。

Pod unit 已挂载到 `default.target`，因此它会随该 systemd 管理器的默认 target 启动。

## 日常维护

```shell
systemctl --user status dst-room-000-pod.service
journalctl --user -u dst-room-000-forest.service -f
systemctl --user restart dst-room-000-pod.service
systemctl --user stop dst-room-000-pod.service
```

生成的 worker 为 SDK 内部 30 秒的优雅停机期限配置了 40 秒 Podman 超时和 50 秒 systemd 超时。

每个 worker 都会请求它自己的分片保存并关闭。
超过宽限期仍无响应的分片会被强制终止。

每个 worker 都会为自己的分片创建 `console` 命名管道。
主分片管道位于集群根目录，次级分片管道位于对应分片目录：

```shell
echo 'c_announce("服务器即将维护。")' > "${HOME}/.local/share/dst/room-000/console"
echo 'c_save()' > "${HOME}/.local/share/dst/room-000/cave/console"
```

常用命令包括 `c_reset()`、`c_regenerateworld()`、`c_save()`、`c_shutdown(false)`、`c_announce("...")` 和 `c_listallplayers()`。

## 启动过程

独立的一次性容器会先运行 `dst-server prepare`，以验证集群、创建权限和 Mod 文件，并更新创意工坊内容。
只有预处理成功后，systemd 才会启动 Pod，并由每个 `dst-server run <shard>` worker 通过 `-cloudserver` 协议启动且仅启动一个分片。
所有容器都将同一棵集群配置树挂载到 `/cluster`。
镜像使用上文所示的固定 `/install` 和 `/cluster` 路径。

不带子命令运行镜像时，仍会使用 embedded supervisor 在一个容器中发现并启动完整集群。

## Python SDK

`dst-server` 包可以通过 `Server` 控制单个 Linux DST 分片，也可以通过 `Cluster` 控制完整集群。

```python
import asyncio

from dst_server import Server, ServerConfig


async def main() -> None:
    async with Server(ServerConfig(shard="Master")) as server:
        world = await server.game.world.state()
        players = await server.game.players.list()
        await server.game.world.announce(
            f"第 {world.day} 天，在线玩家 {len(players)} 人"
        )
        await server.save()


asyncio.run(main())
```

### 强类型集群配置

集群配置使用显式世界模型，并以经过 Lua literal 安全校验的映射保存 Mod 选项：

完整房间可用 `RoomPreset` 组合集群设置、分片、世界和 Mod；参见[房间级组合 preset](docs/dedicated-server-configuration.md#房间级组合-preset)。

```python
import asyncio
from pathlib import Path
from pydantic import SecretStr

from dst_server.cluster import (
    CaveOverrides,
    Cluster,
    ClusterConfig,
    ForestOverrides,
    ModOverride,
    ModOverrides,
    ModSettings,
    ShardConfig,
    ShardSettings,
    WorldgenOverride,
)


cluster = ClusterConfig(
    shards={
        "Master": ShardConfig(
            settings=ShardSettings(is_master=True, id=1),
            world=WorldgenOverride.forest(
                overrides=ForestOverrides(day="longday"),
            ),
            mods=ModOverrides(
                entries={
                    "workshop-351325790": ModOverride(
                        enabled=True,
                        configuration_options={"difficulty": "hard"},
                    ),
                },
            ),
        ),
    },
    token=SecretStr("replace-with-cluster-token"),
    mod_settings=ModSettings(disable_local_mod_warning=True),
)


async def run_cluster() -> None:
    path = Path("/srv/dst/Cluster_1")
    async with Cluster(
        path,
        config=cluster,
        log_handler=lambda shard, line: print(f"[{shard}] {line}"),
    ) as runtime:
        print(await runtime.execute_all("print(TheWorld ~= nil)"))
        print(await runtime["Master"].game.world.state())
        await runtime.save()


asyncio.run(run_cluster())
```

洞穴配置使用 `WorldgenOverride.cave(overrides=CaveOverrides(...))`。
`ModSettings` 类型化 `force_enabled`、`debug_print`、`mod_errors`、`disable_mod_disabling` 和 `disable_local_mod_warning`。
省略 `mod_settings` 会保留已有文件，显式传入 `ModSettings()` 则清空这些设置。
setup 文件只负责下载 Mod，`ModOverride.enabled=True` 或 `ModSettings.force_enabled` 才负责启用。
游戏原生的 `saved_server` Mod 持久值可能遮蔽同名 override，SDK 不会删除这些持久数据。

向 `Cluster` 传入 `config` 时，SDK 会在启动前验证并保存完整配置树。
省略 `config` 时，SDK 会通过 `ClusterConfig.load()` 严格读取已有配置树；自定义或歧义世界字段需要传入对应的强类型注册表。
运行时把每个分片暴露为 `Server`，以 `(shard, line)` 路由日志，按分片读取生命周期事件和游戏事件，并支持单分片或并发原始命令。
整组保存只触发一次 master，并等待每个分片各自确认。

`Cluster` 仍是由应用自行持有完整运行时时使用的 embedded 多进程 API。
推荐的 Quadlet 部署则为每个分片提供独立的 `Server` 进程和 systemd unit。

### 强类型 Quadlet 配置

Pod 和容器 unit 与游戏配置一样支持读取、不可变编辑和原子保存：

```python
from pathlib import Path

from dst_server.cluster import QuadletApplication

directory = Path.home() / ".config/containers/systemd"
application = QuadletApplication.load(directory, name="dst-room-000")
application = application.replace(
    pod=application.pod.replace(description="DST survival room 000"),
)
application.save(directory)
```

`QuadletApplication` 默认让 Pod 和独立的 prepare 容器使用 Podman 默认网络，不再为每个部署生成共享 network unit。
rootful 部署通过上面的全局 `podman.json` 开启 DNS；rootless Podman 保持自己的原生网络默认值。
修改并保存 Quadlet 源文件后需要重载 systemd。
通过 `application.replace(pod=...)` 修改或删除玩家端口时，对应 worker 的 `--external-port` 会默认同步。
显式同时传入 `workers` 可以保留不同的公告端口。

类型化 API 覆盖世界与玩家查询、背包、管理操作、确认完成的保存、原始 Lua、服务端生命周期事件和游戏事件。

运行时 blocklist 查询和 `unban()` 只作用于选中的分片进程，不承诺跨分片同步。
白名单查询和增删应使用主分片，`is_admin()` 返回已连接玩家的管理员状态，玩家离线时返回 `None`。
管理员名单增删仍通过配置文件完成。

`reset()`、`regenerate()`、`regenerate_shard()` 和 `rollback()` 只有在观察到更高 FD5 generation 且对应 driver ready 后才返回。
默认 30 秒的完成期限覆盖触发 RPC、generation 切换和 reinstall。

如果 Python 在写入其他类型化命令后观察到 generation 变化，SDK 会抛出 `IndeterminateCommandError`，并且绝不自动重放该命令。

有限管理操作共享一个总期限，不会为每个等待阶段重新计时。
最终编码后的 FD3 命令行上限为 64 KiB，单个 FD4 frame 的 payload 上限为 64 KiB 和 1,024 行。

SDK 默认安装 management RPC driver 和 `critical` 游戏事件遥测。
容器入口可通过 `DST_SERVER_TELEMETRY_PROFILE=off|critical|history` 调整记录等级。
本机 Netdata 2.11 的 logs-only 配置、落盘验证和安全边界见[游戏事件与 OpenTelemetry](docs/opentelemetry-game-events.md)。
rootful Quadlet 示例已经使用该宿主机专用日志端点。

安装 `dst-server[otel]` 可启用 OTLP 导出，安装 `dst-server[klei]` 可查询 Klei 构建和 Lobby 服务。

## 文档

游戏服务端参考：

- [集群架构与配置文件](docs/dedicated-server-configuration.md)
- [服务端启动参数](docs/dedicated-server-options.md)

技术设计：

- [`-cloudserver` 双向通信](docs/cloudserver-ipc.md)
- [游戏事件与 OpenTelemetry](docs/opentelemetry-game-events.md)

源码索引：

- [DST Lua 源码索引](dst-scripts/index/README.md)
