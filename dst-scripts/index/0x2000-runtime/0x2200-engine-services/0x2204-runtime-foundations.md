# `0x22040000` 运行时基础设施

本页覆盖启动早期加载的 Lua 基础设施。

它解释 `class.lua`、`json.lua` 和 `constants.lua` 如何成为后续系统的共享契约。

## `0x22041111` 本页定位 / 要回答的运行时问题 / 为什么这些文件不是普通工具函数 / 验证点

`main.lua` 在启动早期加载 `json`、`constants` 和 `class`。

这些文件先于大部分 gameplay、UI、prefab 和 worldgen 模块进入运行时。

因此它们定义的是基础语言层和共享枚举，不是某个玩法系统的私有 helper。

## `0x22042000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/main.lua` | `require("json")` | 早期加载 JSON 编解码 |
| `scripts/main.lua` | `require("constants")` | 早期加载共享枚举和数值契约 |
| `scripts/main.lua` | `require("class")` | 早期加载 `Class` 构造器 |
| `scripts/class.lua` | `Class` | 构造类、继承链、property 和实例 metatable |
| `scripts/class.lua` | `ClassRegistry` | 记录类继承关系并支持清理 |
| `scripts/json.lua` | `json.encode` / `json.decode` | 非标准 JSON 编解码入口 |
| `scripts/json.lua` | `json.encode_compliant` | 标准 JSON 编码入口 |
| `scripts/constants.lua` | `RESET_ACTION` | reset 分流枚举 |
| `scripts/constants.lua` | `SAVELOAD` | 存读档状态枚举 |
| `scripts/constants.lua` | `REMOTESHARDSTATE` / `SHARDID` | shard 状态与身份枚举 |
| `scripts/constants.lua` | `SHARDTRANSACTIONTYPES` | 跨 shard transaction 类型 |

## `0x22043000` 运行流程

~~~mermaid
flowchart TD
    A["main.lua startup"]
    A --> B["require json"]
    A --> C["require constants"]
    A --> D["require class"]
    B --> E["data encode/decode helpers"]
    C --> F["shared runtime enums"]
    D --> G["Class(base, ctor, props)"]
    G --> H["components / widgets / managers"]
    F --> I["gamelogic / networking / shard paths"]
~~~

### `0x22043111` `Class` 基础层 / 构造与继承 / 边界条件

`Class(base, _ctor, props)` 会构造 metatable、实例 `__index` 和可选 property setter。

它也会把继承关系写入 `ClassRegistry`。

`__tostring`、`__call` 和只读 property 语义都在这里形成。

不要把组件、widget 或 manager 的 `Class(...)` 当作普通 table 工厂。

### `0x22043211` JSON 基础层 / 编码口径 / 边界条件

`json.encode` 是源码常用入口。

`json.encode_compliant` 才是显式标准 JSON 编码入口。

`json.decode` 与 `json.null` 共同处理反序列化和空值表达。

阅读存档、配置、服务器数据和外部文本工具时，应先确认调用的是哪一种编码口径。

### `0x22043311` 常量基础层 / 共享枚举 / 边界条件

`constants.lua` 不是只给一个系统使用。

`RESET_ACTION` 影响 `gamelogic.lua` 的 reset 分流。

`SAVELOAD` 影响存读档状态表达。

`REMOTESHARDSTATE`、`SHARDID` 和 `SHARDTRANSACTIONTYPES` 影响 shard 网络路径。

如果这些枚举改动，文档应同步核对运行时、网络和世界状态专题。

## `0x22044100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "require\\(\"json\"\\)|require\\(\"constants\"\\)|require\\(\"class\"\\)" \
  scripts/main.lua

rg -n "function Class|ClassRegistry|property" \
  scripts/class.lua

rg -n "json\\.encode|json\\.decode|encode_compliant|null" \
  scripts/json.lua

rg -n "RESET_ACTION|SAVELOAD|REMOTESHARDSTATE|SHARDID|SHARDTRANSACTIONTYPES" \
  scripts/constants.lua
~~~

### `0x22044111` 推荐顺序 / 最小闭环

先读 `main.lua` 的加载顺序。

再读 `class.lua`，确认 `Class` 如何定义后续对象。

最后按问题域回到 `json.lua` 或 `constants.lua`。
