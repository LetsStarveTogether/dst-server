# 饥荒联机版专用服务器

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

通过 Rust SDK、Agent、CLI 和 Python 绑定管理饥荒联机版服务端。
每个房间使用一个容器，由一个 Agent 管理全部配置分片；闭馆期间游戏进程停止，Agent 继续运行。
发布的二进制和 Python wheel 支持 Linux x86_64、CPython 3.14 和同机可信调用。

`0.4.0` 使用 Rust 重写了原有 Python 实现。
接口变化、验证方法及已知限制见 [迁移指南](docs/migration.md)。
内部就绪不代表公网可达。

在 CPython 3.14 环境中执行 `pip install dst-server==0.4.0`，安装 Python SDK。
[GitHub Release](https://github.com/LetsStarveTogether/dst-server/releases/tag/v0.4.0) 同时提供独立的 Linux x86_64 CLI。

## 构建并创建房间

安装 Rust 1.99、C/C++ 编译工具、Cap'n Proto 和 [uv](https://docs.astral.sh/uv/)。
宿主部署还需要 Podman、Quadlet 和 systemd。
在本仓库运行：

```sh
cargo build --locked --release -p dst-server --bin dst-server
export PATH="$PWD/target/release:$PATH"
uv sync --python 3.14
dst-server --help
uv run python -m dst_server --help
```

原生二进制和 Python 入口调用同一个 Rust CLI。
从当前代码构建游戏镜像，指定 SteamCMD 实际安装的游戏版本：

```sh
podman build --target game --build-arg GAME_VERSION=756039 \
  -t localhost/dst-server:rust .
```

构建过程核对游戏版本，并打包匹配的 SDK Lua 资源。
创建房间时使用匹配的发布镜像，也可以选择本地构建的镜像。

默认部署镜像为 `quay.io/wh2099/dst-server:latest`。
通过 `deployment.image` 或 `--image` 选择 `quay.io/wh2099/dst-server:beta` 可使用测试版。
生成的 Quadlet 保留这些标签，并为这两个发布渠道设置 `AutoUpdate=registry`。
Quadlet 使用 `Pull=never`，首次启动前先拉取所选镜像。
在主机执行 `systemctl enable --now podman-auto-update.timer`，启用定期检查；发现新镜像后自动更新并重启容器。

创建 Klei 服务端令牌，保存到 `/run/secrets/dst_cluster_token`。
使用拥有房间目录和 systemd 服务管理权限的账号运行：

```python
import asyncio
from pathlib import Path

from dst_server import Host
from dst_server.settings import Room, build_template


async def main():
    cluster = build_template(
        "pure_survival",
        number=299,
        token=Path("/run/secrets/dst_cluster_token").read_text().strip(),
    )
    room = Room(
        number=299,
        template="pure_survival",
        cluster=cluster,
        deployment={
            "image": "localhost/dst-server:rust",
            "volume_idmap": "uids=0-1000-1;gids=0-1000-1",
        },
    )
    await Host("/srv/dst", "/etc/containers/systemd").create(room)


asyncio.run(main())
```

这会创建 `/srv/dst/299` 和 `dst-299.container`，随后可以启动服务。
示例 ID 映射让镜像内 UID/GID 1000 访问 root 所有的房间文件；请按实际宿主账号选择所有权或映射。

```sh
dst-server host start --room 299
dst-server host status --room 299
dst-server call list_players --socket /srv/dst/299/.dst-agent.sock
journalctl -u dst-299.service -n 100 -f
```

通过 `host --root PATH --quadlet-dir PATH` 指定目录，`host --user` 使用用户 systemd 管理器。
房间编号支持 `000–299`，与玩法模板分别选择。
内置 120 个 LST 房间为 `000–099` 和 `200–219`：

```sh
dst-server template list
dst-server host fleet --room 000,030,209 \
  --token-file /run/secrets/dst_cluster_token --image localhost/dst-server:rust \
  --volume-idmap 'uids=0-1000-1;gids=0-1000-1'
```

## CLI

| 命令 | 用途 |
| --- | --- |
| `host` | 创建、批量生成、修改和管理宿主服务；按房间、模板或全部房间选择。 |
| `call` | 通过 Agent socket 调用房间或分片操作。 |
| `describe` | 查看方法、作用范围、参数 schema 和默认超时。 |
| `subscribe` | 读取有界的 `logs`、`lifecycle`、`events` 批次及丢弃计数。 |
| `logs`、`archive` | 查询或跟随历史日志，导出及上传一致的房间存档。 |
| `console` | 在指定分片执行可信 Lua，支持文本、文件和交互输入。 |
| `config`、`template`、`inspect` | 校验配置、查看 schema 和模板、发现原生分片拓扑。 |
| `scripts`、`annotations`、`completion` | 构建及验证脚本包、生成 Lua 注解及 shell 补全。 |
| `agent` | 运行房间监督进程，通常作为容器入口。 |

命令输出 JSON；`--json` 使用紧凑 JSON 和结构化错误。
批量操作保留每个房间的结果，任何目标失败都会使退出码非零。
JSON 参数支持直接文本、`@文件名` 或从 stdin 读取的 `-`。

```sh
dst-server --json host list
dst-server host show 299 --field /cluster/settings/max_players
dst-server host schema
dst-server host diagnose --room 299
dst-server describe
dst-server call save --socket /srv/dst/299/.dst-agent.sock
dst-server call get_player '{"userid":"KU_example"}' --socket /srv/dst/299/.dst-agent.sock
dst-server subscribe lifecycle --socket /srv/dst/299/.dst-agent.sock
```

`call stop` 关闭游戏并保留 Agent，日程可以再次开服。
`host stop --room 299` 停止整个容器服务，包括 Agent。
离线修改配置之前使用后者：

```sh
dst-server host stop --room 299
dst-server host edit --room 299 --set '/cluster/settings/max_players=9'
dst-server host start --room 299
```

游戏停止时，常驻 Agent 仍持有房间锁；此时的维护应通过 Agent 接口协调。
修改世界生成参数会保留已有存档；`regenerate` 才会生成新世界。

## Rust 与 Python SDK

Rust 原生 API 使用明确的请求类型：

```rust
use dst_server::{model::{Envelope, Request, Target}, rpc::Client};

async fn inspect_room() -> dst_server::model::Result<serde_json::Value> {
    let client = Client::connect("/srv/dst/299/.dst-agent.sock").await?;
    let result = client.call(Envelope::new(Target::Room, Request::Status {})?).await;
    client.close().await?;
    result
}
```

Python 提供异步调用及连接、订阅的显式清理：

```python
import asyncio
from dst_server import Client


async def main():
    async with await Client.connect("/srv/dst/299/.dst-agent.sock") as client:
        print(await client.call("status"))
        print(await client.call("save"))
        async with await client.subscribe("events") as events:
            print(await events.next(max_items=100))


asyncio.run(main())
```

使用 `describe()` 或 `dst-server describe --shard` 查看准确契约。
接口覆盖世界查询、保存、回档、重建、暂停、玩家查询与修改、迁移、权限、Mod 和导出。
玩家操作按 `userid` 查找分片，并在执行前复核身份。
需要指定分片时传入 `shard="forest"`。
实体引用包含世界 session 和运行世代，重载后的旧引用失效。

每个房间同时执行一个修改操作，冲突返回 `busy`。
已受理操作由 Agent 持有；调用方超时、断线或取消 Python 等待，不会撤销操作。
结果未知时先查询 `status`，再决定是否重试。
保存成功要求全部分片完成原生保存流程；正常停服要求退出码为零、进程已回收、输出已排空，且没有强制终止或协议故障。
停服失败仍保留各分片的退出状态与已确认存档；信号终止使用负信号编号表示。
停服默认等待 220 秒，包含公告倒计时；调整公告延时会同步调整等待预算。
主动禁用存档的 Mod 可以正常停服并返回 `saved_snapshot: null`；显式保存没有确认新快照时返回错误。
迁移成功要求目标世界确认玩家已进入。
控制台成功只表示 Lua 代码返回，该代码启动的异步行为可能仍在进行。

## 配置与文件

```text
/srv/dst/299/
├── cluster.ini
├── cluster_token.txt
├── adminlist.txt / blocklist.txt / whitelist.txt
├── .dst-control.json
├── .dst-room.lock
├── .dst-agent.sock
├── mods/
│   ├── dedicated_server_mods_setup.lua
│   ├── modsettings.lua
│   └── ugc/
├── forest/
│   ├── server.ini
│   ├── worldgenoverride.lua
│   ├── modoverrides.lua
│   └── save/
└── cave/
    └── server.ini、世界配置、Mod 和存档
```

原生 INI/Lua 文件是游戏配置来源，Quadlet 定义部署。
`.dst-control.json` 保存模板标签、运营策略、活动记录和恢复状态。
更换容器时保留该文件，以延续恢复额度和维护进度。
房间存在期间不得删除或替换 `.dst-room.lock`。

含有 `server.ini` 的目录就是启用的分片。
分片名可以任意指定，要求唯一主分片、共享密钥一致、分片 ID 唯一，以及游戏、查询、分片端口没有冲突。
按实际分片数量分配端口，在宿主锁内持久化映射，取消旧版最多四分片的限制。
默认宿主端口池为 `30000–65535`。
全部分片共享容器网络和 Mod 目录，每个游戏进程独立持有控制管道及输出读取器。

Python 配置工厂调用 Rust 完成验证：

```python
from dst_server.settings import ClusterSettings, RoomStore, build_template

settings = ClusterSettings(max_players=9, cluster_name="周五游戏")
cluster = build_template("pure_endless", number=299, settings=settings)
print(cluster.dump(defaults=True))
print(settings.schema())

store = RoomStore("/srv/dst")
room = store.load(299)
updated = room.edit("/cluster/settings/cluster_description", "周五游戏")
# 停止整个服务后写入：
# store.save(updated)
```

`dump()` 默认遮蔽令牌、共享密钥和密码，显式 `secrets=True` 才返回原值。
渲染后的原生文件和 `files()` 包含真实凭据。
默认值按需补齐，缺省字段与显式 `None` 保持区别。
冻结的 schema、默认值和预设随 Rust 打包，包含 12 个模板和全部 120 个房间预设。
业务验证及文件操作集中在 Rust，Python 保留绑定、转换和异步封装。

离线写入持有房间锁，拒绝符号链接，并通过可恢复事务同步提交文件。
常规配置保存保留游戏更新的管理员、封禁和白名单；在线修改使用权限接口。
结构化 Lua 读取只静态解析 Lua 5.1 字面量。
动态配置可以交给游戏原生加载，但不能通过结构化配置接口修改。

## 日程、维护与恢复

默认策略使用 `Asia/Shanghai` 时区，全天开放，开启 Mod 自动维护，关闭闲置世界重建。
可以向运行中的 Agent 更新策略：

```sh
dst-server call set_policy - --socket /srv/dst/299/.dst-agent.sock <<'JSON'
{
  "policy": {
    "timezone": "Asia/Shanghai",
    "schedule": [{"start": "18:00", "end": "00:00"}],
    "mod_auto_update": true,
    "idle_regeneration": false
  }
}
JSON
```

Agent 每 30 秒检查日程，支持跨午夜窗口。
闭馆时停止全部游戏，保留 Agent 等待下一次开放；待处理的 Mod 维护在开门前执行。
默认维护倒计时 60 秒，每 30 秒公告一次。
下载或校验失败最多额外尝试两次，间隔五分钟；仍失败则保持关闭，等待明确维护请求或下个开放期。

必需分片崩溃或持续失控会停止整个房间。
异常退出、强制终止和 IPC 故障需要重建容器，恢复状态跨容器保留。
可重试故障最多使用当前存档重启两次，间隔 30 秒。
只有确认的存档加载失败，才允许再尝试一次前一个完整房间快照；每个世界的额外回退须能证明不超过一个游戏日。
修改前固定全部目标，中断后继续同一目标。
状态未知、目标缺失、故障原因不受支持或恢复失败时保持关闭。
连续稳定运行 30 分钟后恢复额度；额度耗尽后，下个开放期可重新尝试，全天开放房间需显式启动。

闲置世界重建只在显式启用、开放时段、全部分片就绪且可靠确认无人时执行。
按世界天数使用 6、24、36、72、168 小时保留期。
活动观察中断或世界变化会重算保留期，正常计划闭馆保留闲置计时。

## 日志、导出与工具

默认游戏事件 profile 为 `history`，`agent --profile` 还支持 `critical` 和 `off`。
事件采用有界、尽力交付；源事件缺口、订阅溢出、输出丢弃和导出拒收分别计数。
订阅不提供历史重放或可靠业务触发。

日志、指标和追踪使用 OTLP gRPC。
`OTEL_EXPORTER_OTLP_LOGS_*`、`METRICS_*`、`TRACES_*` 覆盖通用 `OTEL_EXPORTER_OTLP_*` 参数。
可以配置 endpoint、headers、TLS 证书、压缩和超时；超时单位为毫秒。
`OTEL_*_EXPORTER=none` 关闭对应信号，`OTEL_SDK_DISABLED=true` 关闭全部导出。
结构化日志传输保留 null、整数、浮点、数组和对象；Netdata 扁平查询有独立的类型限制。

`dst_server.logs` 提供 `JournalLogs`、`JournalQuery`、`JournalStream`、`NetdataLogs` 和 `NetdataLogQuery`。
CLI 使用相同查询字段，支持游标、方向及过滤条件：

```sh
dst-server logs journal --unit dst-299.service --query '{"limit":100,"since":"today"}'
dst-server logs journal --unit dst-299.service --follow
dst-server logs telemetry '{"since":1790899200,"until":1790985600,"limit":100}'
```

跟随模式逐条输出记录；中断日志查询时会等待读取进程退出。
`dst_server.events` 通过 `event_schema()` 和 `validate_event()` 提供原生事件 schema 及验证。
`dst_server.KleiClient` 提供有界的版本、区域、大厅和房间查询，批量查询保留每项失败。
请求失败和成功但没有结果分别表达。

存档导出在全部游戏停止后准备数据，保留原生世界、玩家及 Mod 进度，移除凭据、权限名单和 SDK 运行文件。
默认使用 7z、Zstd 3，同一时间只运行一个压缩器。
上传通过原生对象存储实现，失败或取消时显式终止分段上传；清理失败进入结果和日志。
Python 提供 `dst_server.archive.export_cluster` 和 `ClusterArchive.save/upload`。
导出要求游戏停止，并通过常驻 Agent 或离线独占锁协调。
离线导出到本地文件：

```sh
dst-server host stop --room 299
dst-server archive export 299 --output /tmp/room-299.7z
```

输出文件必须尚不存在。
入口和验证情况见 [迁移记录](docs/migration.md)。

```sh
dst-server scripts build /path/to/native/scripts.zip --output /tmp/scripts.zip
dst-server scripts verify /tmp/scripts.zip --source /path/to/native/scripts.zip
dst-server annotations components /path/to/scripts/components --output /tmp/components.lua
dst-server annotations modutil /path/to/scripts/modutil.lua --output /tmp/modutil.lua
```

## 开发与验证

工作区包含 [`crates/dst-server`](crates/dst-server) 和 [`crates/dst-server-python`](crates/dst-server-python)。
Python 封装位于 [`python/dst_server`](python/dst_server)，Lua 资源位于 [`resources/lua`](resources/lua)。
容器直接运行 Rust 二进制。

```sh
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked -p dst-server --all-targets
uv run ruff check python tests/rust tools
uv run ty check python
uv run python tests/rust/check_lua.py
uv build --python 3.14 --out-dir dist --no-sources
```

CI 从源码包构建 wheel，在隔离环境安装并运行绑定和进程测试。
Lua 契约使用 Lua 5.1 和 LuaJIT 验证。
游戏版本 `756039` 在五个互联分片中出现过尚未解决的 [原生停服崩溃](docs/migration.md#known-native-shutdown-failure)。
暴食和熔炉主动禁用保存；SDK 返回保存未确认错误，仍可正常关闭游戏。
真实游戏探针只用于临时目录和测试容器。
构建清单记录 SDK、Lua、协议、游戏版本和产物摘要。
验证方法和部署步骤见 [迁移指南](docs/migration.md)。
