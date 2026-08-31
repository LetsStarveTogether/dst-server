# DST 专用服务器启动参数

这些参数属于 `dontstarve_dedicated_server_nullrenderer_x64`，不是 Podman 参数。

命令行参数会覆盖 `cluster.ini` 或 `server.ini` 中对应的配置。

生成的容器已经固定存储路径、集群目录、分片和 Mod 目录，正常部署不需要手工传入这些参数。

## 配置路径

以下参数共同决定服务端读取哪个集群和分片：

| 参数 | 作用 |
| --- | --- |
| `-persistent_storage_root <绝对路径>` | 设置配置根目录，Linux 默认值为 `~/.klei`。 |
| `-conf_dir <目录名>` | 设置配置目录名，默认值为 `DoNotStarveTogether`。 |
| `-cluster <目录名>` | 设置集群目录名，默认值为 `Cluster_1`。 |
| `-shard <目录名>` | 设置分片目录名，默认值为 `Master`。 |

服务端最终读取以下路径：

```text
<persistent_storage_root>/<conf_dir>/<cluster>/cluster.ini
<persistent_storage_root>/<conf_dir>/<cluster>/<shard>/server.ini
```

本项目把宿主机集群目录挂载到 `/cluster`，并使用根目录 `/`、配置目录 `.` 和集群名 `cluster` 解析该路径。

## 网络与访问

| 参数 | 作用 |
| --- | --- |
| `-offline` | 以离线模式启动，并限制为局域网访问。 |
| `-disabledatacollection` | 禁用数据收集，并同时限制为离线模式。 |
| `-bind_ip <地址>` | 覆盖服务端监听地址。 |
| `-port <端口>` | 覆盖 `[NETWORK] / server_port`，有效范围为 `1024–65535`。 |
| `-master_port <端口>` | 覆盖 `[SHARD] / master_port`，有效范围为 `1024–65535`。 |
| `-external_port <端口>` | 只覆盖向 Klei 大厅公告的玩家端口，不改变容器内监听端口。 |
| `-players <人数>` | 覆盖 `[GAMEPLAY] / max_players`，有效范围为 `1–64`。 |
| `-steam_master_server_port <端口>` | 覆盖 Steam query 和 master-server-updater 使用的端口。 |
| `-tick <频率>` | 覆盖 `[NETWORK] / tick_rate`，命令行有效范围为 `15–60`。 |
| `-fo` | 只允许好友加入。 |
| `-token <令牌>` | 直接传入集群令牌，常规部署应优先使用 `cluster_token.txt`。 |

同一 Pod 中每个分片的玩家端口和 Steam query 端口都必须唯一。

Pod 将宿主机端口映射到不同内部端口时，生成器会把对应宿主机玩家端口传给 `-external_port`。

## 日志、Mod 与进程

| 参数 | 作用 |
| --- | --- |
| `-backup_log_count <数量>` | 设置保留的日志备份数量，默认值为 `100`。 |
| `-backup_log_period <秒>` | 设置日志备份间隔，默认值为 `86400` 秒。 |
| `-secondary_log_prefix <前缀>` | 设置辅助日志文件名前缀。 |
| `-only_update_server_mods` | 更新共享下载清单中的服务端 Mod，完成后退出。 |
| `-skip_update_server_mods` | 启动游戏进程时跳过 Mod 更新。 |
| `-ugc_directory <路径>` | 覆盖 v2/UGC Mod 的存储目录。 |
| `-allow_ioopenwrite_sandbox_escape` | 允许 Mod 使用 `io.open` 与 `io.write`，只应对可信代码启用。 |
| `-monitor_parent_process <PID>` | 在指定的父进程退出时自动关闭服务端。 |

每个配置 revision 首次准备时，Controller 会通过独立更新进程统一更新一次所需 Mod。

每个分片游戏进程都使用 `-skip_update_server_mods`，因此普通启动和自动重试不会重复更新。

显式更新时应通过 `ClusterClient` 依次调用 `stop()`、`update_mods()` 和 `start()`。

## `-cloudserver`

`-cloudserver` 是 Linux 专用的进程间通信模式。

它通过 FD 3 接收单行 Lua 命令，通过 FD 4 返回命令结果，并通过 FD 5 发送生命周期消息。

`ServerConfig` 始终启用该模式，具体协议见 [`-cloudserver` 双向通信](cloudserver-ipc.md)。

## 当前容器命令

主分片容器运行 `dst-server master`，并在本地组合集群 Controller 与主分片 Agent。

次分片容器运行 `dst-server serve <shard>`，并通过 Pod 内部 RPC 注册自己的 Agent。

每个 Agent 生成的游戏命令都会包含固定路径参数、当前分片、UGC 目录、父进程监控、`-skip_update_server_mods` 和 `-cloudserver`。

存在 Pod 端口映射时，Agent 还会传入该分片的 `-external_port`。

## 参考资料

- [Klei：Dedicated Server Command Line Options Guide](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)
- [Klei：`-ugc_directory`](https://forums.kleientertainment.com/game-updates/dst/456207-r1496/)
- [Klei Forum：`-cloudserver` 的 FD 3、4、5](https://forums.kleientertainment.com/forums/topic/118972-unix-python-web-portal-for-dedicated-dst-server/#findComment-1344090)
