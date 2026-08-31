# 饥荒联机版专用服务器容器

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

镜像地址：`quay.io/wh2099/dst-server`

本项目将一个 DST 集群运行成一个 Pod，并为每个分片提供一个长驻 Agent 容器。
主分片容器负责集群协调和公开 RPC endpoint。
每个 Agent 管理一个可重启的游戏进程。

## 快速开始

1. 安装 [Podman](https://docs.podman.io/en/latest/index.html)。
2. 在 [Klei 服务端管理页面](https://accounts.klei.com/account/game/servers?game=DontStarveTogether)创建专服 token。
3. 导出 token，并生成房间配置和 Quadlet unit：

   ```shell
   export DST_SERVER_CLUSTER_TOKEN='replace-with-cluster-token'
   uv run python -m scripts.generate_rooms \
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

   启动房间前，将该范围排除在 Linux 自动端口分配之外：

   ```shell
   sudo install -Dm0755 deploy/sysctl/dst-server-reserve-ports /usr/libexec/dst-server-reserve-ports
   sudo install -Dm0644 deploy/sysctl/dst-server-port-reservation.service /etc/systemd/system/dst-server-port-reservation.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now dst-server-port-reservation.service
   ```

4. 重载 rootless systemd 管理器并启动生成的 Pod：

   ```shell
   systemctl --user daemon-reload
   systemctl --user start dst-room-000-pod.service
   ```

rootful 部署使用 `/srv/dst` 作为 `--cluster-root`，使用 `/etc/containers/systemd` 作为 `--quadlet-dir`。
其 systemctl 命令应省略 `--user`。

镜像以 `:<游戏版本号>` 作为稳定版本标签。
`:latest` 和 `:beta` 是滚动渠道别名。

## 日常维护

```shell
systemctl --user status dst-room-000-pod.service
journalctl --user -u dst-room-000-forest.service -f
systemctl --user restart dst-room-000-pod.service
systemctl --user stop dst-room-000-pod.service
```

停止房间不会隐式保存。
需要最新快照时，应在停止前调用集群保存 RPC。

每个 Agent 还会创建 `console` FIFO 作为恢复接口。
主分片 FIFO 位于集群根目录，次分片 FIFO 位于对应分片目录：

```shell
echo 'c_announce("服务器即将维护。")' > "${HOME}/.local/share/dst/room-000/console"
echo 'c_save()' > "${HOME}/.local/share/dst/room-000/cave/console"
```

FIFO 可以执行任意服务端 Lua，必须与游戏进程处于同一信任边界。

## 运行时架构

主分片容器运行 `dst-server master`。
次分片容器运行 `dst-server serve <shard>`，并通过 Pod 内部 socket 向主分片注册。

所有容器将同一个集群目录挂载到 `/cluster`，并使用镜像内 `/install` 的游戏安装。
主分片在 `/cluster/.dst-server.sock` 提供仅所有者可访问的集群 RPC。

控制器会等待配置中的完整 shard roster，再准备或启动任何游戏进程。
每个配置 revision 的首次准备会验证共享目录，并更新所需的服务端 Mod。
显式刷新 Mod 时必须依次调用 `stop()`、`update_mods()` 和 `start()`。

每个 Agent 独立监督自己的游戏进程，并对短暂故障进行有界重试。
重试预算耗尽或 Agent 丢失时，控制器会停止其余游戏进程。
重试预算耗尽并 fail-close 后，Agent daemon 和公开 RPC 会保持可用，以便诊断和恢复。
主分片容器丢失时 RPC 会暂时断开，直到 systemd 重启主分片及其绑定的次分片。

容器健康检查监控各 Agent 的心跳，systemd 负责重启失败的容器。
生命周期状态和故障边界见[运行时架构](docs/python-sdk-telemetry-flow.md)。

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
数据与故障边界见[游戏事件与 OpenTelemetry](docs/opentelemetry-game-events.md)。

## Lua 注解

`dst-annotations` 可以从 DST component 或 `modutil.lua` 生成兼容 LSP 的 Lua 定义。

```console
dst-annotations dst-scripts/scripts/components --output components_def.lua
```

## 文档

- [集群架构与配置](docs/dedicated-server-configuration.md)
- [专用服务器启动参数](docs/dedicated-server-options.md)
- [`-cloudserver` IPC 契约](docs/cloudserver-ipc.md)
- [运行时架构](docs/python-sdk-telemetry-flow.md)
- [游戏事件与 OpenTelemetry](docs/opentelemetry-game-events.md)
- [DST Lua 源码索引](dst-scripts/index/README.md)
