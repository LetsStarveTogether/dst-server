# 运行时与进程通信

本文说明维护集群控制、分片进程和游戏管理接口时需要遵守的边界。
部署入口见 [README](../README.md)，目录与配置见[配置指南](configuration.md)。
Mod 下载策略见 [Mod 管理](mods.md)，事件模型与持久交付见[遥测指南](telemetry.md)。

## 进程所有权

一个 Pod 内，主分片容器同时运行 `ClusterController` 和主分片 `ShardAgent`。
每个次分片容器运行自己的 `ShardAgent`，向主控制器注册。
控制器维护预期分片名单、期望状态、共享准备和集群故障处理。
每个 Agent 独占一个分片的游戏进程、日志、生命周期观察、游戏事件消费和遥测 pipeline。

| 组件 | 责任与代码入口 |
| --- | --- |
| daemon | 提供管理服务、注册连接和 systemd 通知，见 [daemon.py](../src/dst_server/cluster/daemon.py)。 |
| Controller | 协调集群操作和配置 revision，见 [controller.py](../src/dst_server/cluster/controller.py)。 |
| Agent | 激活分片资源并消费观察流，见 [agent.py](../src/dst_server/cluster/agent.py)。 |
| Supervisor | 监督、停止和重试游戏进程，见 [supervisor.py](../src/dst_server/cluster/supervisor.py)。 |
| Server | 管理一次 DST 子进程尝试及其通信通道，见 [server.py](../src/dst_server/runtime/server.py)。 |

`Server` 是单次使用对象，Supervisor 每次重试都会创建新实例。
游戏进程重启、Agent daemon 重启和整服重启是不同操作，不能根据容器存活推断游戏已经就绪。

## 两层 IPC

管理层使用 Cap'n Proto RPC。
公开入口默认为 `/cluster/.dst-server.sock`，`ClusterClient` 通过它访问集群及指定分片。
主 Agent 在进程内注册，次 Agent 通过 Pod 内的抽象 Unix socket `dst-server-registry` 注册。
连接建立时校验 schema fingerprint，客户端与 daemon 应使用一致版本。
接口定义见 [rpc.capnp](../src/dst_server/rpc/schema/rpc.capnp)，传输见 [transport.py](../src/dst_server/rpc/transport.py)。

游戏层使用 DST 的 `-cloudserver`：FD 3 输入 Lua，FD 4 返回命令文本，FD 5 报告原生生命周期。
启动 wrapper 将匿名 pipe 映射到这三个 FD；普通日志通过 stdout 接收，stderr 合并到 stdout。
每个分片的 Console 串行发送命令并消费完整结果；原始 Lua 需要显式 `print` 才能返回文本。
实现见 [fds.py](../src/dst_server/runtime/fds.py) 和 [console.py](../src/dst_server/runtime/console.py)。

FD 5 承载 Ready、Session、Saved、Stopping 等状态，游戏领域事件通过 stdout 接收。
这些输出必须持续消费，否则 pipe 或队列的背压可能阻塞游戏。
直接使用 `Server` 的调用方负责消费观察流；标准部署由 Agent 完成。

公开 socket 权限为 `0600`，其目录必须由当前用户控制且不可被组或其他用户写入。
内部抽象 socket 依赖 Pod 网络命名空间隔离，不提供文件权限边界。
RPC 包含任意 Lua 执行能力，能连接这些接口的进程应处于同一信任域。

## 表情与轮盘动作编码

`dst_server.game` 公开 `Emoji`、`Emote` 和 `EmoteType`，定义见 [enums.py](../src/dst_server/game/enums.py)。
映射对应仓库固定的游戏 build `747465`，SDK 运行时无需读取游戏 Lua 文件。

| 枚举 | 值与附加字段 |
| --- | --- |
| `Emoji` | 50 个原生表情字符；`chat_token` 为 `:name:`，`item_type` 为账号物品类型标识 |
| `Emote` | 32 个原生动作命令名；提供 `slash_command`、`category`、`item_type` 和原始 `aliases` 元组 |
| `EmoteType` | 轮盘分类：`EMOTION=0`、`ACTION=1`、`UNLOCKABLE=2` |

```python
from dst_server.game import Emoji, Emote, EmoteType

assert Emoji.BEEFALO == "\U000f0001"
assert Emoji.BEEFALO.chat_token == ":beefalo:"
assert Emoji.ALCHEMY.item_type == "emoji_alchemyengine"
assert Emoji("\U000f0001") is Emoji.BEEFALO
assert Emoji.BEEFALO.encode("utf-8") == b"\xf3\xb0\x80\x81"

assert Emote.WAVE == "wave"
assert Emote.WAVE.slash_command == "/wave"
assert Emote.WAVE.category is EmoteType.EMOTION
assert Emote.WAVE.aliases == ("waves", "hi", "bye", "goodbye")
assert Emote.CHEER.item_type == "emote_jumpcheer"
assert Emote("wave") is Emote.WAVE
```

`Emoji` 与 `Emote` 都是 `StrEnum`，可直接用作字符串并按值序列化为 JSON；码点可用 `ord(Emoji.BEEFALO)` 获取。
标准枚举构造器按值反查，未知值抛出 `ValueError`；聊天标记、斜杠写法和别名不是构造器接受的值。
成员名采用原生输入名或命令名的大写，物品类型使用源表的实际映射，不能统一按名称拼接。

表情字符来自 [emoji_items.lua](../dst-scripts/scripts/emoji_items.lua)，占用 U+F0000–U+F0031 私用区。
聊天补全写入 `:name:` 文本，原生注册将输入名、字符和物品类型关联；显示仍依赖游戏字体。
动作定义来自 [emotes.lua](../dst-scripts/scripts/emotes.lua) 与 [emote_items.lua](../dst-scripts/scripts/emote_items.lua)。
轮盘通过 `SendSlashCmdToServer` 发送不带 `/` 的命令名；`EmoteType` 只表示轮盘分组，不是网络动作编号。
普通动作的 `item_type` 为 `None`，解锁动作保留对应账号物品类型；该字段不是库存中单件物品的实例 ID。
枚举不判断玩家当前的所有权、装备或姿态是否允许执行。
平台语言别名与 Mod 动态注册以运行中的游戏为准，源码契约测试会检查原版映射随游戏版本发生的变化。

## 启动、重启与接管

控制器收到全部预期 Agent 后，自动协调默认的 `running` 期望状态。
首次启动先校验配置和拓扑，准备共享目录并更新 Mod，再激活各 Agent，并发启动所需分片。
共享准备由控制器统一调用 [service.py](../src/dst_server/cluster/service.py)，只有全部游戏进程停稳时才执行实际更新。
失败状态仍带有 PID 的分片不算已停止；更新失败也不会继续启动游戏。

整服 `stop()` 或 `kill()` 会使准备缓存失效，随后 `start()` 重新检查更新。
整服 `restart()` 先停止所有游戏，再执行共享准备和启动。
手动 `update_mods()` 要求游戏全部停止，其成功结果可由紧接着的 `start()` 复用。
共享准备仍有效时，重复 `start()` 和单分片重启复用现有 Mod。
Supervisor 崩溃重试只恢复游戏进程，不触发共享更新。

控制器接管已运行的 Agent 时，校验配置后跳过共享更新，保留现有游戏。
这种接管不会声明已完成准备；状态中的 `prepared_revision` 可以为空，等整服停止后再更新。
已有准备 revision 时，运行中配置漂移会阻止新的共享准备，不会边运行边覆盖 Mod。

`Server.start()` 默认用 300 秒覆盖进程创建、原生就绪确认和首次 Lua driver 安装。
Driver 安装失败可使类型化管理接口不可用，但只要进程与生命周期通道仍正常，游戏可以继续运行。
首次安装和后续重载共用一个安装任务；同一代失败后不重复尝试，新的 Session 可以重新安装并恢复接口。

## 保存与世界重载

`stop()` 和 `restart()` 不隐式保存，daemon 正常退出也不构成保存确认。
需要保存进度时，应先等待 `cluster.save()` 成功，再停止或重启。
集群保存只向主分片发起一次请求，然后等待所有分片在各自标记之后报告对应的 Saved 确认。
命令已提交但确认中断时，结果可能不确定；不能把 Lua 命令返回或日志中的退出提示当成保存成功。

FD 5 的 Session 推进宿主记录的 generation，并使上一代 driver 健康状态失效。
同一 Lua VM 的 Hook 只安装一次；迟到的 Session 只更新相同配置下的 generation，不重复安装 Hook 或清零事件序号。
类型化请求等待当前 generation 就绪，重置、回档和重新生成还需等待后续 generation 安装完成。
写入命令前发现 generation 变化可以等待后重试，写入后发现变化则报告不确定结果，不自动重放。
原始 `Server.execute()` 不等待 driver 就绪，调用方需要自行处理世界重载时序。
安装屏障见 [driver.py](../src/dst_server/runtime/driver.py)，保存确认见 [lifecycle.py](../src/dst_server/runtime/lifecycle.py)。

## 故障处理

Supervisor 最多连续尝试五次，失败间隔一秒，稳定运行十分钟后清零连续失败计数。
某个分片耗尽重试预算时，控制器停止已注册分片的游戏；Agent daemon 和公开 RPC 保留用于排障或显式启动。
次分片注册连接断开时会杀死自己的游戏，控制器则停止仍连接的其他分片，避免部分集群继续运行。
这些停止路径同样没有隐式保存保证。

| 现象 | 应检查的边界 |
| --- | --- |
| 游戏运行但类型化请求失败 | 查看 `driver_error` 与 driver health；FD 4 的 EOF 或不完整响应会使 Console 不可用。 |
| 遥测安装失败但游戏运行 | 检查遥测 health；Hook 安装失败本身不触发游戏重启。 |
| Agent daemon 退出 | 检查关键观察流异常和遥测持久化失败；进程管理器负责重启容器。 |
| 保存、回档等操作返回不确定结果 | 先查询状态或确认事件，再决定下一步，避免重复执行有副作用的操作。 |

配置 `NOTIFY_SOCKET` 时，daemon 先发送 `READY=1`，随后每 60 秒发送 `WATCHDOG=1`。
READY 表示管理进程开始运行，发生在等待完整注册和游戏就绪之前。
生成的 Quadlet 使用 `WatchdogSec=300`，超时后由 systemd 清理服务进程并重启容器。
watchdog 只检查 daemon event loop，不能证明游戏线程正常推进或遥测已成功交付。
部署参数见 [quadlet.py](../src/dst_server/cluster/quadlet.py)，交付故障处理见[遥测指南](telemetry.md)。
