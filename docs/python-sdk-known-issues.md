# Python SDK 已确认问题

本文记录 Python SDK、注入式 Lua driver 和 `-cloudserver` IPC 链路中已经确认的问题及当前状态。

已解决项只保留稳定契约，未解决项保留继续实现所需的边界。

## 状态矩阵

| 编号 | 历史严重度 | 当前状态 | 当前结论 |
| --- | --- | --- | --- |
| SDK-001 | P0 | 已解决 | 首次 SDK 安装失败会降级 SDK，不再终止已经 ready 的游戏进程 |
| SDK-002 | P0 | 已解决 | 超长行可恢复，ULID 帧身份隔离每次命令 |
| SDK-003 | P0 | 部分解决 | 已停止错误补写 Session，但事件仍缺少真实 Session 和跨 reload 身份 |
| SDK-004 | P1 | 已解决 | Reload API 会等待严格更高 generation 及其 driver ready |
| SDK-005 | P1 | 部分解决 | 安装失败可安全降级，但物理副作用和同代恢复仍未闭环 |
| SDK-006 | P2 | 已解决 | Health 不再伪装成当前在线人数 |
| SDK-007 | P1 | 已解决 | FD5 观察队列已有界，坏行和 EOF 可恢复 |
| SDK-008 | P1 | 已解决 | 有限管理操作已有总 deadline，FD3 命令与 FD4 frame 均已有资源上限 |

P0 表示会终止受管游戏进程、破坏 RPC 完整性或生成错误 Session 数据。

P1 表示会造成无限等待、重复副作用、陈旧状态、无界资源消耗或控制通道失效。

P2 表示不会直接破坏游戏进程，但会让健康状态或监控数据表达错误。

## SDK-001：SDK 安装失败被做成游戏启动硬依赖

状态：已解决。

默认 profile 为 `critical`，安装 management core 和关键游戏事件 Hook。

Telemetry 的模块加载和 Hook 安装位于隔离边界内。

失败返回 `telemetry_status="failed"`，不会阻止 shard ready 或 management RPC。

首次 Core 模块、非法 options、非权威 world、RPC envelope 或 FD4 结果错误会记录 `driver_error`。

类型化 SDK 明确报告 unavailable，游戏进程继续运行。

保持帧同步的 Lua 或 envelope 错误仍可使用原始 console；FD4 EOF 等不可恢复错误会同时关闭 console 控制通道。

进程退出、FD5 lifecycle EOF、取消和总启动 timeout 仍会完成进程与 pipe 清理后向调用方报错。

安装失败前产生的物理 Hook 保持 inactive，剩余限制由 [SDK-005](#sdk-005lua-安装有副作用但不具备可安全重试性) 跟踪。

## SDK-002：FD4 超长行会破坏后续 RPC 帧边界

状态：已解决。

结构化结果的完整单行内容上限为 65,536 bytes，超限时 Lua 返回短小的 failure envelope。

Python 会完整排空超长物理行和当前 frame，再允许下一次请求进入 FD3。

每次实际写入 FD3 前都会生成新的标准 ULID，并使用匹配的 `START` 和 `END` 标记验证响应边界。

ULID 是项目对随机相关模块和内部相关性 ID 的统一要求，不得替换为无格式随机字符串。

命令写入前的真实 `DST_LuaBusy` 可以使用新的 ULID 安全重试，命令写入后的不确定结果不会自动重放。

ULID 用于协议相关性，不隔离能够读取并伪造当前 token 的同信任域 raw Lua。

整个 frame 还限制总 payload bytes 和总 payload lines，具体契约见 [SDK-008](#sdk-008管理链路缺少总-deadline-和资源上限)。

## SDK-003：游戏事件可能被归入错误 Session

状态：部分解决。

Python exporter 不再读取可变的 `server.session_id`，因此不会把旧事件错误标为当前 Session。

当前事件 envelope 只有 `nonce` 和 generation 内递增的 `seq`，没有 producer 写入的 Session 或 Lua generation。

Lua reload 后 `seq` 会重新开始。

Python 生成 canonical ULID 作为 `EventStream.nonce`。

Lua producer 和 Python schema 都强制该格式。

它在整个 Python `Server` 生命周期内保持不变。

因此 `(nonce, seq)` 不能作为跨 reload 唯一身份。

需要 Session 归属时，Session 必须由 Lua producer 在事件发生时写入。

需要跨 reload 去重时，还必须增加每个 Lua runtime 或 install 唯一的 ULID generation ID。

FD5 到达顺序、Python 入队时间和 exporter 消费时间都不能替代 producer identity。

## SDK-004：Reload 后没有可靠的 Driver-Ready 屏障

状态：已解决。

每个合法 FD5 Session 都会增加 generation，并立即使上一代 committed health 失效。

普通类型化请求在持有 Console lock 且排空 pending frame 后验证 generation token，再写入 FD3。

普通类型化请求在写入前 token 失效时会等待新 generation ready 后安全重试。

它在写入后变代时会抛出 `IndeterminateCommandError`，并且不会重放。

Driver 是 committed health 的唯一所有者，旧 generation 的 install 结果不能覆盖新状态。

`reset()`、`regenerate()`、`regenerate_shard()` 和 `rollback()` 会记录命令接受前的 generation。

这些方法只有在观察到严格更高的 FD5 generation 且该代 driver ready 后才返回。

触发 RPC、generation 等待和 reinstall 共用调用方提供的同一个完成期限，超时不会重放已接受的命令。

Raw `Server.execute()` 是不经过 generation 屏障的显式逃生口。

## SDK-005：Lua 安装有副作用但不具备可安全重试性

状态：部分解决。

Lua 会在产生副作用前验证全部 options，并且只在所有 Telemetry 安装阶段成功后切换为 active。

安装失败会返回 failed health，残留 wrapper 和 listener 保持 inactive，同一 generation 不会自动 retry。

`Shard_UpdateWorldState` wrapper、world listener、watcher 和 player listener 仍没有完整卸载事务。

Player attachment 中途失败还可能留下不完整 listener 或 marker，并在 active health 下形成事件缺口。

当前没有同 generation 恢复需求时，保持 fail-once 是最小安全契约。

如果增加同代恢复，每个阶段必须先具备 cleanup 或可证明幂等的完成 marker，并确保一个游戏事实最多产生一条事件。

## SDK-006：`driver.health.players` 不是当前在线人数

状态：已解决。

Driver health 只表达 protocol、Telemetry 状态、安装错误和事件计数，不再暴露 `players`。

即时在线人数由 `world.room()` 或 `players.list()` 的权威游戏查询提供。

Recorder gauge 是 best-effort Telemetry，不是权威在线人数契约。

## SDK-007：FD5 生命周期观察队列可能无限增长

状态：已解决。

Lifecycle 先更新 ready、Session、save 和 stopping 控制状态，再发布到最多 64 条的 best-effort 观察队列。

队列满时丢弃最旧记录，不会阻塞 FD5 pump，也不会影响独立保存的控制状态。

超长或字段非法的物理行只降级当前记录，下一条合法控制消息仍可生效。

真实 EOF 会唤醒 ready 和 save 等待方，观察队列排空后稳定返回 `None`。

## SDK-008：管理链路缺少总 Deadline 和资源上限

状态：已解决。

`Server.start()` 使用默认 300 秒的单一 timeout 覆盖进程创建、FD5 ready 和首次 driver install。

进程退出、Lifecycle 失败、总 timeout 或取消会清理进程、pipe 和后台 task；普通首次 driver 错误只降级 SDK。

Mod updater 使用独立进程组，并在异常或取消时执行 TERM、有限 grace、KILL 和 reap。

Raw、类型化、save 和 reload 管理操作默认使用 30 秒总期限。

Raw `Server.execute(completion_timeout=T)` 使用单一总预算。

该预算覆盖 driver/lifecycle ready、Console lock、pending drain、Lua Busy retry、writer drain 和完整 frame。

`save(completion_timeout=T)` 的 RPC 与 FD5 save confirmation 共用同一个绝对 deadline。

每次实际写入具有独立 attempt，`LuaBusy` 重试前的无关保存不能充当最终确认。

`Cluster.save()` 只向 master 发出一次请求，并分别等待每个分片的 FD5 确认。

四个 reload API 的触发 RPC、严格更高 generation 等待和 driver reinstall 也共用同一个绝对 deadline。

最终写入 FD3 的 encoded line 包含结尾 LF，并且总长不得超过 65,536 bytes。

一个 FD4 frame 的 payload 内容不得超过 65,536 bytes 或 1,024 行，行尾 CRLF 不计入 payload bytes。

frame 超限时 Python 会继续排空匹配的 `END` 与原生 Done，再抛出 `ResponseTooLargeError`，因此下一条命令仍可安全执行。

命令在写入前超时或收到 `DST_LuaBusy` 后超时不会破坏 Console。

命令已被接受且总期限耗尽时，Python 会取消结果 reader 并把 Console 标为不可用，因为此时不能可靠恢复 frame 边界。

保存命令写入后若调用方主动取消，后续保存仍会等待迟到的明确结果解除 barrier；明确的写入前失败不会建立 barrier。

Stop 和 kill 的取消会先完成 kill、reap 和 `finish()` 再传播；`give(count)` 同时在 Python 与 Lua 限制为 64。

长期观察 API 刻意不内置有限期限。

这包括 `Server.wait()`、`read_event()`、`read_game_event()` 和 cluster `serve()`，调用方可按需要使用 `asyncio.timeout()`。

## 收敛顺序

1. 只有出现真实同 generation 恢复需求时，才扩展 SDK-005 的 rollback 或幂等 retry。
2. 只有出现 Session 归属或跨 reload 去重需求时，才扩展 SDK-003 的 producer identity。

SDK-001、SDK-002、SDK-004、SDK-006、SDK-007 和 SDK-008 只需保留回归测试。
