# `0x10000000` 定向与阅读模型

本目录负责读者进入索引前的定向、BBC 文档树模型、源码快照、阅读路径、术语和维护约束。

本目录直接承载进入索引前的 reading model。

目录级语义由本 README 承载，独立专题文件只解释具体问题或具体阅读链路。

## `0x10001111` 目录职责 / 入口边界 / 目录载体 / 验证点

BBC 在这里不是文件名前缀规则，而是 Markdown 文档树的安排规范。

本目录回答“怎么进入索引”“怎么读编码”“怎么选择第一条源码链路”。

运行时、实体、动作、世界生成和 reference 的业务细节进入后续区域。

目录自身的定位、边界和子页面索引保留在 `README.md`。

目录名 `0x1000-orientation` 只写 4 位 name code。

本 README 的 H1 写完整布局恢复后的 heading code `0x10000000`。

## `0x10002111` 专题索引 / 直接入口 / 从规范到维护 / 链接校验

- [BBC 文档树规范](0x1001-bbc-encoding.md)
- [源码快照](0x1002-source-snapshot.md)
- [阅读路径](0x1003-reading-workflows.md)
- [术语表](0x1004-glossary.md)
- [维护规则](0x1005-maintenance.md)

## `0x10003111` 阅读入口 / 最小路径 / 先校准模型 / 抽样动作

先读本 README，确认目录载体和独立专题文件的分工。

再读 BBC 文档树规范，确认文件内标题编码不是字符串追加。

然后读源码快照，确认本索引覆盖的 DST Lua 文件边界。

最后按阅读路径选择启动、动作、AI 或世界生成中的一条链路。

## `0x10004111` 源码入口 / 第一批锚点 / 从运行时开始 / 搜索信号

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/main.lua` | loader / mod startup / debug startup | 安装加载器、建立全局对象并启动 mod/debug 装载 |
| `scripts/mainfunctions.lua` | `Start` / `RunScript` | 创建 `TheFrontEnd` 并装入 `gamelogic` |
| `scripts/frontend.lua` | `FrontEnd` / screen stack | 管理 screen、输入分发和调试面板 |
| `scripts/gamelogic.lua` | `DoGenerateWorld` / `LoadSlot` | 决定读档、生成世界和前端切换 |
| `scripts/mods.lua` | `ModManager:LoadMods` | 加载 modmain 与 mod worldgen 相关入口 |
| `scripts/modindex.lua` | `KnownModIndex` | 维护 mod 配置、依赖和启动序列 |
| `scripts/entityscript.lua` | `EntityScript` / `AddComponent` | 实体、组件、事件和动作的交汇点 |
| `scripts/componentactions.lua` | `CollectActions` | 把组件能力汇总为候选动作 |
| `scripts/worldgen_main.lua` | `GenerateNew` | 世界生成入口 |

`main.lua` 顶层 loader 和全局模块加载早于 `require("mainfunctions")` 的运行时 handoff。

不要把 `require("mainfunctions")` 本身写成 loader 安装点。
