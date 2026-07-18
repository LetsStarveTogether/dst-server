# `0x80000000` Reference

Reference 区只放可审计清单，不承载长篇行为解释。

目录级语义由本 README 承载，独立专题文件只解释具体运行链路。

## `0x80001111` 区域定位 / 清单入口 / 机器可读块 / 验证点

本区集中完整文件清单、Prefab/Component/SG/Brain/Widget/Screen 目录。

## `0x80002111` 源码锚点 / 清单入口 / 机器可读块 / 验证点

完整清单以 `git ls-files --recurse-submodules scripts` 为准。

~~~bash
git ls-files --recurse-submodules scripts
~~~

## `0x80003111` 目录索引 / README 载体 / 二级目录 / 链接校验

以下入口先进入目录 README，再进入具体专题文件。

- [文件清单](0x8100-file-inventory/README.md)
- [完整文件清单](0x8100-file-inventory/0x8101-complete-file-inventory.md)
- [根文件清单](0x8100-file-inventory/0x8102-root-files.md)
- [运行时 Catalog](0x8200-runtime-catalogs/README.md)
- [Prefab Catalog](0x8200-runtime-catalogs/0x8201-prefab-catalog.md)
- [Component Catalog](0x8200-runtime-catalogs/0x8202-component-catalog.md)
- [StateGraph Brain Catalog](0x8200-runtime-catalogs/0x8203-stategraph-brain-catalog.md)
- [UI 与世界 Catalog](0x8300-ui-world-catalogs/README.md)
- [UI Catalog](0x8300-ui-world-catalogs/0x8301-ui-catalog.md)
- [Map Scenario Catalog](0x8300-ui-world-catalogs/0x8302-map-scenario-catalog.md)
