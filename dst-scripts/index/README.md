# `0x0000` DST Scripts Index

DST Scripts Index 是面向系统阅读 DST Lua 源码的中文索引。

它使用 [BBC](https://github.com/WH-2099/BBC) 组织 Markdown 文档树。

文中的 `scripts/...` 源码路径和命令均以父目录 `dst-scripts/` 为基准。

## `0x1111` 文档树模型 / BBC 规则 / 目录到标题 / 验证点

根 `README.md` 是文档根目录的 README 载体，H1 使用根载体编码 `0x0000`。

目录、独立文件和标题必须都能恢复到 BBC 的 8 位布局 `0xD1D2D3FH2H3H4H5`。

文件系统名称只写 `D1D2D3F` 的 name code。

文件内标题用 heading code 记录逻辑路径。

Markdown 标题层级按实际内容自然停止，不为凑齐 H5 创建空标题。

标题序号不能追加在文件级编码字符串后面。

每个目录必须有 `README.md` 载体来承载目录定位、边界、索引和推荐入口。

## `0x2111` 顶层区域 / 运行时阅读路径 / 区域入口 / 链接校验

- [定向与阅读模型](0x1000-orientation/README.md)
- [运行时](0x2000-runtime/README.md)
- [实体与动作](0x3000-entity-action/README.md)
- [AI 与动画](0x4000-ai-animation/README.md)
- [世界模拟](0x5000-world-simulation/README.md)
- [玩法系统](0x6000-gameplay-systems/README.md)
- [前端数据与工具](0x7000-frontend-data-tools/README.md)
- [Reference](0x8000-reference/README.md)

## `0x3111` 推荐阅读路径 / 从启动到动作 / 最小源码闭环 / 抽样链路

推荐先读 `scripts/main.lua` 的 `require` 顺序，再进入 `scripts/mainfunctions.lua` 的 `Start`。

动作链路从 `scripts/input.lua` 采集输入。
再进入 `scripts/componentactions.lua`、`scripts/bufferedaction.lua` 和 `EntityScript:PushBufferedAction`。

AI 与动画链路从 `StateGraphInstance:StartAction` 和 `BrainWrangler:Update` 分别确认表现层与决策层。

世界生成链路先从 `scripts/gamelogic.lua` 的 `DoGenerateWorld` 看到 `WorldGenScreen`。
再进入 `scripts/worldgen_main.lua` 的 `GenerateNew`、`forest_map.Generate` 和 `Story:GenerateNodesFromTasks`。

如果只想查完整文件，直接进入 `0x8000-reference`。

## `0x4111` 运行关系图 / 入口到专题 / 源码路径 / 边界条件

~~~mermaid
flowchart TD
    A["main.lua require 顺序"]
    A --> B["mainfunctions.lua Start"]
    B --> C["frontend.lua screen/input"]
    C --> D["gamelogic.lua 世界装载或生成"]
    D --> E["entityscript.lua EntityScript"]
    E --> F["componentactions.lua 动作采集"]
    F --> G["bufferedaction.lua 延迟动作"]
    G --> H["stategraph.lua / brain.lua"]
    D --> I["WorldGenScreen"]
    I --> J["worldgen_main.lua GenerateNew"]
    J --> K["map/level.lua 与 map/storygen.lua"]
~~~
