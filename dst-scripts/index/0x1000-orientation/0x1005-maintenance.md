# `0x10050000` 维护规则

维护规则约束新增页面必须先说明运行链路，再放源码锚点，最后才放清单。

## `0x10051111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

维护者要优先核对源码事实、文档边界和 reference 清单的一致性。

维护文档时只改索引 Markdown，不能为了让文档成立去改 `scripts` Lua。

BBC 编码、标题层级和源码路径应保持简单、可读、可追踪。

## `0x10052000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `SaveGame` | 存档链路可审计 |
| `scripts/worldgen_main.lua` | `CheckMapSaveData` | 生成结果可校验 |
| `scripts/entityscript.lua` | `GetPersistData` | 实体状态可追踪 |
| `scripts/entityreplica.lua` | `ReplicateEntity` | client replica 可追踪 |

### `0x10052111` 主锚点 / `scripts/mainfunctions.lua` / 搜索信号

先在 `scripts/mainfunctions.lua` 搜索 `SaveGame`。

如果维护的是世界生成页面，再搜索 `CheckMapSaveData`。

如果维护的是实体或网络页面，再搜索 `GetPersistData`、`ReplicateEntity` 或 `ReplicateComponent`。

## `0x10053000` 运行流程

~~~mermaid
flowchart TD
    A["改文档"]
    A --> B["核对源码锚点"]
    B --> C["收敛专题边界"]
    C --> D["抽样关键链路"]
~~~

### `0x10053111` 流程分段 / 入口到副作用 / 边界条件

- 专题页解释运行关系，不承载大型完整目录。
- reference 页承载完整文件清单和目录型索引。
- 新增源码锚点必须真实存在于 `scripts`。
- 标题中的 BBC 编码必须保持 code span，不能裸写。

## `0x10054111` 结构细节 / 数据结构与生命周期 / 具体 Lua 结构 / 需要核对的字段

- 非 reference 专题页必须有清晰的 H2 起始结构，并按内容需要继续使用 H3/H4。
- 不为凑齐 H5 创建没有正文的中间标题。
- 源码路径必须指向真实文件。
- 完整覆盖清单只维护在 `0x8000-reference`。
- 每个非 reference 专题页至少保留源码锚点表、Mermaid 流程图和 `rg` 查询块。
- `rg` 查询块应优先搜索函数名、类名、表名或状态机名。

## `0x10055100` 阅读路线 / 从哪里开始读源码

~~~bash
rg -n "SaveGame|CheckMapSaveData|GetPersistData|ReplicateEntity" \
  scripts/mainfunctions.lua \
  scripts/worldgen_main.lua \
  scripts/entityscript.lua \
  scripts/entityreplica.lua
~~~

### `0x10055111` 推荐顺序 / 最小闭环

先从源码锚点确认事实，再回到专题边界清理重复叙述。
