# `0x10020000` 源码快照

`scripts` 跟踪文件为 4045 个，其中 Lua 文件为 4030 个。

`scripts` 子模块快照为 `3b390612`，对应上游提交信息 `740256`。

`scripts/components/` 有 821 个 Lua 文件。

`scripts/prefabs/` 有 1594 个 Lua 文件。

`scripts/stategraphs/` 有 261 个 Lua 文件。

重点阅读区包括 Carnival 高尔夫、Eets critter 状态、物品温度、干燥剂、Vault Scrapbook 数据、皮肤文本和运行时边界。

## `0x10021111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

读者要先知道源码规模，再决定先读运行链路还是进入 reference 清单。

本页统计口径固定为 `git ls-files --recurse-submodules scripts`。

## `0x10022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `LoadScript` / `RunScript` | 运行时脚本缓存与执行 |
| `scripts/worldgen_main.lua` | `LoadScript` / `RunScript` / `GenerateNew` | 世界生成脚本缓存与生成入口 |
| `scripts/prefabs.lua` | `Prefab = Class` | Prefab 对象定义入口 |
| `scripts/entityscript.lua` | `AddComponent` | 组件挂载入口 |

### `0x10022111` 主锚点 / `scripts/mainfunctions.lua` / 搜索信号

先在 `scripts/mainfunctions.lua` 搜索 `LoadScript` 与 `RunScript`。

再在 `scripts/worldgen_main.lua` 搜索同名函数，确认 worldgen 使用独立加载上下文。

## `0x10023000` 运行流程

~~~mermaid
flowchart TD
    A["git ls-files"]
    A --> B["目录聚合"]
    B --> C["运行时专题"]
    C --> D["reference 覆盖"]
~~~

### `0x10023111` 流程分段 / 入口到副作用 / 边界条件

- 跟踪文件总数包含 15 个非 Lua 文件。
- 非 Lua 文件包括 `scripts/.github/workflows/update.yml`、`controller.vdf`、13 个语言 `.po` 或 `.pot` 文件。
- 目录统计只用于分配阅读注意力，不替代 reference 覆盖清单。

## `0x10024111` 结构细节 / 数据结构与生命周期 / 具体 Lua 结构 / 需要核对的字段

| 范围 | Lua 文件数 | 阅读含义 |
| --- | ---: | --- |
| `scripts/` 根层 | 218 | 启动、全局服务和数据入口 |
| `scripts/prefabs/` | 1594 | 实体装配最大区 |
| `scripts/components/` | 821 | server 侧行为状态主区 |
| `scripts/stategraphs/` | 261 | 动作表现与动画状态机 |
| `scripts/brains/` | 191 | AI 决策入口 |
| `scripts/behaviours/` | 29 | behaviour tree 节点 |
| `scripts/map/` | 444 | worldgen、布局与世界定义 |
| `scripts/widgets/` | 272 | HUD 与 UI 组件 |
| `scripts/screens/` | 135 | 前端 screen |
| `scripts/scenarios/` | 50 | 场景脚本 |
| `scripts/util/` | 8 | 小型工具模块 |
| `scripts/languages/` | 2 | 语言加载工具 |
| `scripts/nis/` | 2 | 过场脚本 |
| `scripts/tools/` | 2 | 维护和导出工具 |
| `scripts/cameras/` | 1 | camera Lua 实现 |

## `0x10024211` 结构细节 / 重点阅读区 / 关联模块 / 验证点

- Carnival 高尔夫链路横跨 `recipes.lua`、`standardcomponents.lua`、`widgets/controls.lua` 和 `prefabs/carnivalgame_golf*.lua`。
- 物品温度链路集中在 `components/inventoryitemtemperature.lua`、`components/inventoryitem.lua` 和 `prefabs/desiccant.lua`。
- Fumarole 工具链也读取这组温度状态。
- Eets pet 链路横跨 `prefabs/critters.lua`、`components/crittertraits.lua`、`stategraphs/SGcritter_common.lua` 和 `stategraphs/SGcritter_eets.lua`。
- Vault 和 Scrapbook 链路横跨 `prefabs/vault_key_activator.lua`、`screens/redux/scrapbookdata.lua` 和 `debugcommands.lua`。
- `components/vaultroom.lua` 处理 Vault 虚拟房间卸载与 migration pet 保留边界。
- 皮肤与本地化链路包含 `walrushat_minigolf_*` 三个皮肤链路，并覆盖 WX-78 spoon lure 描述文本。

## `0x10025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
git ls-files --recurse-submodules scripts
git ls-files --recurse-submodules scripts | rg "\.lua$" | wc -l
git ls-files --recurse-submodules scripts/prefabs | rg "\.lua$" | wc -l
git ls-files --recurse-submodules scripts/components | rg "\.lua$" | wc -l
git -C scripts rev-parse --short HEAD
git -C scripts log -1 --format=%s
~~~

### `0x10025111` 推荐顺序 / 最小闭环

先复算总数和两个最大目录，再决定专题页是否应该改成解释型页面或 reference 清单页面。

如果 `git ls-files` 结果变化，必须同步更新 `0x8000-reference` 的完整覆盖清单。
