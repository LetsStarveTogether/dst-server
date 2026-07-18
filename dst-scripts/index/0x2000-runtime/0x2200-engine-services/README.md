# `0x22000000` 引擎服务

本目录承载引擎全局、存档读档和网络运行时的目录语义。

它还拆出启动基础设施和世界设置运行时，避免 `0x2201` 变成混合页。

目录级语义由本 README 承载，独立专题文件只承载具体问题、链路或清单。

## `0x22001111` 目录职责 / 文档边界 / 目录载体 / 验证点

本 README 只说明目录定位、子页面边界和推荐入口。

具体源码行为进入本目录下的独立专题文件。

不要让第一个独立文件替代目录 README。

## `0x22002111` 子页面索引 / 推荐顺序 / 从目录到专题 / 链接校验

- [引擎全局](0x2201-engine-globals.md)
- [存档与读档](0x2202-save-load.md)
- [网络运行时](0x2203-network-runtime.md)
- [运行时基础设施](0x2204-runtime-foundations.md)
- [World Settings Runtime](0x2205-world-settings-runtime.md)

## `0x22003111` 阅读入口 / 最小路径 / 先定位再展开 / 抽样动作

优先进入 `0x2201-engine-globals.md`，再读 `0x2204-runtime-foundations.md`。

随后按问题域进入存读档、网络或世界设置页面。

如果要查完整清单，回到 `0x8000-reference/README.md`。
