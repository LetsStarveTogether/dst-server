# `0x22010000` 引擎全局

引擎全局页解释 C++ 注入对象如何进入 Lua 运行时。

本页只追 `TheSim`、`TheNet`、`TheInput` 和实体包装层的边界。

## `0x22011111` 本页定位 / 要回答的运行时问题 / Lua 能力来自哪里 / 验证点

`TheSim`、`TheNet`、`TheInput` 不是 Lua 文件里 new 出来的对象。

Lua 侧通过 `mainfunctions.lua`、`input.lua`、`networking.lua` 把这些对象包装成可读的运行时入口。

## `0x22011211` 本页定位 / 不要混淆的边界 / 引擎对象与 Lua 管理表 / 搜索信号

看到 `TheSim:CreateEntity()` 时，继续追 `EntityScript(ent)` 和 `Ents[guid]`。

看到 `TheNet:GetIsServer()` 时，继续判断当前逻辑是否只允许 server 写入。

## `0x22012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/main.lua` | `KnownModIndex:Load` | 启动阶段开始装载 mod 与运行时资源 |
| `scripts/main.lua` | `require("globalvariableoverrides")` | 加载全局变量覆盖文件 |
| `scripts/main.lua` | `require("standardcomponents")` | 加载标准组件默认回调 |
| `scripts/mainfunctions.lua` | `CreateEntity` | 把 `TheSim:CreateEntity()` 包成 `EntityScript` |
| `scripts/mainfunctions.lua` | `SavePersistentString` | 统一调用 `TheSim:SetPersistentString` |
| `scripts/mainfunctions.lua` | `ReplicateEntity` | 由 guid 找到 `Ents` 后触发实体复制 |
| `scripts/standardcomponents.lua` | `DefaultBurntFn` | 全局默认组件行为函数 |
| `scripts/globalvariableoverrides.lua` | intentionally blank | 主覆盖文件不注入额外变量 |
| `scripts/input.lua` | `TheInput` | 输入更新与动作候选收集的全局入口 |
| `scripts/networking.lua` | `SerializeUserSession` | Lua 侧封装 `TheNet` 的玩家会话序列化 |

### `0x22012111` 主锚点 / `scripts/mainfunctions.lua` / 搜索信号

从 `CreateEntity` 开始可以看到 Lua 对实体生命周期的最小控制面。

`CreateEntity` 创建引擎实体、读取 guid、构造 `EntityScript`，再登记到全局 `Ents`。

### `0x22012211` 输入锚点 / `scripts/input.lua` / 验证点

`TheInput` 是输入全局对象。

它连接前端按键、HUD 交互和后续动作页里的 component action 收集。

### `0x22012311` 网络锚点 / `scripts/networking.lua` / 验证点

`SerializeUserSession` 会调用 `player:GetSaveRecord()`，再交给 `TheNet:SerializeUserSession()`。

这说明网络会话保存仍然依赖实体持久化结构。

### `0x22012411` 启动辅助锚点 / `standardcomponents.lua` / 验证点

`standardcomponents.lua` 定义 `DefaultIgniteFn`、`DefaultBurnFn`、`DefaultBurntFn` 等全局默认函数。

这些函数会被 prefab 或 component 配置复用，而不是由引擎自动调用。

## `0x22013000` 运行流程

~~~mermaid
flowchart TD
    A["C++ engine globals"]
    A --> B["TheSim / TheNet / TheInput"]
    B --> C["Lua wrapper functions"]
    C --> D["EntityScript / Ents / Managers"]
    D --> E["components, stategraphs, UI"]
~~~

### `0x22013111` `TheSim` 到实体 / `CreateEntity` / 边界条件

`TheSim:CreateEntity()` 只返回底层实体。

Lua 的组件、事件、任务和持久化都挂在随后创建的 `EntityScript` 上。

### `0x22013211` `TheNet` 到权威判断 / `TheNet:GetIsServer` / 边界条件

写世界状态前先确认 server 权威路径。

`SaveGame` 在 client 侧会直接返回，并打印禁用提示。

### `0x22013311` `TheInput` 到玩家意图 / 输入更新 / 边界条件

输入只能产生意图和候选动作。

真正修改世界仍需要动作、组件或网络 RPC 的 server 侧路径。

## `0x22014111` 结构细节 / 全局表 / `Ents` / 需要核对的字段

`Ents[guid]` 保存 guid 到 `EntityScript` 的映射。

`OnRemoveEntity` 会清理 `Ents`、任务、更新队列、brain 和 stategraph。

## `0x22014211` 结构细节 / 包装函数 / 持久化包装 / 需要核对的字段

`SavePersistentString` 是 Lua 对 `TheSim:SetPersistentString` 的薄封装。

世界保存不直接用它写完整世界，而是通过 `SerializeWorldSession` 和 `ShardGameIndex` 收尾。

## `0x22014311` 结构细节 / 管理器入口 / 更新与移除 / 需要核对的字段

实体移除会通知 `BrainManager` 和 `SGManager`。

所以实体生命周期页、AI 页和本页共享同一个 guid 生命周期入口。

## `0x22014411` 结构细节 / 全局覆盖文件 / `globalvariableoverrides.lua` / 需要核对的字段

tracked 的主文件只有 `-- Intentionally blank`。

同目录还有平台或环境变体文件，但主启动链路只 require `globalvariableoverrides`。

## `0x22015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "CreateEntity|OnRemoveEntity|SavePersistentString|ReplicateEntity|TheSim|TheNet|TheInput" \
  scripts/main.lua \
  scripts/mainfunctions.lua \
  scripts/standardcomponents.lua \
  scripts/globalvariableoverrides.lua \
  scripts/input.lua \
  scripts/networking.lua
~~~

### `0x22015111` 推荐顺序 / 最小闭环

先读 `CreateEntity` 和 `OnRemoveEntity`。

再读一个 `TheNet:GetIsServer()` 分支。

最后回到 `TheInput`，确认输入链路只提交意图。
