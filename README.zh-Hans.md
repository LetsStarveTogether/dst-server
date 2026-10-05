# 饥荒联机版专用服务器

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

通过 Rust SDK、Agent、CLI 和 Python 绑定管理饥荒联机版服务端。
每个房间使用一个容器，由常驻 Agent 管理全部配置分片。
Agent 按房间策略启动和关闭游戏，闭馆期间保持控制接口可用。
发布的二进制和 Python wheel 支持 Linux x86_64、CPython 3.15 和同机可信调用。

`0.4.0` 使用 Rust 重写了原有 Python 实现。
每日开放和关闭均由 Agent 管理，无需外部调度器。
接口变化、验证方法及已知限制见 [迁移指南](docs/migration.md)。
内部就绪不代表公网可达。

在 CPython 3.15 环境中执行 `pip install dst-server==0.5.0`，安装 Python SDK。
[GitHub Release](https://github.com/LetsStarveTogether/dst-server/releases/tag/v0.5.0) 同时提供原生 Linux x86_64 CLI，需要 libarchive 3.7 或更新版本。
Python wheel 已包含 libarchive 及其所需的共享库，相关版权和许可文本位于 `.dist-info/licenses/`。

## 构建并创建房间

安装最新版稳定 Rust、C/C++ 编译工具、Cap'n Proto、pkg-config、libarchive 开发文件和 [uv](https://docs.astral.sh/uv/)。
宿主部署还需要 Podman、Quadlet 和 systemd。
在本仓库运行：

```sh
cargo build --locked --release -p dst-server --bin dst-server
export PATH="$PWD/target/release:$PATH"
uv sync
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
内置 121 个 `LST-朗诵团` 房间为 `000–100` 和 `200–219`。
生成的房间名使用 `LST-朗诵团` 前缀，后接房间编号和玩法名称。
`100` 号房间 `LST-朗诵团-100-测试尝鲜` 使用官方 Beta 游戏版本，沿用纯净无尽配置。
地面使用游戏原生 `ENDLESS` 预设，洞穴沿用纯净无尽模板的对应覆盖项。

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
| `logs`、`archive` | 查询或跟随历史日志，导入、导出及上传原生房间存档。 |
| `console` | 在指定分片执行可信 Lua，支持文本、文件和交互输入。 |
| `config`、`template`、`inspect` | 校验配置、查看模板、发现原生分片拓扑。 |
| `scripts`、`annotations`、`completion` | 构建及验证脚本包、生成 Lua 注解及 shell 补全。 |
| `agent` | 运行房间监督进程，通常作为容器入口。 |

命令输出 JSON；`--json` 使用紧凑 JSON 和结构化错误。
批量操作保留每个房间的结果，任何目标失败都会使退出码非零。
JSON 参数支持直接文本、`@文件名` 或从 stdin 读取的 `-`。

```sh
dst-server --json host list
dst-server host show 299 --field /cluster/settings/max_players
dst-server config validate ShardSettings '{}' --resolved
dst-server host diagnose --room 299
dst-server describe
dst-server call save --socket /srv/dst/299/.dst-agent.sock
dst-server call get_player '{"userid":"KU_example"}' --socket /srv/dst/299/.dst-agent.sock
dst-server subscribe lifecycle --socket /srv/dst/299/.dst-agent.sock
```

`call stop` 正常关闭全部分片，随后 Agent 和容器以退出码 `0` 结束。
`host stop --room 299` 停止整个容器服务，包括 Agent。
停止容器后，自动调度也随之停止，直到重新启动服务。
`call restart` 在同一个 Agent 内重启游戏，开放时段会等待全部分片就绪。
`call start` 请求重新执行开放策略，并为全天开放房间重置耗尽的恢复额度。
使用 `host start` 或 `host restart` 等待全部分片就绪。
非开放时段启动会保持 Agent 在线、游戏关闭；恢复额度仍然耗尽则报告错误。
离线修改配置之前停止服务：

```sh
dst-server host stop --room 299
dst-server host edit --room 299 --set '/cluster/settings/max_players=9'
dst-server host start --room 299
```

容器退出后，`host status` 直接读取已保存的策略、当前日程、维护和恢复状态，无需 Agent socket。
修改世界生成参数会保留已有存档；`regenerate` 才会生成新世界。

## Rust 与 Python SDK

RPC 的请求、错误、结果和订阅记录使用 Cap'n Proto 原生字段编码。
动态游戏及 MOD 数据使用递归的值类型，数字、文本、列表和对象都以二进制字段传输。
Rust 的 `serde_json::Value` 和 Python 字典用于表示 SDK 内存中的值。

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
结果未知时先查询 `status`；容器已退出则使用 `host status`，再决定是否重试。
正常停服向每个分片发送一次原生 `c_shutdown()`，由游戏完成最终保存。
原生退出流程内部可能执行多次保存。
保存返回 `true` 表示原生命令已接受请求，磁盘写入可能随后完成。
正常停服要求退出码为零、进程已回收、输出已排空，且没有强制终止或协议故障。
停服失败仍保留各分片的退出状态、强制终止标记和输出排空结果；信号终止使用负信号编号表示。
停服默认等待 220 秒，包含公告倒计时；调整公告延时会同步调整等待预算。
主动禁用存档的 Mod 可以接受保存请求并正常停服，但不生成快照。
迁移返回原生接受结果；后续可查询玩家位置来观察进度。
控制台成功只表示 Lua 代码返回，该代码启动的异步行为可能仍在进行。

SDK 生成的唯一 ID 统一使用 Rust `ulid` 库生成的标准大写 ULID，包括启动批次、进程实例、事件与日志、在线状态观察周期、请求、操作、归档产物和服务实例。
同一事件在订阅、本地输出、OTLP 导出及重试中保留相同 ID。
进程 ID 在世界重载时保持稳定，在新进程启动时重新生成；观察周期 ID 在进程、世界世代或连续观察发生变化时重新生成。
Lua 提供本地观察周期计数，由 Rust 分配对外使用的 ULID。
游戏原生 ID、房间编号、系统 ID 和 OpenTelemetry trace/span ID 遵循各自协议。

## 配置与文件

```text
/srv/dst/299/
├── cluster.ini
├── cluster_token.txt
├── adminlist.txt / blocklist.txt / whitelist.txt
├── .dst-server.json
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
`.dst-server.json` 统一保存模板标签、运营策略、活动记录、恢复状态和公共 Lua 启动参数。
Agent 在启动游戏前更新 `driver` 字段，并将生成的参数写入各分片的 `dst_server_driver.json`。
Lua 通过原生存储接口读取该启动文件；该接口拒绝跨出分片目录的路径。
首次访问时，持有房间锁后将旧 `.dst-control.json` 迁移为新名称。
发布端口直接从 Quadlet 读取，不再维护独立的端口登记文件。
更换容器时保留该文件，以延续恢复额度和维护进度。
房间存在期间不得删除或替换 `.dst-room.lock`。

含有 `server.ini` 的目录就是启用的分片。
分片名可以任意指定，要求唯一主分片、共享密钥一致、分片 ID 唯一，以及游戏、查询、分片端口没有冲突。
按实际分片数量分配端口，在宿主锁内持久化映射，取消旧版最多四分片的限制。
内置房间的宿主 UDP 端口以 `30000 + 房间编号 × 10` 为基址，主世界游戏/查询端口偏移为 `0/1`，副世界为 `2/3`。
固定端口发生冲突时，部署会报错。
未指定映射的自定义部署使用默认宿主端口池 `30000–65535`。
全部分片共享容器网络和 Mod 目录，每个游戏进程独立持有控制管道及输出读取器。

Python 配置工厂调用 Rust 完成验证：

```python
from dst_server.settings import ClusterSettings, RoomStore, build_template

settings = ClusterSettings(max_players=9, cluster_name="周五游戏")
cluster = build_template("pure_endless", number=299, settings=settings)
print(cluster.dump(defaults=True))
print(settings.render())

store = RoomStore("/srv/dst")
room = store.load(299)
updated = room.edit("/cluster/settings/cluster_description", "周五游戏")
# 停止整个服务后写入：
# store.save(updated)
```

`dump()` 默认遮蔽令牌、共享密钥和密码，显式 `secrets=True` 才返回原值。
渲染后的原生文件和 `files()` 包含真实凭据。
默认值按需补齐，缺省字段与显式 `None` 保持区别。
配置字段、默认值和 INI 节位置由 Rust 类型定义，Serde 和 `garde` 负责输入校验。
`encode_user_path` 默认为 `false`，写出 `server.ini` 时始终显式输出；显式指定的 `true` 会保留。
内置预设包含 12 个模板和全部 121 个房间。
业务验证及文件操作集中在 Rust，Python 保留绑定、转换和异步封装。

离线写入持有房间锁，拒绝符号链接，并逐个替换文件。
写入失败时可能只有部分文件更新；修复错误后重新应用配置。
常规配置保存保留游戏更新的管理员、封禁和白名单；在线修改使用权限接口。

账号管理和投票统一作用于整个房间。
`Client` 提供具名方法，`Host` 在相同参数前增加房间编号：

```python
await client.ban("KU_example", seconds=600)
await client.ban("KU_example")  # 将临时封禁替换为永久封禁。
await client.unban("KU_example")
await client.set_admin("KU_example", enabled=True)
await client.whitelist("KU_example")
permissions = await client.read_permissions()
disconnected = await client.kick("KU_example")

commands = await client.list_vote_commands()
status = await client.start_vote(
    "kick", starter_userid="KU_starter", target_userid="KU_target"
)
status = await client.get_vote()
cancelled = await client.cancel_vote()
```

权限写入统一返回 `None`，重复设置成功完成。
`blocklist()`、`is_blocked()`、`is_admin()` 和 `is_whitelisted()` 查询对应状态，`unwhitelist()` 移出白名单。
永久权限修改支持游戏停服状态，`Host` 也支持整个服务停止后的文件修改。
`read_permissions()`、`blocklist()` 和 `is_blocked()` 读取永久权限名单。
原生临时封禁单独维护，`unban()` 移除永久记录，临时封禁自行到期。
临时封禁的账号可以追加永久封禁，已有永久记录的时间戳及资料会保留。
`kick()` 检查所有分片的客户端连接，覆盖选人、加载和迁移中的客户端。
客户端已离线时返回 `False`；部分分片失败或无法确认断开时抛出错误。

投票使用游戏原生 `worldvoter`，可发现已注册的投票命令，包括 Mod 命令。
发起者和命令要求的目标必须在线，原生权限、人数、玩家年龄、自定义条件及冷却限制仍然生效。
发起成功返回观察到的投票状态；最终是否通过由原生投票决定。
`get_vote()` 返回 `enabled` 和可空的 `current`，包含倒计时、选项、票数及参与者选择。
选项从 1 开始编号；未投票或无投票资格时 `selection=None`，资格由 `can_vote` 表示。
`cancel_vote()` 在没有投票时返回 `False`，取消进行中的投票会触发原生取消冷却。
关闭追踪日志不影响投票控制；使用其他投票组件的模式返回 `unsupported`。
结构化 Lua 读取只静态解析 Lua 5.1 字面量。
动态配置可以交给游戏原生加载，但不能通过结构化配置接口修改。

## 日程、维护与恢复

默认策略使用系统本地时间（`timezone: "local"`），全天开放，开启 Mod 自动维护，关闭闲置世界重建。
生成的 Quadlet 设置 `Timezone=local`，让容器继承宿主机时区。
需要覆盖日程时区时，将策略的 `timezone` 设为 `Asia/Shanghai` 等 IANA 时区名称。
可以向运行中的 Agent 更新策略：

```sh
dst-server call set_policy - --socket /srv/dst/299/.dst-agent.sock <<'JSON'
{
  "policy": {
    "timezone": "local",
    "schedule": [{"start": "18:00", "end": "00:00"}],
    "mod_auto_update": true,
    "idle_regeneration": false
  }
}
JSON
```

Agent 在启动时及运行期间每 30 秒检查开放时间，支持跨午夜窗口。
闭馆时停止全部游戏，Agent 保持在线，等待下一次开放。
非开放时段启动容器时，Agent 常驻，游戏进程保持关闭。
修改策略时间或时区会触发当前 Agent 重新检查日程。
需要暂停自动运行时，直接停止容器服务。
待处理的 Mod 维护在启动游戏前完成；启用自动维护时，每个新的开放时段都会检查 Mod。
游戏运行期间，Agent 每五分钟查询已启用的创意工坊 MOD，并与实际安装清单比较。
检测独立于房间操作，确认内容变更后进入同一维护队列。
`status.agent.mod_check` 提供最近检测时间、不可查询的条目和错误；检测失败不会关闭游戏。
`call update_mods` 排入维护请求并返回，由当前 Agent 执行；`restart: false` 会在更新后退出 Agent。
显式 Mod 维护可以在非开放时段执行，游戏启动仍遵循日程。
默认维护倒计时 60 秒，每 30 秒公告一次。
下载或校验失败最多额外尝试两次，间隔五分钟；仍失败则保持关闭，等待明确维护请求或下个开放期。

必需分片崩溃或持续失控会停止整个房间。
异常退出、强制终止和 IPC 故障会保存恢复状态，并以退出码 `1` 结束，交给宿主更换容器。
正常游戏重启和 Mod 维护复用当前 Agent。
生成的 Quadlet 使用 `Restart=on-failure`；升级方法见 [常驻 Agent 部署](docs/migration.md#resident-agent-deployment)。
可重试故障最多使用当前存档重启两次，间隔 30 秒。
只有确认的存档加载失败，才允许再尝试一次前一个完整房间快照；目标与各分片最新存档最多相差三个快照编号。
修改前固定全部目标，中断后继续同一目标。
恢复状态省略空字段，当前存档身份只保存在待执行的重试中。
回档中断后重新核对所有分片的固定目标，不单独保存分片进度表。
状态未知、目标缺失、故障原因不受支持或恢复失败时保持关闭。
连续稳定运行 30 分钟后恢复额度；额度耗尽后 Agent 仍可查询和管理，下个开放期可重新尝试，全天开放房间需显式启动。

闲置世界重建只在显式启用、开放时段、全部分片就绪且可靠确认无人时执行。
按世界天数使用 6、24、36、72、168 小时保留期。
活动观察中断或世界变化会重算保留期，正常计划闭馆保留闲置计时。

## 日志、导出与工具

默认游戏事件 profile 为 `history`，`agent --profile` 还支持 `critical` 和 `off`。
历史记录保留工作完成和物品活动，省略逐次工具挥动和普通 PvE 命中。
战斗细节覆盖玩家及归属于玩家的实体之间的交互；死亡和施法动作仍会记录。
历史记录包含容器取放后所选槽位的物品变化，省略失败尝试和自动库存变动。
虫洞、地图临时出口等本地传送在原生激活成功后产生 `dst.player.teleport_started`。
客户端认证和断开连接事件记录 `userid`（Klei ID）、`steam_id`（SteamID64 字符串）、昵称 `name`、`admin` 和 `moderator`。
`dst.server.presence` 在启动时及每隔 60 秒通过 `clients` 记录这些信息，`players` 同时记录角色 `prefab` 和幽灵状态 `is_ghost`。
昵称和权限从原生客户端列表刷新；退出事件使用最近一次观察，下一次认证前清除旧身份。
昵称最多保留 256 个 UTF-8 字节；无法取得的昵称、权限或有效 Steam ID 为 null。
`list_players` 和 `get_player` 返回相同身份字段，包括仍在大厅中的连接。
`dst.player.loaded` 记录当时的昵称、Steam ID、幽灵状态和 `age_seconds`，包括从其他分片迁入的玩家。
死亡和复活事件也记录 `age_seconds`，值为原生 age 组件累计的角色年龄秒数，无法获取时为 null。
在线快照的 `world` 记录季节、昼夜、月相、噩梦周期及雨、雪、酸雨和月雹状态；洞穴使用自身的昼夜和月相。
装备、卸下和容器所选槽位的取放记录可用的物品 `condition`：`uses_percent`、`fuel_percent`、`freshness_percent` 和 `armor_percent`。
这些值保留原生比例，通常为 0–1；缺失的组件字段省略。

已受理的 SDK 踢出、封禁、解封、管理员和白名单修改在执行完成后产生 `dst.admin.operation`，包括关闭房间时的操作。
生命周期订阅和日志记录目标 `userid`、方法、传入的封禁时长、操作 ID、开始时间、状态、可空布尔结果及错误码。
状态为 `success`、`failure` 或 `unknown`；成功表示方法完成。
权限写入结果为 null；踢人结果为 false 表示客户端已离线。
事件采用有界、尽力交付；源事件缺口、订阅溢出、输出丢弃和导出拒收分别计数。
RPC 的每类订阅共用 Tokio 广播缓存，保留最近 128 条，每条最多 64 KiB，缓存载荷最多 8 MiB。
慢客户端通过 `dropped` 获知被覆盖的记录数，超长记录也计为丢失。
每个批次最多返回 16 条（1 MiB），每个 Agent 最多接受 128 个订阅。
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
dst-server logs telemetry '{"since":1790899200,"until":1790985600,"userid":"KU_example"}'
```

使用 `userid` 可统一查询玩家作为操作者、目标、死者、投票参与者，以及在线快照和 SDK 管理操作中的记录，也支持已有历史。
它可与时间、服务和字段过滤条件组合；`userid` 和全文 `query` 不能同时使用。
每条记录保留当时观察到的昵称，旧记录不会补写新增元信息。

```python
from dst_server.logs import NetdataLogQuery, NetdataLogs

history = await NetdataLogs().query(
    NetdataLogQuery(since=1790899200, until=1790985600, userid="KU_example")
)
```

跟随模式逐条输出记录；中断日志查询时会等待读取进程退出。
`dst_server.events` 通过 `event_names()` 列出支持的事件名称，通过 `validate_event()` 校验记录。
Rust 枚举及事件数据类型定义事件契约，并保留必填空值和数值精度。
`dst_server.KleiClient` 提供有界的版本、区域、大厅和房间查询，批量查询保留每项失败。
季节、模式、服务器意图和角色 prefab 保留游戏更新或 Mod 返回的未知字符串，响应中的新增字段也会保留。
请求失败和成功但没有结果分别表达。

Rust 的 [`dst_server::steam`](crates/dst-server/src/steam.rs) 提供匿名工坊元数据查询。
使用 `Client::published_file_details(&[PublishedFileId(...)])` 调用 `GetPublishedFileDetails`。
每个 `PublishedFile` 保留 ID 和 `Result<PublishedFileDetails, ResultCode>`，分别表达单项不可用与整次请求失败。
元数据包含明确类型的 ID、内容标识、UTC 时间、可见性、URL 和标签，完整保留 64 位数值精度。
每次最多查询 100 个 ID，结果按请求顺序返回并保留重复 ID，不完整或不匹配的响应会报错。
客户端克隆共享 HTTP 连接池，响应上限为 8 MiB，总时限为 30 秒；这个只读 POST 遇到临时故障时最多重试两次。
重试在总时限内遵循 `Retry-After`，支持秒数和三种 HTTP 日期格式。
`Error::Transport` 使用 `TransportError` 表达错误类别，普通输出、调试输出和错误源均不包含 URL 或上游错误文本。
Agent 使用此客户端检测创意工坊更新，同时保留游戏原生更新通知。
`DST_SERVER_MOD_PROXY` 同时为工坊版本检查和原生 Mod 下载指定 HTTP(S) 代理。

存档导出可在游戏运行时进行，保留世界、玩家及 Mod 进度。
所需文件读取一次后保存在内存中，默认使用编码器的 LZMA2 9 级预设生成可寻址的 7z 内存文件。
压缩级别范围为 1–9，字典和匹配参数采用编码器预设。
压缩包可使用标准 7-Zip 工具解压。
每个进程同时运行一个导出压缩器，不写临时目录或交接文件。
导出包含房间及分片配置、世界生成和 Mod 配置、原生 session、`shardindex` 及食谱。
支持的 Mod 进度包括世界跳转、ReForged 成就和经验。
排除 Mod/UGC 下载内容、`server_temp`、`client_temp`、日志、backup、索引和 profile 缓存及 SDK 运行文件。
原凭据和权限名单会移除；源房间没有 token 文件也可导出。
每次导出都会在 `cluster.ini` 中生成新的随机 `cluster_key`，供所有分片共用。
基础导出保留原分片目录名和玩家路径编码方式。
可选的 `client_compatible=True`（CLI 为 `--client-compatible`）仅适用于一个主分片和一个副分片，共两个分片。
启用后，主副分片分别放到 `Cluster_<房间编号>/Master` 和 `Cluster_<房间编号>/Caves`。
同时将 `server.ini` 与 `shardindex` 中的 `encode_user_path` 设为 true，并转换未编码的玩家目录。
人物快照、元数据和 `savelocation` 随目录一起保留；已编码的玩家目录保持原名。
该选项默认关闭，不能与自定义 `room_id` 一起使用。
Mod 下载清单、启用配置、覆盖选项和存档中的 `enabled_mods` 均保留，玩家自行安装 Mod。
客户端开房无需原服务器 token；独立的在线服务端需要自行配置 token。
运行中导出不强制保存，也不保证所有文件来自同一时刻。
上传通过原生对象存储实现，失败或取消时显式终止分段上传；清理失败进入结果和日志。
Python 提供 `dst_server.archive.export_cluster` 和 `ClusterArchive.save/upload`。
SDK 和 CLI 均直接读取房间目录，无需 Agent 或 systemd。
导出默认返回内存中的文件，保存到磁盘和上传是对同一压缩包显式执行的操作。

```python
async with export_cluster(host, 299, client_compatible=True) as archive:
    contents = archive.stream.read()
```

在上述上下文中，用 `await archive.save(path)` 保存本地文件。
用 `await archive.upload(bucket=..., endpoint=...)` 上传至 R2/S3。

```sh
dst-server archive export 299 --output /tmp/room-299.7z
dst-server archive export 299 --client-compatible --output /tmp/room-299-client.7z
dst-server archive upload 299 --client-compatible --bucket saves --endpoint "$AWS_ENDPOINT_URL_S3"
```

输出文件必须尚不存在。
`import_cluster(source)` 返回保存在内存中的 `ImportedCluster`。
输入可以是目录、压缩包路径或 `ClusterArchive`，根据文件内容自动识别格式。
支持 ZIP（未压缩、Deflate）、7z（LZMA/LZMA2 和历史 Zstandard 导出包）、RAR4/RAR5 和 TAR。
TAR 支持 gzip、bzip2、XZ 和 Zstandard 压缩。
支持 RAR5 固实压缩；不支持 RAR4 固实压缩、加密压缩包和分卷压缩包。
输入直接包含一个房间，或在外面包一层目录。
原生配置与分片索引用于自动识别存档结构，支持自定义分片目录名。
INI 配置优先，索引补齐缺失的房间、世界和 Mod 配置。
主分片依次根据明确的分片配置、`Master` 名称或唯一的森林世界识别。
无法确定时，需要在 `server.ini` 中明确设置 `SHARD.is_master`。
导入保留分片目录名，将已编码或未编码的 Klei 玩家目录统一还原为 `KU_…_` 路径。
同时将 INI 和分片索引中的 `encode_user_path` 设为 `false`，保留所有快照和元数据。
导入与导出共用文件筛选、配置生成、索引脱敏和路径校验。
原凭据和权限名单会移除，每次解析都会生成新的随机共享 `cluster_key`。
通过 `imported.configuration` 读取配置，`imported.files` 枚举文件，`imported.read_file(path)` 读取内容。
显式调用 `await imported.save(destination)` 才会把同一份解析结果和密钥写入新目录。
`save()` 会拒绝已有目标。
CLI 用 `dst-server archive import SOURCE` 在内存中解析，添加 `--output DESTINATION` 才保存。
JSON 输出包含配置和所选文件路径。
导入不会注册服务或启动游戏。
压缩包最多包含 100,000 项，解压后总量不超过 2 GiB；单个原生配置文件不超过 1 MiB。
房间内部路径最多 16 层，每段最多 255 个 UTF-8 字节，总长最多 4096 字节，与保存时的限制一致。
可选的外层包裹目录不计入这些限制。
旧 `saveindex` 和使用旧路径的客户端 session 需先由游戏迁移。

```python
from dst_server.archive import import_cluster

imported = await import_cluster("client-save.zip")
configuration = imported.configuration
world = imported.read_file("Master/save/session/0123456789ABCDEF/0000000001")
await imported.save("/tmp/restored")
```

要更新已有房间，先停止 Agent 和所有游戏进程，再调用 `await imported.apply(path, level="runtime")`。
它返回应用后的配置，支持三个累计层级：

| 层级 | 替换内容 |
| --- | --- |
| `data` | 世界和玩家存档、快照、索引及已保存的游戏进度。 |
| `runtime`（默认） | `data`，加上 Mod 配置和下载列表、`modoverrides.lua`、`worldgenoverride.lua`、`leveldataoverride.lua`。 |
| `all` | `runtime`，加上 `cluster.ini` 和各分片的 `server.ini`，包括端口。 |

未选中的配置文件、已有 token 和权限名单保留原始字节。
所选存档目录会整体替换；`runtime` 和 `all` 也会删除导入内容中已不存在的 override 文件。
源和目标的分片数量必须相同，每个源分片的会话必须包含非空的最新原生世界快照。
快照文件名使用十位数字，编号为正的 32 位无符号整数；非法编号和空的最新快照会被拒绝。
分片依次按目录名、源文件明确提供的 ID、剩余唯一的主副分片角色匹配。
自动补出的 ID 不参与消除歧义；无法确定时会在写入前报错。
玩家路径和分片索引会适配目标最终使用的 `encode_user_path` 配置。
Agent 持有房间锁时无法应用导入。
完整结果先在目标旁准备，再一次交换目录，因此需要同一文件系统中可写的父目录。
应在目标房间之外的工作目录执行导入；目标包含调用方工作目录时会被拒绝。
暂存目录从创建时就限制访问权限，已丢弃的只读目录副本会在清理时恢复写权限。
保留目录的访问 ACL 和默认 ACL 会一并保留，无法保留时会在发布前报错。
发布后的错误会明确提示 `import applied`，并指出尚未清理的旧目录。
遗留的 Agent socket 在确认无人监听后会被移除。
旧的 Agent 运行目标、恢复记录和策略执行状态会清空，策略配置保留。
使用 `all` 改变端口时，还需要检查部署的端口映射。

```sh
dst-server archive import client-save.zip --into /srv/dst/rooms/007
dst-server archive import client-save.zip --into /srv/dst/rooms/007 --level data
```

`--into` 和 `--output` 互斥，`--level` 需要与 `--into` 一起使用。
CLI 目标路径必须是有效的 UTF-8，写入前会完成检查。
应用后的 JSON 输出包含目标路径、层级和实际配置。

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
cargo nextest run --locked -p dst-server --all-targets
uv run ruff check python tests/rust tools
uv run ty check python
uv run --locked --only-group dev pytest tests/lua
uv build --out-dir dist --no-sources
```

CI 从源码包构建 wheel，在隔离环境安装并运行绑定和进程测试。
Lua 契约使用 Lua 5.1 和 LuaJIT 验证，可通过 pytest 单独选择场景。
工具安装、定向执行命令和测试取舍见 [测试指南](docs/testing.md)。
游戏版本 `756039` 在五个互联分片中出现过尚未解决的 [原生停服崩溃](docs/migration.md#known-native-shutdown-failure)。
暴食和熔炉主动禁用保存；接受保存请求不代表已经生成存档。
真实游戏探针只用于临时目录和测试容器。
构建清单记录 SDK、Lua、协议、游戏版本和产物摘要。
验证方法和部署步骤见 [迁移指南](docs/migration.md)。
