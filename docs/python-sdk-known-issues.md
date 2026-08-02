# Python SDK 接入已确认问题

本文单独记录 Python SDK、注入式 Lua driver 和 `-cloudserver` IPC 链路中已经确认的问题。

本文描述的是当前代码的实际行为，不把尚未实现的设计目标当作现状。

问题按以下优先级分类：

- P0：会杀死受管游戏进程、破坏 RPC 协议完整性或产生错误的 session 数据，阻塞生产接入。
- P1：会造成无限等待、重复 Hook、陈旧状态或无界内存增长，应在生产接入前修复。
- P2：不会直接破坏游戏进程，但会让健康状态或监控数据表达错误。

| 编号 | 优先级 | 问题 |
| --- | --- | --- |
| SDK-001 | P0 | 可选 telemetry 被做成游戏启动硬依赖 |
| SDK-002 | P0 | FD4 超长行会破坏后续 RPC 帧边界 |
| SDK-003 | P0 | 游戏事件可能被归入错误 session |
| SDK-004 | P1 | reload 后没有可靠的 driver-ready 屏障 |
| SDK-005 | P1 | Lua 安装有副作用但不具备可安全重试性 |
| SDK-006 | P2 | `driver.health.players` 不是当前在线人数 |
| SDK-007 | P1 | FD5 生命周期观察队列可能无限增长 |
| SDK-008 | P1 | 管理链路缺少总 deadline 和资源上限 |

## SDK-001：可选 Telemetry 被做成游戏启动硬依赖

状态：已解决。

### SDK-001 修复结果

[`TelemetrySettings`](../src/dst_server/telemetry/config.py) 的默认 profile 已改为 `off`。

普通 [`ServerConfig`](../src/dst_server/runtime/config.py) 因此默认只安装 management RPC。

[`cluster.service.prepare()` 和 `run()`](../src/dst_server/cluster/service.py)
接受 `telemetry: TelemetrySettings | None = None`。

其中 `None` 解析为默认 `off`，同一份冻结配置会传给全部 shard。

OTLP 环境变量只配置传输，不会隐式启用 Lua 游戏事件。

[`GameClient.install()`](../src/dst_server/game/client.py) 仍只发送一次同步 `driver.install(options)` RPC。

Lua 顶层只加载 core registry、commands 和 queries，可选 telemetry 模块在受保护的安装边界内按需加载。

首次 install 会先把 nonce、profile 和全部 action ID 验证到局部变量。

首次 install 的 options 非法时返回 RPC failure，并且不会提交配置。

首次合法调用一次性提交配置并把 core management driver 标记为 installed。

Profile 为 `off` 时不会加载 telemetry 模块或安装 Hook，并返回 `disabled` health。

Profile 为 `critical` 或 `history` 时，只有所需可选能力和 Hook 全部成功后才把动态开关切换为 `active`。

可选模块、能力或 Hook 失败时，Lua 返回成功 envelope 和 `failed` health，而不是抛出 core install error。

Python 会保存该 health，记录一次包含 cluster、shard、profile 和原因的 warning，并允许 `Server.start()` 成功。

每个 shard 独立降级，因此一个 shard 的 `failed` 不会取消其他 shard 的启动。

[`configure_otel()`](../src/dst_server/cluster/service.py) 只捕获普通 `Exception`，初始化失败时记录异常并回退到本地结构化事件日志。

底层显式 [`telemetry.otel.configure()`](../src/dst_server/telemetry/otel.py) 仍向库调用者抛错，`BaseException` 也不会被降级逻辑吞掉。

### SDK-001 Driver Health 契约

`DriverHealth` 固定为以下五个扁平字段：

```python
protocol: Literal[1]
telemetry_status: Literal["disabled", "active", "failed"]
telemetry_error: str | None
events_emitted: NonNegativeInt
errors: NonNegativeInt
```

| 状态 | error | 含义 |
| --- | --- | --- |
| `disabled` | `None` | 配置为 `off`，未加载 telemetry 模块，也未安装 Hook |
| `active` | `None` | 配置的 `critical` 或 `history` profile 所需 Hook 全部安装成功 |
| `failed` | 非空字符串 | 当前 generation 的 telemetry 已逻辑全量关闭 |

请求的 profile 来自当前 `TelemetrySettings` 配置，不在 health 中重复回显。

Lua 使用 `json.null` 明确编码没有安装错误的状态。

`telemetry_error` 保留第一次安装错误，清理控制字符并截断到 1024 bytes。

`errors` 只统计运行期 callback 或 emit 错误，不包含安装错误。

Core management driver 是否可用由 install RPC 是否成功表达，不再由 health 字段重复表达。

`protocol=1` 继续表示既有 Lua 与游戏事件 RPC major，不承担独立 health schema 版本职责。

这次 health 字段变化是明确的 breaking change，不提供旧字段兼容层。

### SDK-001 保留的致命与降级边界

Core module、RPC registry、`json.decode`、`json.encode_compliant`、非法 install options 和非权威 world 仍属于致命错误。

初次 core 安装失败、非法 envelope 或传输失败仍会使启动失败并清理游戏进程和后台任务。

`GetTick`、`GetTimeReal`、world event API、`BufferedAction.Do` 和 `Shard_UpdateWorldState` 属于 fail-open telemetry 能力。

Telemetry 安装失败前产生的 wrapper 或 listener 可以物理残留，但它们会保持 inert，不再读取时钟、构造 payload、编码或输出事件。

同一 generation 的后续 install 直接返回缓存 health，不再验证 options、改变配置或重试 telemetry。

该逻辑解决了可选 telemetry 的启动硬依赖，但没有把 SDK-005 的物理回滚或安全重试伪装成已经完成。

SDK-006 已通过移除 `DriverHealth.players` 并停止用 health 覆盖 Recorder 人数而解决。

SDK-004 的 reload-ready 屏障以及 SDK-005 的物理回滚与安全重试仍未解决。

## SDK-002：FD4 超长行会破坏后续 RPC 帧边界

状态：已确认并可稳定复现。

### SDK-002 当前实现

FD4 reader 由 [`open_reader()`](../src/dst_server/runtime/fds.py#L48-L60) 创建。

它使用没有显式 `limit` 的 `asyncio.StreamReader`，因此单行限制采用 Python 默认的 65,536 bytes。

该限制比较换行符之前的整条物理行，并按 reader 收到的 bytes 计算。

`DST_SERVER_RESULT|` 前缀本身占 18 bytes，所以 JSON 并不能使用完整的 65,536 bytes。

[`SUBPROCESS_STREAM_LIMIT`](../src/dst_server/runtime/server.py#L27-L29) 虽然设置为 1 MiB，
但它只传给游戏 stdout reader，见
[`create_subprocess_exec()`](../src/dst_server/runtime/server.py#L136-L146)。

FD4 和 FD5 reader 都没有使用这个值。

[`Console.read_result()`](../src/dst_server/runtime/console.py#L53-L66) 使用 `readline()` 逐行读取，
直到独立一行 `DST_RemoteCommandDone`。

结构化 RPC envelope 由 [`lua_request()`](../src/dst_server/game/rpc.py#L57-L65) 一次性 JSON 编码并打印。

该 envelope 没有 Lua 侧大小限制。

[`world.execute()`](../src/dst_server/game/world.py#L88-L96) 又允许可信管理员返回任意 JSON 值，
所以构造超过 64 KiB 的合法结果并不困难。

### SDK-002 失败时序

1. Python 通过 FD3 发送一个会打印超长单行结果的 Lua 请求。
2. DST 先向 FD4 写入 `DST_SERVER_RESULT|<json>`。
3. `StreamReader.readline()` 因单行超过 limit 而失败。
4. `Console.execute_once()` 看到 result task 已结束并清除 `pending_result`。
5. 当前请求的 `DST_RemoteCommandDone` 仍留在 FD4 缓冲区。
6. 下一次请求被正常写入 FD3。
7. 新 reader 先读到上一次请求遗留的 `DST_RemoteCommandDone`，于是返回空结果。
8. 新请求的真实结果留在流中，并可能被再下一次请求读取。

该问题不只会产生一次显式异常。

它会破坏后续请求与结果的一一对应关系，并可能让兼容 schema 的旧结果被静默当成新结果。

### SDK-002 影响

一个过大的查询、Mod 输出或管理员脚本结果可以永久污染该进程后续的 RPC 流。

如果两个相邻请求使用相同或兼容的响应 schema，错误结果可能通过 Pydantic 校验，形成静默数据错误。

继续提高 reader limit 只能推迟触发点，不能保证协议恢复。

### SDK-002 根因

协议使用 `DST_RemoteCommandDone` 作为唯一帧边界，但异常路径没有保证继续消费到该边界。

Lua producer、Python FD reader 和 RPC envelope 之间也没有共享的最大响应尺寸。

### SDK-002 修复约束

必须为结构化 RPC 定义一个明确且有限的 envelope 上限。

Lua 应在打印前检查完整 prefix 和 JSON，并给 Python reader 留出明确的 framing 余量。

超限时，Lua 应返回一个短小、合法的 failure envelope。

Python 仍必须防御游戏日志、Mod 或旧 Lua producer 输出的超长行。

Python 检测到超限后必须排空当前帧直到 `DST_RemoteCommandDone`，然后再向当前调用者报告稳定的
`ResultTooLarge` 类错误。

如果无法可靠排空，必须把当前 Console 或游戏进程标记为不可继续使用，而不是发送下一条命令。

正常业务没有证据需要巨型 response，因此不应引入分片和重组协议。

### SDK-002 验收标准

- 小于上限的结构化结果正常返回。
- 位于边界的结果不会因为 18-byte prefix 或游戏额外输出而越界。
- 超过上限的结果向当前调用者返回明确错误。
- 超限后的下一次普通 RPC 仍然取得自己的结果。
- 超限帧缺少 `DST_RemoteCommandDone` 时，后续 RPC 被拒绝，直到进程被重启。
- 测试覆盖超限前一字节、边界值、超限后一字节和超长非结构化输出。
- 测试同时覆盖整行一次到达和一行跨多个 pipe buffer 到达。
- 超长普通 `print` 后仍有结构化 envelope 时，当前请求失败但下一请求不会读取该 envelope。

## SDK-003：游戏事件可能被归入错误 Session

状态：已确认。

### SDK-003 当前实现

Lua 在 [`telemetry.emit()`](../src/dst_server/lua/dst_server/telemetry.lua#L5-L27) 中形成事件 envelope。

Envelope 包含 nonce、sequence、tick、进程内单调时间和 cycle，但不包含 session ID 或 Lua generation。

Python 对应的 [`EventRecord`](../src/dst_server/events/base.py#L33-L41) 同样没有这些字段。

[`EventStream.accept()`](../src/dst_server/telemetry/stream.py#L43-L87) 在接收事件时只固化记录内容和
`observed_timestamp_ns`。

[`ObservedGameEvent`](../src/dst_server/events/record.py#L84-L87) 也没有 session 快照。

只有当异步 exporter 从队列取出事件时，
[`export_events()`](../src/dst_server/cluster/service.py#L177-L187)
才读取当前可变的 `server.session_id`。

FD5 lifecycle、FD4 result 和 stdout 是互相独立的 pipe。

这些 pipe 与 Python telemetry 队列之间不存在跨流顺序保证。

### SDK-003 失败时序

1. 旧 session 中的 Lua callback 形成事件。
2. Python 从 stdout 或 FD4 接收事件并把它放入 `EventStream.queue`。
3. FD5 随后报告新的 `DST_SessionId`。
4. `Lifecycle` 立即把 `server.session_id` 更新为新值。
5. Exporter 之后才消费旧事件。
6. Exporter 读取新 `server.session_id`，把旧事件标记为新 session。

反向的跨流竞态同样可能发生。

日志模式只序列化原始 record，见
[`log_events()`](../src/dst_server/cluster/service.py#L168-L174)，
所以它不是错标，而是完全没有 session 事实。

### SDK-003 Reload 后的额外歧义

`EventStream.nonce` 在 `Server` 生命周期内保持不变，见
[`EventStream.__init__()`](../src/dst_server/telemetry/stream.py#L24-L30)。

Lua module reload 后，
[`state.sequence`](../src/dst_server/lua/dst_server/state.lua#L5-L10)
会重新从 0 开始。

Rollback 或 reset 还可能保留相同的 DST session ID。

因此仅使用 `(nonce, session_id, seq)` 也不足以唯一标识一次 Lua runtime。

### SDK-003 影响

玩家行为、死亡、世界状态和 shard 事件可能写入错误的 session。

按 session 统计、审计和回放的数据会出现跨局污染。

该错误发生在异步竞态窗口内，通常不会产生异常，因此比显式丢弃更难发现。

### SDK-003 根因

Session 是事件产生时的不可变事实，却在事件消费时从全局可变状态补写。

当前 envelope 也缺少区分 Lua reload 的 generation。

### SDK-003 修复约束

Lua 应在形成 envelope 时读取真实 session ID。

仓库已有
[`get_runtime()`](../src/dst_server/lua/dst_server/world_queries.lua#L4-L22)
读取 `TheWorld.meta.session_identifier` 的实现，应复用同一事实来源。

每次 driver install 还应携带一个非秘密的 stream generation，并写入每条事件。

Nonce 继续只负责来源校验，不应被重新定义成 session 或 generation。

Exporter 只能使用事件自身携带的 session，不能再从 `Server` 的当前状态补写。

Cluster 和 shard 名来自不可变 `ServerConfig`，仍可以由 Python 补充。

### SDK-003 验收标准

- 旧 session 事件进入队列后再更新 `server.session_id`，导出结果仍保留旧 session。
- 新 session 事件不会被标成旧 session。
- 同一 DST session 内发生 Lua reload 时，stream generation 会变化。
- 每个 generation 的 sequence 可以从 1 重新开始，而事件身份仍不冲突。
- stdout 与 FD4 以不同顺序到达时，session 归属保持一致。
- 日志输出和 OpenTelemetry 导出使用同一份事件 session 事实。

## SDK-004：Reload 后没有可靠的 Driver-Ready 屏障

状态：已确认。

### SDK-004 当前实现

`Lifecycle` 在第一次 Ready 或 Session 消息后把 `ready` 永久设置为 `True`，见
[`Lifecycle.handle()`](../src/dst_server/runtime/lifecycle.py#L42-L60)。

Reset、rollback、regenerate 或新的 Session 消息都不会清除该状态。

每个 `SessionEvent` 会增加 `session_generation`，然后调用
[`Driver.session_started()`](../src/dst_server/runtime/driver.py#L37-L39)。

`Driver` 会异步调度 reinstall，并通过 `installed_generation` 合并连续 reload，见
[`Driver.schedule()`](../src/dst_server/runtime/driver.py#L41-L52)。

但是 [`Server.execute()`](../src/dst_server/runtime/server.py#L184-L203)
只等待永久为真的 lifecycle ready，不等待当前 generation 的 driver 安装完成。

[`GameClient.request()`](../src/dst_server/game/client.py#L73-L89) 也没有 driver-ready 检查。

Lua 的 reset、regenerate 和 rollback 命令只触发游戏操作后立即返回，见
[`commands.lua`](../src/dst_server/lua/dst_server/commands.lua#L20-L37)。

所以命令返回不代表新 world 或新 Lua module 已经可用。

### SDK-004 Reinstall 失败路径

[`Driver.refresh()`](../src/dst_server/runtime/driver.py#L54-L79) 会记录 reinstall 异常，但不向 API 暴露该失败。

失败后 `task` 被清空，而同一 generation 不会再次 schedule。

`installed_generation` 会永久落后于当前 `generation`。

[`GameClient.health`](../src/dst_server/game/client.py#L47-L56) 仍保留上一个 generation 的成功结果。

因此外部可能同时观察到陈旧的健康状态和不可用的新 Lua runtime。

### SDK-004 失败时序

1. 旧 generation 的 driver 正常工作，lifecycle ready 已经是 `True`。
2. 调用者执行 reset、rollback 或 regenerate。
3. 触发命令返回成功，但游戏开始销毁旧 world 和 Lua 状态。
4. FD5 报告新的 session，Python 在后台调度 reinstall。
5. 调用者立即发起下一个类型化 RPC。
6. `wait_ready()` 因旧 flag 立即返回。
7. RPC 与 reinstall 竞争，可能遇到 module 不存在、driver 未安装或半初始化状态。
8. 如果 reinstall 失败，后续请求继续失败，但 health 仍可能显示旧 generation 成功。

### SDK-004 影响

所有会重建 world 或 Lua VM 的管理操作之后都存在竞态窗口。

调用方无法判断当前 API 是 ready、正在重装还是已经重装失败。

自动化管理程序只能依赖偶然的 `DST_LuaBusy` 重试，不能获得明确的一致性保证。

### SDK-004 根因

Lifecycle ready、游戏 session ready 和 Lua driver ready 被合并成了一个永久布尔状态。

`Driver` 虽然记录 generation 数字，却没有向请求路径提供"当前 generation 已安装"的等待或失败结果。

### SDK-004 修复约束

每次新 generation 开始时必须立即使旧 driver health 和 readiness 失效。

所有 `GameClient` 请求必须等待当前 generation 安装成功，或者收到当前 generation 的明确安装错误。

Reset、rollback 和 regenerate 是否等待新 generation，可以由各 API 的语义决定。

但它们返回后发起的下一条请求不能静默使用旧 readiness。

在 SDK-005 完成前不能盲目增加自动重试，否则可能重复安装 Lua Hook。

如果允许重试，应使用一个有界、可取消且有退避的单任务，而不是无延迟热循环。

### SDK-004 验收标准

- Reload 开始后，旧 `driver_health` 不再对外显示为当前健康状态。
- Reinstall 完成前，类型化 RPC 不会进入未安装的新 Lua runtime。
- Reinstall 成功后，等待中的请求只执行一次并正常继续。
- Reinstall 失败后，请求收到稳定且包含 generation 的错误。
- 持久失败不会形成无间隔重试循环。
- Reset、rollback、regenerate 和重复 Session marker 都有覆盖 generation 边界的测试。

## SDK-005：Lua 安装有副作用但不具备可安全重试性

状态：已确认，仍未解决。

### SDK-005 当前实现

首次 [`driver.install()`](../src/dst_server/lua/dst_server.lua) 会在修改 state 或游戏函数前完整验证 nonce、profile 和 action allowlist。

首次合法调用提交冻结配置后立即把 `state.installed` 设为 `true`，该标志只表达 core management driver 可用。

Profile 为 `off` 时不会加载任何 telemetry 模块，也不会产生 Hook 副作用。

Profile 为 `critical` 或 `history` 时，安装从 `telemetry_active=false` 开始，并在受保护的闭包中依次安装所需 Hook。

只有全部阶段成功后，动态开关才原子切换为 `active`。

如果后面的 world 或 player 阶段失败，前面的 action wrapper、shard wrapper、listener 或 watcher 仍可能已经物理安装。

失败会保存第一次有界错误，返回 `telemetry_status="failed"`，并让所有残留 Hook 保持逻辑 inactive。

Action 和 shard wrapper 在 inactive 时直接调用原函数，guarded callback 和 emit 路径也会在读取时钟或构造 payload 前立即返回。

同一 generation 的后续 install 直接返回已保存的 health，不再验证 options、改变配置或重试 telemetry。

### SDK-005 失败时序

1. 安装完成全部参数验证并提交 core 配置。
2. Action wrapper 安装成功。
3. Shard wrapper 安装成功。
4. World 或 player listener 安装中途失败。
5. Core 保持 installed，telemetry 进入 `failed` 并逻辑全量关闭。
6. 已经产生的物理 wrapper、listener 或 player marker 保留到当前 Lua runtime 结束。
7. 同一 generation 的重复 install 返回第一次失败结果，不执行第二次安装。

### SDK-005 影响

SDK-001 已经阻止当前 supervisor 在同一 generation 自动重试，所以部分安装不会因启动重试继续叠加。

但是失败的 telemetry 也无法在当前 generation 内恢复。

残留 Hook 虽然 inert，仍然是未回滚的物理修改，并会带来固定的低成本转发开销。

如果以后直接增加同 generation retry，重复 wrapper、listener 和半安装 player marker 的风险仍会重新出现。

### SDK-005 根因

Telemetry 安装需要修改多个没有统一卸载 API 的游戏函数和 listener registry。

这些物理副作用在整个 profile 成功前已经发生，而当前实现只有一个逻辑 active 开关。

逻辑关闭解决了游戏可用性边界，但没有实现物理 rollback 或可安全恢复的阶段级 retry。

### SDK-005 修复约束

这里不需要实现通用事务框架。

首次完整参数验证、单次提交和 inactive fast path 必须保留。

在允许同 generation retry 前，每个会包装全局函数或批量注册 listener 的阶段必须能够可靠卸载。

另一种选择是提供足以证明安全重入的完成 marker。

Player 只能在全部 listener 成功注册后标记为 attached。

如果某个游戏 API 没有可靠卸载能力，就必须依赖完整 preflight 和可独立判定的幂等提交，不能假装可以 rollback。

Profile 为 `off` 时不得产生任何 telemetry Hook 副作用。

### SDK-005 验收标准

- 当前 generation 的合法重复 install 不执行隐式 retry，也不叠加新的 Hook。
- 参数校验失败不会留下部分 allowlist、profile 或安装状态。
- 在 action、shard、world 和 player 每个阶段注入失败后，残留 Hook 都保持 inert，并精确保留原函数的参数、返回值和错误。
- 要启用安全 retry，失败阶段之前的副作用必须可回滚或可证明幂等。
- 安全 retry 后每个全局函数只能有一层 SDK wrapper，一个游戏事实只能产生一条 telemetry 记录。
- Player attachment 中途失败后必须能够重新完成，而不能被半安装 marker 永久跳过。
- `driver.health()` 只需公开 telemetry 的 `disabled`、`active`、`failed` 总体状态。
- Health 不再要求公开各 Hook 状态。

## SDK-006：`driver.health.players` 不是当前在线人数

状态：已解决。

### SDK-006 修复结果

`DriverHealth` 不再公开 `players`，避免把内部 Hook 附着状态误报为在线人数。

Lua 的 player 弱键表只保留为 Hook 附着去重状态，不再进入 health 契约。

[`GameClient.install()` 和 `get_health()`](../src/dst_server/game/client.py)
不再用 health 覆盖 Recorder 的 player count。

Recorder 人数继续由经过验证的 shard entered、shard left 和 stream close 事件维护。

需要即时查询游戏在线人数时，调用者继续使用 `world.room().player_count`。

## SDK-007：FD5 生命周期观察队列可能无限增长

状态：已确认。

### SDK-007 当前实现

[`Lifecycle.__init__()`](../src/dst_server/runtime/lifecycle.py#L13-L24)
创建了没有 `maxsize` 的 `asyncio.Queue`。

FD5 每产生一行，
[`Lifecycle.pump()`](../src/dst_server/runtime/lifecycle.py#L26-L40)
都会解析并交给 `handle()`。

[`Lifecycle.handle()`](../src/dst_server/runtime/lifecycle.py#L42-L60)
先更新 ready、session、save 和 stopping 状态，然后把每个事件无条件放入观察队列。

未知 FD5 行也会被
[`server.parse_event()`](../src/dst_server/events/server.py#L49-L67)
完整保留为 `UnknownEvent`。

SDK 暴露 [`Server.read_event()`](../src/dst_server/runtime/server.py#L211-L215)
用于消费该队列。

但是当前 cluster service 没有调用这个 API。

因此当前 Python cluster service 会持续生产 FD5 观察记录，却没有对应 consumer。

FD5 除生命周期 marker 外还可能包含周期统计和未知状态行。

### SDK-007 影响

队列长度和进程内存会随 FD5 消息总数持续增长。

运行时间越长、游戏统计输出越频繁，累计风险越高。

不能通过阻塞 FD5 pump 来施加背压，因为 pipe 写满会反向阻塞游戏进程。

### SDK-007 根因

同一个 `Lifecycle` 同时承担控制状态机和可选原始事件观察流。

控制状态已经单独保存在字段和 `asyncio.Event` 中，但所有原始记录又被无界复制到无人消费的队列。

### SDK-007 修复约束

控制状态更新必须继续优先执行，不能依赖观察队列是否有容量。

如果没有真实生产消费者，应删除原始观察队列和 `read_event()`。

如果保留公共观察能力，队列必须固定容量，满时丢弃最旧或最新的非关键观察记录并计数。

观察记录允许丢弃，因为 ready、session、save 和 stopping 的控制状态已经独立保存。

EOF sentinel 进入满队列时必须先腾出空间，确保 consumer 最终能够结束。

### SDK-007 验收标准

- 在没有 consumer 时连续输入大量 unknown 或 stats 行，内存占用保持有界。
- 队列满时，ready、session、save 和 stopping 状态仍然正确更新。
- FD5 pump 永远不会等待观察队列腾出空间。
- 丢弃数量可观察。
- EOF 后 consumer 能稳定读到结束状态。
- Cluster service 不再创建一个确定无人消费的无界事件副本。

## SDK-008：管理链路缺少总 Deadline 和资源上限

状态：已确认。

### SDK-008 当前实现

当前只有停止流程和 save 完成阶段具有局部 timeout。

以下关键路径没有总 deadline：

- [`Server.start_process()`](../src/dst_server/runtime/server.py#L115-L182)
  等待 FD5 ready 和初次 driver install。
- [`cluster.service.wait_for_start()`](../src/dst_server/cluster/service.py#L115-L129)
  等待全部 shard 启动。
- [`Console.execute()`](../src/dst_server/runtime/console.py#L30-L40)
  在 `DST_LuaBusy` 上无限重试。
- [`Console.read_result()`](../src/dst_server/runtime/console.py#L53-L66)
  无限等待 `DST_RemoteCommandDone`。
- [`GameClient.send()`](../src/dst_server/game/client.py#L91-L112)
  等待底层 execute，没有自己的 operation deadline。
- [`Driver.refresh()`](../src/dst_server/runtime/driver.py#L54-L66)
  等待当前 generation 的 driver install。
- [`cluster.mods.update()`](../src/dst_server/cluster/mods.py#L96-L114)
  等待 Mod updater stdout EOF 和进程退出。

[`Server.save()`](../src/dst_server/runtime/server.py#L220-L236)
的 `completion_timeout` 只包住 management command 返回后的 FD5 保存确认。

如果发送 save 的 FD4 命令阶段本身永久 Busy 或没有 Done，整个 save 调用仍可无限等待。

Cluster service 只有在全部 shard 完成 `start()` 后才创建 process wait task，见
[`serve()`](../src/dst_server/cluster/service.py#L211-L240)。

如果一个 shard 已退出而另一个 shard 永远不 ready，启动阶段可能一直等待，也不会进入正常的 fail-fast 退出监控。

Mod updater 在 [`prepare()`](../src/dst_server/cluster/service.py#L44-L85) 中运行。

`run()` 要等 `prepare()` 返回后才安装 SIGINT 和 SIGTERM handler，见
[`cluster.service.run()`](../src/dst_server/cluster/service.py#L257-L282)。

Updater 没有 deadline，取消或异常路径也没有显式 terminate、kill 和 wait。

所以更新进程卡住时，cluster 尚未进入正常 signal 和游戏进程监督阶段。

当前还缺少以下明确资源上限：

- FD3 单条 command 的字节数。
- FD4 单行和整个 response 的字节数及行数。
- `world.execute()` 的 Lua source 和返回 envelope 大小。
- `give_item` 单次创建实体的 count 上限。

例如 [`item_count()`](../src/dst_server/game/validation.py#L40-L46)
只验证 count 是正整数，没有最大值。

Lua 的 [`give_item()`](../src/dst_server/lua/dst_server/commands.lua#L149-L169)
会在游戏模拟线程按 count 循环 `SpawnPrefab`。

一次错误的巨大 count 就可能长时间阻塞模拟线程或耗尽内存。

### SDK-008 影响

游戏卡死、协议丢帧或错误配置都可能让 supervisor 永久停在启动或管理调用中。

外层容器仍然存活，所以编排系统不一定会把它识别成启动失败。

无限制命令、响应和实体创建还允许一次可信管理员误操作放大成游戏卡顿或 OOM。

### SDK-008 根因

底层 SDK 把无限等待留给调用者，但默认 cluster service 没有提供应用级总 deadline。

输入验证只检查类型和最小值，没有统一覆盖会消耗游戏线程、pipe 或 Python 内存的最大成本。

### SDK-008 超时恢复约束

仅在调用方外层套一个 `asyncio.timeout()` 不足以解决 FD4 问题。

命令已经写入 FD3 后，如果超时前没有读到 `DST_RemoteCommandDone`，Python 无法知道游戏是否仍会产生结果。

在没有 request ID 的协议上，超时后继续发送新命令会重新引入跨请求错位。

因此 FD4 deadline 到期后只能：

1. 在有限恢复期限内继续排空当前帧。
2. 排空失败时把 Console 标记为不可用，并停止或重启受管游戏进程。

每个顶层操作应在入口只计算一次基于单调时钟的绝对 deadline。

RPC 的同一预算必须覆盖 lock、旧 pending drain、Busy retry、writer drain 和等待 Done。

Save 的同一预算还必须继续覆盖 FD5 `DST_Saved`，不能在每个阶段重新开始计时。

启动 deadline 到期后也必须终止并清理已经创建的 shard、pipe 和 background task。

Mod updater 的取消、异常和 deadline 路径必须回收子进程。

仓库现有
[`steamcmd.terminate_process()`](../src/dst_server/steamcmd.py#L400-L409)
已经提供 terminate、grace period、kill 和 wait 的同类语义，不需要再设计第二套策略。

### SDK-008 资源限制约束

限制应放在共享信任边界，而不是分散到每个调用方。

FD3 command、FD4 response 和 Lua envelope 应使用明确且一致的字节预算。

会在模拟线程循环或创建实体的 API 必须在 Python 和 Lua 两侧都验证业务上限。

具体上限应该根据正常游戏管理需求确定，但不能以"调用者可信"为理由保持无限。

不需要为此设计通用配额框架。

少量常量、显式 timeout 和稳定错误类型已经足够。

`Server.wait()`、`read_event()`、`read_game_event()` 和 cluster `serve()` 本身是长期等待 API，
不应机械添加短 timeout。

可信管理员通过 raw Lua 执行 `while true do end` 时，唯一可靠的恢复方式是终止 shard。

Deadline 不能把任意 Lua 执行变成可安全抢占的沙箱。

### SDK-008 验收标准

- 游戏从不报告 ready 时，单 shard 和 cluster 启动都在 deadline 内失败并清理所有进程。
- 一个 shard 提前退出、另一个 shard 卡住时，cluster 不会永久停在启动阶段。
- `DST_LuaBusy` 永久持续时，调用在 deadline 内结束。
- 命令发送后永远没有 `DST_RemoteCommandDone` 时，后续命令会被拒绝，直到进程恢复或重启。
- `save(timeout=T)` 卡在 RPC 或 FD5 时，都从方法入口开始受同一个 `T` 约束。
- 多个小行组成的巨大 FD4 response 也会触发总大小限制，并保持后续帧边界。
- 超长 FD3 command 在写入游戏前被拒绝。
- 卡住的 driver reinstall 会在 deadline 内进入明确的 failed 和 not-ready 状态。
- `give_item` 在 Python 和 Lua 两侧都接受 `1..N`，并在任何 `SpawnPrefab` 前拒绝其他 count。
- Mod updater 超时或被取消后，不留下运行中的子进程。
- Timeout 拒绝布尔值、非数字、非有限值和小于等于零的值。

## 修复依赖关系

SDK-002 必须与 SDK-008 的 RPC deadline 一起设计，否则超时仍可能破坏 FD4 帧边界。

SDK-003 和 SDK-004 应共享明确的 stream generation 事实，但 session 必须继续来自 Lua 事件产生时的 world。

SDK-004 的 reload-ready 屏障可以不依赖 retry，但任何会复用部分安装 Lua runtime 的自动重试仍必须等待 SDK-005 的物理回滚或幂等提交完成。

SDK-007 可以独立修复，不需要等待新的生命周期抽象。

SDK-001 和 SDK-006 已完成，剩余问题的最小收敛顺序是 SDK-002、SDK-005、SDK-004、SDK-003、SDK-008、SDK-007。
