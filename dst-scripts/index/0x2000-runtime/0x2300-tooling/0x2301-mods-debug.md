# `0x23010000` 模组与调试

模组与调试页解释 mod 环境、post init 表、控制台命令和 debug 命令的边界。

本页不把调试命令当作 gameplay 主链路。

## `0x23011111` 本页定位 / 要回答的运行时问题 / Mod 注入点在哪里保存 / 验证点

`modutil.lua` 不直接改所有 prefab、component 或 stategraph。

post-init Add API 会把函数登记到 `env.postinitfns`。

部分 Add API 会直接修改全局注册表，例如 action、component action 或 tile 注册。

实际取出并调用这些函数的是 `ModManager:GetPostInitFns()` 的使用点。

## `0x23011211` 本页定位 / 调试命令的边界 / Console 与 Debugcommands / 验证点

`consolecommands.lua` 提供 `c_*` 控制台入口。

`debugcommands.lua` 提供大量 `d_*` 调试入口。

两者经常调用 `DebugSpawn`、`GetDebugEntity` 和 `SetDebugEntity`，但不代表普通运行时会走这些路径。

## `0x23012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/main.lua` | `KnownModIndex:Load` | 启动时加载 mod 索引 |
| `scripts/modindex.lua` | `KnownModIndex = ModIndex()` | 全局 mod 索引对象 |
| `scripts/mods.lua` | `modutil.InsertPostInitFunctions` | 给 mod env 注入 Add 系列 API |
| `scripts/mods.lua` | `ModManager:GetPostInitFns` | 运行时读取 post init 函数 |
| `scripts/modutil.lua` | `AddPrefabPostInit` | 登记 prefab post init |
| `scripts/modutil.lua` | `AddComponentPostInit` | 登记 component post init |
| `scripts/modutil.lua` | `AddStategraphPostInit` | 登记 stategraph post init |
| `scripts/debugtools.lua` | `debugstack` | 调试栈、表打印和 userdata instrumentation |
| `scripts/knownerrors.lua` | `known_assert` | 把已知错误 key 映射到用户可见错误 |
| `scripts/consolecommands.lua` | `c_spawn` | 控制台生成 prefab |
| `scripts/debugcommands.lua` | `d_*` | 调试命令集合 |
| `scripts/debughelpers.lua` | `debug.getinfo` | 调试辅助和 introspection |

### `0x23012111` Mod 索引锚点 / `scripts/main.lua` / 搜索信号

搜索 `KnownModIndex:Load` 可以看到启动阶段先加载 mod 索引。

随后 `BeginStartupSequence` 继续推进启动。

### `0x23012211` Post Init 锚点 / `scripts/modutil.lua` / 搜索信号

搜索 `env.postinitfns`。

post-init Add API 把函数插入这个表，而不是立刻执行。

不要把所有 Add API 都归入 `env.postinitfns`。

### `0x23012311` 调试锚点 / `scripts/consolecommands.lua` / 搜索信号

搜索 `function c_spawn`。

它会调用 `DebugSpawn(prefab)`，并在需要时调用 `SetDebugEntity(inst)`。

## `0x23013000` 运行流程

~~~mermaid
flowchart TD
    A["KnownModIndex:Load"]
    A --> B["ModManager loads mod env"]
    B --> C["InsertPostInitFunctions"]
    C --> D["env.postinitfns"]
    D --> E["ModManager:GetPostInitFns"]
    E --> F["prefab / component / stategraph hook point"]
~~~

### `0x23013111` Mod 环境阶段 / `InsertPostInitFunctions` / 边界条件

`mods.lua` 创建 mod env 后调用 `modutil.InsertPostInitFunctions(env, isworldgen, isfrontend)`。

所以同一个 API 集合会根据 worldgen 或 frontend 场景暴露不同能力。

### `0x23013211` Hook 执行阶段 / `ModManager:GetPostInitFns` / 边界条件

`mainfunctions.lua` 在 prefab 加载和生成时读取 prefab post init。

`stategraph.lua` 在 stategraph 构造时读取 stategraph post init。

`entityscript.lua` 的 `AddComponent` 在构造组件后读取 `ModManager:GetPostInitFns("ComponentPostInit", name)`。

随后它按顺序调用这些 component post init。

### `0x23013311` 调试执行阶段 / `DebugSpawn` / 边界条件

`c_spawn` 和很多 `d_*` 命令会修改世界。

这些入口是开发和控制台工具，不应作为普通玩家动作链路的证据。

`consolecommands.lua` 会在普通启动阶段加载。

`debugcommands.lua` 和 `debugkeys.lua` 只在 `CHEATS_ENABLED` 下加载。

## `0x23014111` 结构细节 / `env.postinitfns` / 分桶表 / 需要核对的字段

`PrefabPostInit`、`PrefabPostInitAny`、`ComponentPostInit` 和 `StategraphPostInit` 都是独立分桶。

worldgen、game、sim、shader、recipe、level、task set、task 和 room 也有独立 post-init 分桶。

指定名称的 post init 使用二级表保存。

any 类型 post init 使用数组保存。

## `0x23014211` 结构细节 / `KnownModIndex` / 全局对象 / 需要核对的字段

`KnownModIndex = ModIndex()` 位于 `modindex.lua` 文件末尾。

`mods.lua` 通过它读取 mod info、启用状态、配置和兼容性。

## `0x23014311` 结构细节 / Debug Helper / Introspection 与已知错误 / 需要核对的字段

`debughelpers.lua` 主要包装 `debug.getinfo`、upvalue 和实体 debug string。

`debugtools.lua` 提供 `debugstack`、`debugstack_oneline`、`dumptable` 和 `instrument_userdata`。

`knownerrors.lua` 提供 `ERRORS` 表和 `known_assert`，用于把固定错误 key 转换成已知错误信息。

真正大量改变世界的调试入口集中在 `consolecommands.lua` 和 `debugcommands.lua`。

## `0x23015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "KnownModIndex:Load|InsertPostInitFunctions|GetPostInitFns" \
  scripts/main.lua \
  scripts/mods.lua \
  scripts/modutil.lua \
  scripts/modindex.lua \
  scripts/mainfunctions.lua \
  scripts/stategraph.lua \
  scripts/debugtools.lua \
  scripts/knownerrors.lua \
  scripts/consolecommands.lua \
  scripts/debugcommands.lua \
  scripts/debughelpers.lua
rg -n "AddPrefabPostInit|AddComponentPostInit|AddStategraphPostInit|c_spawn|DebugSpawn|CHEATS_ENABLED" \
  scripts/main.lua \
  scripts/entityscript.lua \
  scripts/mods.lua \
  scripts/modutil.lua \
  scripts/modindex.lua \
  scripts/mainfunctions.lua \
  scripts/stategraph.lua \
  scripts/debugtools.lua \
  scripts/knownerrors.lua \
  scripts/consolecommands.lua \
  scripts/debugcommands.lua \
  scripts/debughelpers.lua
~~~

### `0x23015111` 推荐顺序 / 最小闭环

先读 `main.lua` 中 `KnownModIndex:Load`。

再读 `mods.lua` 如何创建 mod env。

最后读 `modutil.lua` 的 Add 系列 API 和 `ModManager:GetPostInitFns` 的调用点。
