# `-cloudserver` 双向通信

`-cloudserver` 是 DST Linux 专用服务器提供的本地进程间通信协议。

本项目用它执行 Lua、取得命令结果并观察服务端生命周期。

## 原生 FD 协议

启用 `-cloudserver` 后，DST 会直接使用三个已经打开的文件描述符：

| FD | 方向 | 内容 | 边界 |
| --- | --- | --- | --- |
| 3 | 管理进程 → DST | Lua 命令 | 每条命令以换行结束。 |
| 4 | DST → 管理进程 | 当前命令的文本输出 | 以独立一行 `DST_RemoteCommandDone` 结束。 |
| 5 | DST → 管理进程 | 生命周期与状态消息 | 每条消息占一行。 |

FD 3、4、5 与标准输入、标准输出和标准错误相互独立。

普通游戏日志仍通过 stdout 和 stderr 输出。

调用方必须等当前命令收到 `DST_RemoteCommandDone` 后才能发送下一条命令。

服务端暂时不能执行 Lua 时会在 FD 4 返回 `DST_LuaBusy`。

FD 5 独立于命令执行，并会报告 Ready、Session、Saved、Stopping 和 Shutdown 等状态。

## 命令帧

FD 4 原生协议没有请求 ID、结果类型或错误模型。

本项目用一把分片进程内锁串行执行命令，并为每次尝试生成独立 ULID。

命令会输出与 ULID 绑定的 `START` 和 `END` 标记，以隔离命令内容与原生控制行。

只有匹配的 `START`、`END` 和随后到达的原生 `DST_RemoteCommandDone` 才会完成当前请求。

命令自身打印的 `DST_RemoteCommandDone`、`DST_LuaBusy` 或其他请求标记只会作为普通结果文本处理。

只有在 `START` 前且此前没有普通输出的原生 `DST_LuaBusy` 才表示本次尝试未执行。

收到该 Busy 后，SDK 会等待片刻并使用新的 ULID 重试，直到调用超时。

Lua 编译或执行错误会作为普通结果文本返回，并在完整排空当前帧后结束。

Lua 的 `return` 值不会自动进入 FD 4，原始命令需要显式 `print` 才能返回文本。

类型化游戏请求会打印一个 `DST_SERVER_RESULT|` JSON envelope，并使用严格模型校验成功值或错误。

最终 FD 3 命令行最多为 65,536 bytes，并包含结尾换行。

一个 FD 4 结果帧最多包含 65,536 bytes 和 1,024 行 payload。

过大的结果会被排空到帧边界后报告错误，因此下一条命令仍可安全执行。

EOF、传输错误或读取任务取消会使帧无法重新对齐，此后 Console 会拒绝继续执行命令。

## 进程接管

父进程为三个方向各创建一根匿名 pipe，并且只保留本端需要的 pipe 端点。

轻量启动 wrapper 在执行 DST 前把子进程端点映射到准确的 FD 3、4、5，再关闭多余副本。

三个协议流和 stdout 必须被持续消费，否则写满的 pipe 会反向阻塞游戏进程。

## 生命周期与游戏事件

FD 5 只承载 DST 原生生命周期消息，不是通用游戏事件总线。

项目注入的 Lua driver 使用带 `DST_OTEL|` 前缀的 JSON 行报告游戏事件。

同步命令期间产生的事件进入 FD 4，异步游戏回调产生的事件进入 stdout。

FD 4 与 stdout 因此使用同一个事件解析器，并把其余文本分别保留为命令结果或普通日志。

每个分片拥有独立的 Console、生命周期状态、事件队列和 Lua generation。

## 安全边界

FD 3 可以执行任意服务端 Lua，只能交给与 DST 位于同一信任域的 Agent。

不得把 FD 3 或原始 Console 直接暴露为未经鉴权的网络接口。

对外管理应通过公开的 Cap'n Proto 集群 RPC，并由上层部署负责 socket 访问控制。

## 参考资料

- [Klei Forum：`-cloudserver` 的 FD 3、4、5](https://forums.kleientertainment.com/forums/topic/118972-unix-python-web-portal-for-dedicated-dst-server/#findComment-1344090)
- [Klei Forum：每个方向使用独立 pipe](https://forums.kleientertainment.com/forums/topic/140113-problem-with-file-descriptors/#findComment-1568906)
- [Python：`os.dup2()`](https://docs.python.org/3/library/os.html#os.dup2)
