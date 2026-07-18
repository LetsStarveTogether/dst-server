# 饥荒联机版专用服务器容器

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

![Steam 商店头图](https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/322330/header_schinese.jpg?t=1736195686)

镜像地址：`quay.io/wh2099/dst-server`

镜像会启动一个 DST 集群中的全部分片，保留常用的 console FIFO，并可通过 OpenTelemetry 导出经过校验的游戏事件。

## 快速开始

1. 安装 [Podman](https://docs.podman.io/en/latest/index.html) 等容器运行时。
2. 在 [Klei 服务端管理页面](https://accounts.klei.com/account/game/servers?game=DontStarveTogether) 创建并下载专服配置。
3. 在宿主机上解压。

   挂载到 `/cluster` 的目录必须直接包含 `cluster.ini`、`cluster_token.txt` 和分片目录：

   ```text
   Cluster_1/
   ├── cluster.ini
   ├── cluster_token.txt
   ├── Master/
   │   └── server.ini
   └── Caves/
       └── server.ini
   ```

4. 启动容器，并把宿主机路径替换成实际解压出的集群目录：

   ```shell
   sudo podman run \
     --name dst \
     --detach \
     --network host \
     --volume "${HOME}/Cluster_1:/cluster" \
     quay.io/wh2099/dst-server:latest
   ```

## 日常维护

```shell
podman logs dst
podman stop dst
podman start dst
podman restart dst
```

停止容器时，入口程序会逐个平滑关闭分片，DST 会在关闭过程中保存。

入口程序会为主分片在集群根目录创建一个 `console` 命名管道，并在每个次级分片目录中各创建一个：

```shell
echo 'c_announce("服务器即将维护。")' > "${HOME}/Cluster_1/console"
echo 'c_save()' > "${HOME}/Cluster_1/Caves/console"
```

常用命令包括 `c_reset()`、`c_regenerateworld()`、`c_save()`、`c_shutdown(false)`、`c_announce("...")` 和 `c_listallplayers()`。

## 启动过程

[`entrypoint.py`](entrypoint.py) 会验证集群、准备权限文件和 Mod，并统一更新一次创意工坊内容。
随后，它会发现全部分片，并通过 `-cloudserver` 模式为每个分片启动一个 `Server`。

`DST_SKIP_MOD_UPDATE=1` 可以跳过这次创意工坊更新。

`DST_INSTALL_PATH` 和 `DST_CLUSTER_PATH` 可在开发和测试时覆盖 `/install` 和 `/cluster`。

设置任意 `OTEL_EXPORTER_OTLP_*_ENDPOINT` 环境变量后，入口程序会配置官方 OpenTelemetry Pipeline 并导出经过校验的游戏事件。

## Python SDK

`dst-server` 包负责启动和控制一个 Linux DST 分片进程。

```python
import asyncio

from dst_server import Server, ServerArgs


async def main() -> None:
    async with Server(ServerArgs(shard="Master")) as server:
        world = await server.game.world.state()
        players = await server.game.players.list()
        await server.game.world.announce(
            f"第 {world.day} 天，在线玩家 {len(players)} 人"
        )
        await server.save()


asyncio.run(main())
```

类型化 API 覆盖世界与玩家查询、背包、管理操作、确认完成的保存、原始 Lua、服务端生命周期事件和游戏事件。

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
