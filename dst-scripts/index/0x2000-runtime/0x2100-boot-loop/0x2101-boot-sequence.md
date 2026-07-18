# `0x21010000` 启动序列

启动序列页按 Lua 可见入口阅读。
`main.lua` 的顶层代码负责装配环境，`Start()` 负责进入前端和 `gamelogic.lua`，读档或生成完成后才会执行 `DoInitGame()`。

## `0x21011111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是区分三件事。
第一是 `main.lua` 顶层 require 和 `ModSafeStartup()` 的启动副作用。
第二是全局 `Start()` 创建 `TheFrontEnd` 并加载 `gamelogic.lua`。
第三是 `gamelogic.lua` 的异步 Profile、SaveIndex、ShardIndex 回调进入 `DoResetAction()`。

## `0x21012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/main.lua` | `ModSafeStartup` | 清理文件系统别名、加载 mod、加载全局 prefab 数据 |
| `scripts/main.lua` | `KnownModIndex:Load` | mod 启动序列完成后调用 `ModSafeStartup` |
| `scripts/mainfunctions.lua` | `Start` | 创建 `TheFrontEnd` 并 `require("gamelogic")` |
| `scripts/mainfunctions.lua` | `GlobalInit` | 加载 `global` 和全局事件 prefab |
| `scripts/gamelogic.lua` | `DoResetAction` | 按 reset action 分流到前端、读档、生成、客户端连接 |
| `scripts/gamelogic.lua` | `LoadSlot` | 判断当前 shard 是否已有世界存档 |
| `scripts/gamelogic.lua` | `DoGenerateWorld` | 生成世界并把结果交给 `DoInitGame` |
| `scripts/gamelogic.lua` | `DoLoadWorld` | 读取已有世界并把结果交给 `DoInitGame` |
| `scripts/gamelogic.lua` | `DoInitGame` | 校验 `savedata`、填充世界、执行 `TheWorld:PostInit()` |
| `scripts/gamelogic.lua` | `ActivateWorld` | 玩家激活后取消暂停并恢复音频混音 |

### `0x21012111` 主锚点 / `scripts/main.lua` / 搜索信号

先搜索 `ModSafeStartup`，确认它在 `MODS_ENABLED` 分支中的调用位置。
再搜索 `function Start`，确认 `Start()` 不在 `main.lua` 中被 Lua 代码直接调用。

## `0x21013000` 运行流程

~~~mermaid
flowchart TD
    A["main.lua top-level"]
    A --> B["require mainfunctions, scheduler, stategraph, brain, update"]
    A --> C["KnownModIndex startup sequence"]
    C --> D["ModSafeStartup"]
    D --> E["LoadPrefabFile and data registries"]
    D --> F["GlobalInit when RUN_GLOBAL_INIT"]
    G["engine-facing Start()"] --> H["TheFrontEnd = FrontEnd()"]
    H --> I["require gamelogic"]
    I --> J["Profile and SaveIndex load callbacks"]
    J --> K["OnFilesLoaded"]
    K --> L["DoResetAction"]
    L --> M{"reset action"}
    M --> N["LoadAssets FRONTEND"]
    M --> O["LoadSlot"]
    M --> P["TheNet:StartClient"]
    O --> Q["DoLoadWorld or DoGenerateWorld"]
    Q --> R["LoadAssets BACKEND"]
    R --> S["DoInitGame"]
    S --> T["TheWorld listens playeractivated"]
    T --> U["OnPlayerActivated"]
    U --> V["ActivateWorld"]
~~~

### `0x21013111` 流程分段 / `main.lua` 顶层阶段 / 边界条件

`main.lua` 顶层直接 require 大量基础模块。
`ModSafeStartup()` 负责 mod、翻译、prefab 注册、全局数据 registry 和 `TheGlobalInstance`。
如果 `RUN_GLOBAL_INIT` 为真，它在 `ModSafeStartup()` 内调用 `GlobalInit()`。

### `0x21013121` 流程分段 / `Start()` 到 `gamelogic.lua` / 边界条件

`Start()` 建立 `TheFrontEnd` 后加载 `gamelogic.lua`。
`gamelogic.lua` 的文件尾部创建 `SaveGameIndex`、`ShardSaveGameIndex`、`ShardGameIndex` 等对象，并通过嵌套回调进入 `OnFilesLoaded()`。
这段不是 `Start -> LoadSlot` 的同步直连。

### `0x21013131` 流程分段 / 世界激活阶段 / 边界条件

`DoInitGame()` 在世界数据 ready 后填充世界、执行 `ModManager:SimPostInit()` 和 `TheWorld:PostInit()`。
`ActivateWorld()` 不是 `DoInitGame()` 的直接尾调用。
它由 `playeractivated` 事件中的 `OnPlayerActivated()` 触发，负责取消暂停和淡入。

## `0x21014111` 结构细节 / 数据结构与生命周期 / `Settings.reset_action` / 需要核对的字段

`DoResetAction()` 根据 `RESET_ACTION.LOAD_FRONTEND`、`LOAD_SLOT`、`LOAD_FILE`、`JOIN_SERVER` 等分支决定后续路径。
读世界启动时，应先确认当前 reset action，再判断是否进入 `LoadSlot()`。

## `0x21014121` 结构细节 / 数据结构与生命周期 / `savedata` / 需要核对的字段

`DoInitGame()` 明确断言 `savedata.map`、`savedata.map.prefab`、`savedata.map.tiles`、`savedata.map.topology` 和 `savedata.ents`。
这些断言比页面目录更适合作为读档数据结构的第一批锚点。

## `0x21015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n \
  -e "ModSafeStartup" \
  -e "function Start" \
  -e "function GlobalInit" \
  -e "local function DoResetAction" \
  -e "local function LoadSlot" \
  -e "local function DoInitGame" \
  -e "local function ActivateWorld" \
  scripts/main.lua \
  scripts/mainfunctions.lua \
  scripts/gamelogic.lua
~~~

### `0x21015111` 推荐顺序 / 最小闭环

先读 `main.lua` 中 `ModSafeStartup()` 的调用分支。
再读 `mainfunctions.lua` 的 `Start()` 和 `GlobalInit()`。
最后读 `gamelogic.lua` 从 `Profile:Load()` 到 `DoResetAction()`、`LoadSlot()`、`DoInitGame()`、`OnPlayerActivated()` 的链路。
