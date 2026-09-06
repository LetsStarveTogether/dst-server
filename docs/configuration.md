# 配置与部署

首次部署从项目 [快速开始](../README.zh-Hans.md#快速开始) 进入。
本文说明配置放在哪里、哪些设置必须保持一致，以及如何修改已有房间。

## 配置文件

一个集群使用一个宿主机目录，所有分片容器将它挂载为 `/cluster`。
以森林和洞穴为例：

```text
cluster/
├── cluster.ini
├── cluster_token.txt
├── adminlist.txt / blocklist.txt / whitelist.txt
├── mods/
│   ├── dedicated_server_mods_setup.lua
│   ├── modsettings.lua
│   └── ugc/
├── forest/
│   ├── server.ini
│   ├── worldgenoverride.lua
│   ├── modoverrides.lua
│   └── save/
│       ├── shardindex
│       ├── shardindex_time
│       ├── session/
│       └── …
└── cave/
    └── 与 forest 相同的分片文件
```

| 文件 | 管理的内容 |
| --- | --- |
| `cluster.ini` | 集群名称、访问限制、人数、玩法与分片连接设置。 |
| `cluster_token.txt` | Klei 专服令牌。 |
| 三个权限名单 | 管理员、封禁和白名单，每行一个标识符。 |
| `<shard>/server.ini` | 分片身份、玩家端口、Steam 查询端口和玩家存档路径编码方式。 |
| `<shard>/worldgenoverride.lua` | 世界生成与世界设置覆盖。 |
| `<shard>/leveldataoverride.lua` | 可选的完整关卡基线，事件世界需要。 |
| `<shard>/modoverrides.lua` | 当前分片启用的 Mod 和选项。 |
| `<shard>/save/` | 分片存档索引、世界与人物快照，以及运行环境的辅助数据；完整路径和内容见 [存档文件](../README.zh-Hans.md#存档文件)（[English](../README.md#save-files)）。 |
| `mods/` | 共享下载清单、Mod 行为配置、内容与缓存，详见 [Mod 更新](mods.md)。 |

`cluster.ini`、`cluster_token.txt` 和每个分片的 `server.ini` 必须存在，且只能有一个主分片。
集群根目录中除 `mods` 外的子目录都被视为分片；备份应放在集群目录外。
受管配置和分片目录不能使用符号链接。
准备阶段会补齐缺失的权限名单与 Mod 支持文件，运行期文件的位置见 [运行机制](runtime.md)。

## 分片与端口

同一 Pod 内的分片共享网络命名空间，通常使用 `master_ip = 127.0.0.1`。
多分片配置必须满足以下约束：

- 启用 `shard_enabled`，所有分片使用相同的非空 `cluster_key` 和 `master_port`。
- 每份 `server.ini` 显式声明 `is_master`，次分片必须有名称。
- 显式设置 ID 时，主分片为 `1`，次分片从 `2` 开始且不能重复。
- `master_port`、各分片的 `server_port` 与 `master_server_port` 不能冲突。

房间生成器为每个房间分配一个包含十个宿主机端口的槽位，只发布实际使用的端口。
玩家端口的宿主机映射通过 `-external_port` 公告，容器仍监听 `server.ini` 中的内部端口。
修改分片集合、主分片身份或发布端口时，需要重新生成配置与 Quadlet，并重建对应 Pod。
不要只修改其中一侧的端口。

`cluster.ini` 的 `[NETWORK]` 控制名称与访问限制，`[GAMEPLAY]` 控制人数、PVP 和空房暂停等玩法。
完整字段、范围及 SDK 默认值以 [ClusterSettings 与 ShardSettings](../src/dst_server/cluster/config.py) 为准。
`encode_user_path` 默认为 `True`，生成的 `server.ini` 总会显式写入该设置；显式传入或读取的 `False` 仍会保留。
INI 渲染通过 Pydantic 的 `model_dump(include=..., exclude_none=True)` 选择输出字段，不改变模型记录的显式字段，因此不会影响配置片段的合并。
此设置必须与玩家存档目录的编码方式一致，已有存档后不能只改配置而不迁移目录。

## 世界设置

`worldgenoverride.lua` 需要 `override_enabled = true` 才会生效。
标准森林可以使用：

```lua
return {
    override_enabled = true,
    worldgen_preset = "SURVIVAL_TOGETHER",
    settings_preset = "SURVIVAL_TOGETHER",
    overrides = {},
}
```

洞穴的两个 preset 使用 `DST_CAVE`。
无尽模式保留 `game_mode = survival`，通过森林的 `ENDLESS` preset 和洞穴的对应 overrides 表达。
优先组合 [内置房间片段](../src/dst_server/cluster/presets.py)，避免手动重复世界键。
`lavaarena` 与 `quagmire` 还需要完整的 `leveldataoverride.lua`，内置事件片段已包含这些数据。
`leveldataoverride.lua` 提供关卡基线，`worldgenoverride.lua` 随后应用覆盖。
生成参数不会重建已有地图，游戏也可能在保存时重写世界设置，因此应停服后修改。

## 配置 SDK

`ClusterConfig` 负责读取、验证和保存完整配置树，`RoomPreset` 用于组合房间配置。
以下示例在一个新目录生成无尽森林与洞穴：

```python
import os
import secrets
from pathlib import Path

from pydantic import SecretStr

from dst_server.cluster.presets import ENDLESS, FOREST_CAVES, compose

config = compose(FOREST_CAVES, ENDLESS).build(
    token=SecretStr(os.environ["DST_SERVER_CLUSTER_TOKEN"]),
    cluster_key=SecretStr(secrets.token_urlsafe(24)),
)
config.save(Path("cluster"))
```

这只生成游戏配置；生成配套容器配置使用 [generate_configured_room](../scripts/generate_rooms.py)。
已有配置可通过 `ClusterConfig.load(path)` 读取，再用 `.replace(...)` 得到修改后的模型。
配置 SDK 只解析受支持的声明式 Lua，不执行脚本，具体 Mod 边界见 [Mod 更新](mods.md)。
保存会规范化输出，不保留原注释与排版；替换按文件执行，不是整个配置树的跨文件事务。

对已部署的集群，使用 [ClusterClient](../src/dst_server/rpc/client.py) 修改：

1. 调用 `save()` 并确认成功，再调用 `stop()`。
2. 调用 `read_configuration()`，确认返回有效配置并保留 revision。
3. 将修改后的配置和原 revision 传给 `save_configuration()`。
4. 调用 `start()`，由控制器完成 Mod 准备并启动分片。

revision 冲突时应重新读取并合并修改，不能直接覆盖。
RPC 写入要求所有游戏进程停止，并拒绝修改分片拓扑、`server_port` 和 `master_server_port`。
内部 `master_port` 可在所有分片保持一致的前提下修改，不需要更改 Pod 的端口发布。
停服和重启不会隐式保存游戏。

## 导出存档

安装包含 `py7zr` 和 `obstore` 的导出依赖后，可将已有配置和存档打包为 `.7z`：

```shell
pip install 'dst-server[export]'
```

```python
import shutil
from pathlib import Path

from dst_server.cluster.archive import export_cluster

with export_cluster(Path("/srv/dst/001")) as archive:
    destination = Path("/path/to/exports") / archive.filename
    with destination.open("xb") as output:
        shutil.copyfileobj(archive.stream, output)
```

将示例导出目录替换为自己的已有目录，持久文件的路径和后续删除由调用者负责。
SDK 内部使用匿名 `TemporaryFile`，`archive.stream` 支持读取和定位，离开 `with` 后自动关闭并清理。
默认文件名形如 `DST-001-20260908T010203Z.7z`，由目录名或指定的 `room_id` 与 UTC 时间组成。
归档使用 ZSTD 级别 22 压缩，请用 `py7zr` 或 `7-Zip-zstd` 读取；原版 `7z` 不一定提供该解码器。

也可先用 `config = ClusterConfig.load(directory)` 读取配置。
调用 `with export_cluster(directory, configuration=config) as archive:` 时可复用该配置对象。
目录参数始终必填，因为配置模型不拥有磁盘中的存档文件。
默认 `encode_user_path=True` 按源分片 `server.ini` 的设置决定是否转换玩家目录：已启用编码的分片直接保留路径，其余分片转换为游戏使用的编码。
导出配置与 `shardindex` 中的标志会同步为 `True`。
传入 `encode_user_path=False` 会保留原设置与玩家目录名。

上传至 R2 时，在调用进程中设置以下环境变量：

```shell
export AWS_ENDPOINT='https://<account-id>.r2.cloudflarestorage.com'
export AWS_BUCKET='your-bucket'
export AWS_ACCESS_KEY_ID='your-access-key-id'
export AWS_SECRET_ACCESS_KEY='your-secret-access-key'
```

内部的 `S3Store(region="auto")` 直接读取这些 [obstore 原生环境变量](https://developmentseed.org/obstore/latest/api/store/aws/#obstore.store.S3Config)。
区域固定为 R2 使用的 [`auto`](https://developers.cloudflare.com/r2/api/s3/api/#bucket-region)，无需设置 `AWS_REGION`。

```python
from pathlib import Path

from dst_server.cluster.archive import export_cluster

with export_cluster(Path("/srv/dst/001")) as archive:
    key = archive.upload()

print(key)
```

`upload()` 返回桶内对象 key，格式为 `<ULID>/<原 DST-id-UTC.7z 文件名>`，保留文件名并用独立前缀避免同秒上传覆盖。
上传从流开头读取，异常直接传给调用者，离开 `with` 时仍会清理本地匿名临时文件。
大文件的 multipart 上传由 [obstore 原生处理](https://developmentseed.org/obstore/latest/api/put/)。
R2 默认在发起上传七天后清理未完成分片，具体时间可通过 [桶生命周期规则](https://developers.cloudflare.com/r2/buckets/object-lifecycles/) 修改，上传失败不保证远端分片立即清理。

归档保留 SDK 支持的游戏配置和 `save/session/` 下的全部普通文件，包括玩家快照、元数据与 `savelocation`。
必要的 `save/shardindex` 会保留世界、会话和 Mod 信息，同时清除其中的密码等凭据。
额外进度文件 `save/recipebook`、`save/reforged_achievements_server` 和 `save/mod_config_data/mod_worldjump_data_*` 也会保留。
日志、权限名单、专服令牌、Mod 内容与 UGC 缓存、临时文件及其余辅助索引不进入归档。
导出配置清除 `cluster_password`，并以新生成的共享 `cluster_key` 替换原部署密钥，供导出的分片互相连接。
归档不包含 `cluster_token.txt`，通过配置 SDK 读取或部署前需自行补充对应凭据。
旧版仅含 `saveindex` 的存档需先由游戏迁移，损坏或非受支持格式的 `shardindex` 会使导出失败。

输入目录必须已经保存并保持静止，导出不会自动保存或停止游戏。
检测到文件变化时会失败，但变化检测不能保证在线多分片的原子快照。
目前提供导出与 R2 上传，导入尚未实现。

## 游戏启动参数

常规部署由 Agent 构造游戏命令，用户不需要手写以下参数：

| 参数 | 本项目中的用途 |
| --- | --- |
| `-persistent_storage_root`、`-conf_dir`、`-cluster`、`-shard` | 定位集群与分片配置；容器最终读取 `/cluster`。 |
| `-external_port` | 公告映射后的玩家端口。 |
| `-ugc_directory` | 指定共享 UGC 缓存。 |
| `-only_update_server_mods`、`-skip_update_server_mods` | 分开执行更新与游戏启动，详见 [Mod 更新](mods.md)。 |
| `-monitor_parent_process` | 父进程退出时关闭游戏进程。 |
| `-cloudserver` | 开启 Agent 与游戏之间的本地通信，详见 [运行机制](runtime.md)。 |

自行管理游戏进程时，可通过 [ServerConfig](../src/dst_server/runtime/config.py) 设置路径与 `extra_args`。
其他原生参数见 [Klei 命令行指南](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)。

## 容器用户与目录权限

镜像以 `steam` 用户运行，UID/GID 均为 `1000`。
房间生成器的映射选项默认留空，部署时应显式指定。

rootful 部署以 root 生成和维护配置，传入 `--volume-idmap 'uids=0-1000-1;gids=0-1000-1'`，每个分片的 `.container` 使用以下挂载：

```ini
[Container]
Volume=/srv/dst/000:/cluster:idmap=uids=0-1000-1;gids=0-1000-1
```

该映射保留宿主目录和文件的 `root:root` 属主，容器内显示为 `1000:1000`。
容器新建的文件在宿主上也属于 root，因此以 root 生成的 `0600` 配置始终可由容器读取。
宿主内核和数据目录所在文件系统必须支持 [idmapped mount](https://docs.podman.io/en/latest/markdown/podman-run.1.html#volume-v-source-volume-host-dir-container-dir-options)。

rootless 部署由拥有集群目录的普通用户生成配置，传入 `--userns 'keep-id:uid=1000,gid=1000'`，通过用户 systemd 管理器启动。
挂载保持普通 bind mount，映射设置在 `.pod` 的 `[Pod]` 中，由所有分片容器继承：

```ini
[Pod]
UserNS=keep-id:uid=1000,gid=1000
```

该设置将宿主部署用户映射为容器内的 `1000:1000`，宿主文件仍由该部署用户拥有。
集群目录必须由对应部署用户拥有，且不能允许组或其他用户写入，以满足 RPC socket 的目录检查。
映射配置修改后，应重新生成 Quadlet 并重建对应 Pod。

`QuadletApplication.for_cluster` 与四个 `generate_*` 函数接受 `volume_idmap` 和 `userns` 参数。
两者默认均为 `None`，不生成 idmap 或 `UserNS` 设置，也不根据执行用户自动选择。

## 容器 DNS

rootful Podman 可通过 [DNS 策略](../deploy/containers/podman-dns.json) 将默认网络的查询交给宿主机 systemd-resolved。
前提是宿主机的 `127.0.0.53` stub 正常工作；若 resolved 已配置 DNS over TLS，容器查询会复用其上游策略。
这是默认网络的宿主机级设置，会影响该网络上的其他容器。

在目标机上生成候选配置，保留该机器的网络身份和地址：

```shell
podman network inspect podman |
  jaq --slurpfile dns deploy/containers/podman-dns.json \
    '.[0] | del(.containers) | . + $dns[0]'
```

策略文件不能直接安装为完整 Podman 网络配置。
先备份网络配置、保存游戏，再停止默认网络上的所有容器，包括 infra 和非 DST 容器。
确认候选结果只有 DNS 字段变化后，以 `0644` 权限安装到 `/etc/containers/networks/podman.json`。
重新启动受影响的 Pod 和服务，并在容器内验证 DNS。
只重启游戏进程或执行 `podman network reload` 不会完整应用这项修改。
