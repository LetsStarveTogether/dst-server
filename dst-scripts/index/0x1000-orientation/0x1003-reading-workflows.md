# `0x10030000` 阅读路径

推荐先读运行链路，再读实体、动作、AI、世界生成，最后用 reference 补全文件覆盖。

## `0x10031111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

读者要避免从海量 Prefab 清单开始迷路，而是沿着一次玩家操作或一次世界生成来读源码。

每条路径都要能落到一个函数调用、一个表结构或一个状态机入口。

## `0x10032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/input.lua` | `Input` / `TheInput` | C++ 输入回调如何进入 Lua |
| `scripts/componentactions.lua` | `CollectActions` | 组件如何产出候选动作 |
| `scripts/bufferedaction.lua` | `BufferedAction` | 动作如何延迟执行 |
| `scripts/entityscript.lua` | `PushBufferedAction` | 动作如何进入实体和 SG |
| `scripts/gamelogic.lua` | `DoGenerateWorld` | 运行时发起世界生成界面 |
| `scripts/worldgen_main.lua` | `GenerateNew` | 世界生成如何闭环 |

### `0x10032111` 主锚点 / `scripts/input.lua` / 搜索信号

先在 `scripts/input.lua` 搜索 `TheInput = Input()`。

动作候选不要停在 `actions.lua` 的定义表，要继续追到 `EntityScript:CollectActions` 与 `BufferedAction:Do`。

## `0x10033000` 运行流程

~~~mermaid
flowchart TD
    A["main.lua"]
    A --> B["mainfunctions.lua Start"]
    B --> C["gamelogic.lua"]
    C --> D["input.lua TheInput"]
    D --> E["componentactions.lua CollectActions"]
    E --> F["bufferedaction.lua BufferedAction"]
    F --> G["entityscript.lua PushBufferedAction"]
    G --> H["stategraph.lua StartAction"]
    C --> I["gamelogic.lua DoGenerateWorld"]
    I --> J["WorldGenScreen / worker"]
    J --> K["worldgen_main.lua GenerateNew"]
    K --> L["map/level.lua Level:ChooseTasks"]
    K --> M["map/forest_map.lua Generate"]
    M --> N["BuildStory"]
    N --> O["map/storygen.lua Story:GenerationPipeline"]
~~~

### `0x10033111` 流程分段 / 入口到副作用 / 边界条件

- 动作链路的 `input.lua` 负责输入抽象，不直接执行世界状态改变。
- `BufferedAction:Do` 调用 `self.action.fn(self)`，这里才进入动作副作用。
- 运行时通过 `gamelogic.lua` 发起世界生成界面或 worker。
- `worldgen_main.lua` 是独立生成上下文，不是普通游戏帧循环。

## `0x10034111` 结构细节 / 数据结构与生命周期 / 具体 Lua 结构 / 需要核对的字段

- 读动作链路时固定从 `input.lua` 进入，不从 `actions.lua` 的定义表开始。
- 读 AI 链路时先看 `StateGraphInstance:StartAction` 响应动作，再看 `BrainWrangler:Update` 选择意图。
- 读世界链路时先看 `GenerateNew` 调用 `Level:ChooseTasks`。
- 再追 `forest_map.Generate`、`BuildStory` 和 `Story:GenerationPipeline`。
- 读网络链路时先看 server 权威对象，再看 `entityreplica.lua` 和 classified prefab。

## `0x10035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "TheInput = Input|CollectActions|BufferedAction|PushBufferedAction" \
  scripts/input.lua \
  scripts/componentactions.lua \
  scripts/bufferedaction.lua \
  scripts/entityscript.lua
rg -n "GenerateNew|ChooseTasks|GenerateNodesFromTasks" \
  scripts/worldgen_main.lua \
  scripts/map/level.lua \
  scripts/map/storygen.lua
~~~

### `0x10035111` 推荐顺序 / 最小闭环

先追一条动作成功路径，再追一条 worldgen 成功路径。

如果一个搜索词在多个文件同名出现，页面必须写明当前上下文。
