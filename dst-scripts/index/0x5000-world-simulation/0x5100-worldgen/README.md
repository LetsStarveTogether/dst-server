# `0x51000000` 世界生成

本目录承载 worldgen 从参数入口到 `savedata.map` 与 `savedata.ents` 的完整阅读路径。

目录 README 只定义边界和顺序。

具体源码事实放入独立专题页。

## `0x51001111` 目录职责 / 文档边界 / 本目录覆盖什么 / 验证点

本目录覆盖 `scripts/worldgen_main.lua`、`scripts/map/*`、`scripts/tiledefs.lua`、
`scripts/worldtiledefs.lua`、`scripts/tilemanager.lua` 和 `scripts/tilegroups.lua`
中与新世界生成直接相关的链路。

它不把存档升级、运行时世界状态、组件刷新和 prefab 行为混进 worldgen 入口。

这些内容分别进入 `0x5200-world-state`、`0x6000-gameplay-systems` 或 `0x8000-reference`。

## `0x51002111` 子页面索引 / 推荐顺序 / 从入口到输出 / 链接校验

- [Worldgen Main](0x5101-worldgen-main.md)
- [Levels Tasks Rooms](0x5102-levels-tasks-rooms.md)
- [Layouts](0x5103-layouts.md)
- [Forest Map Output](0x5104-forest-map-output.md)

## `0x51002211` 子页面索引 / 页面分工 / 不重复写同一事实 / 验证点

`0x5101` 只回答入口、重试、参数、DLC、存档校验和 world entity 注入。

`0x5102` 只回答 preset、task set、task、room 与 story graph。

`0x5103` 只回答 graph layout、static layout、set piece 与 object layout。

`0x5104` 只回答 `forest_map.Generate` 如何把拓扑烘焙成 tile map、实体表、海洋、道路和季节初始数据。

## `0x51003111` 阅读入口 / 最小路径 / 先定位再展开 / 抽样动作

优先从 `0x5101-worldgen-main.md` 建立 `GEN_PARAMETERS -> GenerateNew -> forest_map.Generate` 主链路。

然后进入 `0x5102-levels-tasks-rooms.md` 确认 `Level:ChooseTasks` 到 `Story:GenerateNodesFromTask` 的数据展开。

再进入 `0x5103-layouts.md` 确认 set piece 和 static layout 如何进入 `entities`。

最后进入 `0x5104-forest-map-output.md` 确认 `WorldSim`、海洋、道路、编码和 `savedata` 字段。

如果要查完整文件清单，进入 `0x8000-reference/README.md`。
