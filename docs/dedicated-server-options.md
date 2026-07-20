# DST 专用服务器启动参数

这些参数属于 `dontstarve_dedicated_server_nullrenderer`，不是 Podman 参数。

本项目会自动选择存储目录、集群、分片和 Mod 目录，正常使用容器时不需要手工传入。

命令行参数会覆盖 `cluster.ini` 或 `server.ini` 中的同名配置。

## 配置路径

以下四个参数共同决定服务端读取哪个集群和分片：

| 参数 | 作用 |
| --- | --- |
| `-persistent_storage_root <绝对路径>` | 配置根目录。Linux 默认值为 `~/.klei`。 |
| `-conf_dir <目录名>` | 配置目录名，不能包含斜杠。默认值为 `DoNotStarveTogether`。 |
| `-cluster <目录名>` | 集群目录名。默认值为 `Cluster_1`。 |
| `-shard <目录名>` | 分片目录名。默认值为 `Master`。 |

最终读取的文件是：

```text
<persistent_storage_root>/<conf_dir>/<cluster>/cluster.ini
<persistent_storage_root>/<conf_dir>/<cluster>/<shard>/server.ini
```

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `-offline` | 以离线模式启动。服务端不会公开列出，只允许局域网玩家加入，Steam 相关功能不可用。 |
| `-disabledatacollection` | 禁用数据收集，同时把服务端限制为离线模式。 |
| `-bind_ip <地址>` | 覆盖监听玩家连接的地址。普通部署不需要设置。 |
| `-port <端口>` | 覆盖 `[NETWORK] / server_port`。有效范围为 1024–65535；同一主机上的每个分片必须不同；局域网发现要求 10998–11018。 |
| `-players <人数>` | 覆盖 `[GAMEPLAY] / max_players`。有效范围为 1–64。 |
| `-steam_master_server_port <端口>` | 覆盖 `[STEAM] / master_server_port`。同一主机上的每个服务端进程必须不同。 |
| `-steam_authentication_port <端口>` | 覆盖 `[STEAM] / authentication_port`。同一主机上的每个服务端进程必须不同。 |
| `-tick <频率>` | 覆盖 `[NETWORK] / tick_rate`。有效范围为 15–60；默认值 15 通常已经足够。 |
| `-fo` | 只允许好友加入。 |
| `-token <令牌>` | 直接传入集群令牌。优先使用 `cluster_token.txt`，避免令牌出现在进程参数中。 |

## 日志、Mod 与进程管理

| 参数 | 作用 |
| --- | --- |
| `-backup_log_count <数量>` | 保留的日志备份数，默认 100。旧参数 `-backup_logs` 已被替换。 |
| `-backup_log_period <秒>` | 日志备份间隔，默认 86400 秒。 |
| `-only_update_server_mods` | 更新 `dedicated_server_mods_setup.lua` 中列出的 Mod，完成后退出。适合多分片启动前只执行一次。 |
| `-skip_update_server_mods` | 启动时跳过 Mod 更新。应在已经单独完成更新后使用。 |
| `-ugc_directory <路径>` | 覆盖 v2/UGC Mod 的存储目录。相对路径以游戏安装目录的 `data` 目录为基准，部署脚本应优先传绝对路径。 |
| `-allow_ioopenwrite_sandbox_escape` | 允许 Mod 和工具使用 `io.open` 与 `io.write`。这会放宽沙箱边界，只应对可信代码启用。 |
| `-monitor_parent_process <PID>` | 指定的父进程退出时自动关闭服务端。 |

## `-cloudserver`

`-cloudserver` 是 Linux 专用的进程间通信模式。

它让服务端通过 FD 3 接收 Lua，通过 FD 4 返回命令结果，并通过 FD 5 发送生命周期和状态消息。

Klei 的公开参数表没有列出它，但 Klei 开发者在论坛中说明了接口和基本协议。

Python SDK 的 `ServerConfig` 始终启用该参数，具体机制见 [`-cloudserver` 双向通信](cloudserver-ipc.md)。

容器的 `entrypoint.sh` 当前不使用该模式。

## Python SDK 实际使用的参数

`ServerConfig` 生成的命令大致如下：

```shell
dontstarve_dedicated_server_nullrenderer_x64 \
  -persistent_storage_root / \
  -conf_dir . \
  -cluster cluster \
  -shard Master \
  -ugc_directory /cluster/mods/ugc \
  -monitor_parent_process <PID> \
  -skip_update_server_mods \
  -cloudserver
```

`dst_server.cluster.service` 会先单独更新一次 Mod，再为每个分片启动一个服务端进程，所以分片进程本身使用 `-skip_update_server_mods`。

## 参考资料

- [Klei：Dedicated Server Command Line Options Guide](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)
- [Klei Forum：Dedicated Server Command Line Options Guide](https://forums.kleientertainment.com/forums/topic/64743-dedicated-server-command-line-options-guide/)
- [Klei Forum：2022 Updated Dedicated Server Quick Setup Guide - Linux](https://forums.kleientertainment.com/forums/topic/140715-2022-updated-dedicated-server-quick-setup-guide-linux/)
- [Klei Game Update 129926：Mod 更新参数](https://forums.kleientertainment.com/forums/topic/51981-game-update-129926-3122015/)
- [Klei Game Update 456207：`-ugc_directory`](https://forums.kleientertainment.com/game-updates/dst/456207-r1496/)
- [Klei Forum：`-cloudserver` 的 FD 3、4、5](https://forums.kleientertainment.com/forums/topic/118972-unix-python-web-portal-for-dedicated-dst-server/#findComment-1344090)
