# Fieldkit 调研与开发计划

本轮调研覆盖 Fieldkit 客户端模组及 [参考清单](fieldkit-workshop-links.txt) 中的 21 个模组，核对日期为 2026-10-29。
建议首轮开发聚焦连续作业的动作节奏和远端结果确认，随后完善任务反馈与改键体验。
现有功能已经覆盖多数参考模组的主要使用场景，下一步的重点是把这些工作流验收到可以稳定交付。
2026-10-30 已执行首轮开发，交付范围、验证结果与剩余验收见下方执行记录。

基线采用当前工作区的 Fieldkit 0.15.0，包含尚未提交的实现。
游戏脚本快照为 `758848`，对应子模块提交 `e5d46e0c`。
现有 [使用说明](../mods/fieldkit/README.md) 和源码已经包含目标阶段筛选、连续投入、自身与玩家动作、工具切换、树桩及替换实体续接等能力。
这些功能进入回归验收清单。

本轮已运行 `tests/rust/check_client_mod.py`。
Lua 5.1 和 LuaJIT 各通过 42 个功能测试文件及 110 个 Lua 文件语法检查。
该结果覆盖当前自动化基线；图形客户端、手柄、真实多人网络和作业速度仍需实机验收。

版本核对发现两项参考快照已经落后。
下表的线上版本取自本轮打开的作者页面；其余参考模组的版本仍以本地快照为准。

| 参考模组 | 本地版本 | 作者页面版本 | 对计划的影响 |
| --- | --- | --- | --- |
| [ActionQueue RB3][aq] | 4.3 | 4.4 | 优先核对连续工作节奏。 |
| [群鸟绘卷春篇][spring] | 11.8 | 12.1 | 重新核对新增能力后再确定扩展需求。 |
| [群鸟绘卷夏篇][summer] | 11.33 | 11.33 | 继续作为交互及功能组合参考。 |
| [Item Scroller][scroller] | 0.22.0 | 0.22.0 | 对照游戏内改键与库存入口。 |
| [Auto Walking][walking] | 3.8.2 | 3.8.2 | 对照地图输入、路线追加和复杂障碍表现。 |
| [Ground Chest][ground] | 1.3.62 | 1.3.62 | 对照寻物筛选及列表使用体验。 |

ActionQueue 在 10 月 19 日的 [更新记录][aq-changes] 中明确修复了海狸形态未获得工作速度加成的问题。
调研基线中的 [作业执行代码](../mods/fieldkit/scripts/fieldkit/features/work.lua) 等待角色进入空闲或移动状态，再次派发工作动作至少间隔 0.3 秒；策略可另外声明就绪条件。
这构成优先验证作业速度的依据，尚不足以断言 Fieldkit 存在相同缺陷。
现有 `working_animation_owns_input` 测试保护正在进行的动作，修改就绪条件时需要保留这项行为。

21 个参考模组按下面的使用场景对应到现有实现。
"已有能力"表示本地代码中存在相应路径，各项体验的完整覆盖仍以验收结果为准。

| 使用场景与参考 | Fieldkit 已有能力与入口 | 下一步关注点 |
| --- | --- | --- |
| 批量作业：[ActionQueue RB3][aq] | [作业队列](../mods/fieldkit/scripts/fieldkit/features/work.lua)、[动作策略](../mods/fieldkit/scripts/fieldkit/rules/work.lua)、阶段筛选、自动工具、掉落收集、连续投入及续接。 | 角色能力是否影响动作节奏；远端动作完成与目标变化能否区分。 |
| 几何摆放：[Geometric Placement][placement] | [放置规则](../mods/fieldkit/scripts/fieldkit/game/placement.lua)、[批量施工](../mods/fieldkit/scripts/fieldkit/features/construction.lua)、原生格点、种植排列、船上坐标。 | 预览和实际落点、岸线与锚点、船体移动时的结果一致性。 |
| 导航与跟随：[Auto Walking][walking]、[Keep Following][following]、[Minimap HUD][minimap] | [移动任务](../mods/fieldkit/scripts/fieldkit/features/travel.lua)、大小地图航点、道路偏好、避障、跟随、推挤及航行。 | 复杂路线耗时、地图原生动作冲突、丢失目标及船体运动。 |
| 寻物与搬运：[Ground Chest][ground]、[Item Scroller][scroller] | [地面搜索](../mods/fieldkit/scripts/fieldkit/features/ground.lua)、[库存操作](../mods/fieldkit/scripts/fieldkit/features/storage.lua)、皮肤分组、同船筛选、定量移料。 | 关闭容器、库存满、连续输入、手柄焦点及游戏内改键。 |
| 烹饪：[Auto Cooking][cooking]、[Crockpot Repeater][repeater] | [烹饪任务](../mods/fieldkit/scripts/fieldkit/features/cooking.lua)、配方记忆、批量开锅、库存取料、原生窗口重复按钮。 | 多锅、多容器、调味站、容量不足与中途取消。 |
| 状态与骑乘：[Combined Status][status]、[Nightmare phase indicator][nightmare]、[Beefalo Buck Timer][beefalo] | [角色与世界信息](../mods/fieldkit/scripts/fieldkit/game/information.lua)、状态数字、梦魇阶段、骑乘估算及观察记录。 | 估算与未知状态的表达、上牛观测完整性、回档和重连。 |
| 范围与掉落：[Range Indicator][ranges]、[Boss Indicators][bosses]、[Shiny Loot][loot] | [范围和方向提示](../mods/fieldkit/scripts/fieldkit/features/awareness.lua)、[掉落提示](../mods/fieldkit/scripts/fieldkit/features/loot.lua)、自定范围、固定目标及已观察位置。 | 新物件的数据覆盖，离屏信息的时效和密集场景开销。 |
| 名称与感官：[Show Nickname][names]、[滤镜 RR][filters] | [名称显示](../mods/fieldkit/scripts/fieldkit/features/presentation.lua)、[视觉调整](../mods/fieldkit/scripts/fieldkit/features/visual.lua) 及独立感官选项。 | 不同分辨率、角色变化、HUD 重建和恢复设置。 |
| 宣告与表情：[快捷宣告 NoMu][announce]、[Gesture Wheel][gestures] | [就地宣告](../mods/fieldkit/scripts/fieldkit/features/announcements.lua)、[表情入口](../mods/fieldkit/scripts/fieldkit/features/emotes.lua)、常用八格和手柄操作。 | 文本输入焦点、频道选择、常用位保存及输入冲突。 |
| 功能组合：[群鸟绘卷春篇][spring]、[夏篇][summer] | [自动维修](../mods/fieldkit/scripts/fieldkit/features/repair.lua)、海钓、晾晒、计时、虫洞记录等已有独立模块。 | 一键整理、箱子内容高亮、打包内容记忆作为待评估候选。 |

调研基线中能够直接确认的体验差距有两项。
[HUD](../mods/fieldkit/scripts/fieldkit/ui/hud.lua) 主要展示任务名称；作业完成后才汇总成功和跳过数量，运行中的进度与跳过原因可以更明确。
[快捷键配置](../mods/fieldkit/scripts/fieldkit/core/config.lua) 已有重复键保护，但会关闭后遇到的重复绑定；主要快捷键来自模组配置，缺少游戏内修改和冲突说明入口。
Item Scroller 的作者页面明确提供游戏内控制设置改键，可作为交互参考。

后续开发按以下顺序推进。
每一步完成后以验收结果决定下一步的具体改动范围。

| 顺序 | 工作项 | 主要落点 | 完成条件 |
| --- | --- | --- | --- |
| P0.1 | 对齐目标版本并测量连续作业节奏。 | 参考快照、`game/actions.lua`、`features/work.lua`、`rules/work.lua`。 | 取得 AQ 4.4 源码差异；记录目标游戏版本；测得原生操作与 Fieldkit 的耗时、动作次数和额外等待来源。 |
| P0.2 | 修复测量或复现确认的问题，验证远端结果确认。 | 作业、库存、装备、施工和晾晒模块中对应的问题路径。 | 相同操作得到相同结果与耗料；同步延迟、工具耗尽、目标变化和取消均有明确结束状态。 |
| P1.1 | 完善任务反馈。 | 先从 `features/work.lua` 和 `ui/hud.lua` 开始。 | 运行中可见已完成、剩余和跳过数量；结束时能说明目标变化、工具不足、无法到达或结果未确认。 |
| P1.2 | 完善改键和冲突提示。 | `core/config.lua`、`application.lua` 及对应输入入口。 | 用户能查看有效绑定、发现冲突并调整；停止操作始终可达；聊天和文本编辑不会误触发。 |
| P2 | 评估并选择一个新的高频库存工作流。 | `game/inventory.lua`、`features/storage.lua` 或有明确需要的记录模块。 | 单独明确输入、操作范围、取消后物品位置和成功证据，再进入开发。 |

P0.1 先测量普通角色、伍迪人形、海狸形态和 WX-78 工作能力相关场景。
分别记录本地主机与远端客户端、延迟补偿开关下的表现，并保留原生手动连续作业作为比较基准。
需要区分行走时间、动作前摇、动作执行和客户端额外等待。
若确认存在多余等待，应根据原生状态图的可续接条件调整对应动作策略，同时保证尚未完成的动作仍持有输入控制。

P0.2 的首批验收场景包括连续砍采、工具耗尽后切换、树桩及替换实体续接、连续水肥、堆肥桶和多槽晾晒架。
库存相关场景还应覆盖移动中的容器关闭、材料延迟同步、其他玩家改变目标，以及执行到一半手动取消。
优先复用现有测试，新增测试应对应本轮发现的缺口或可复现问题。
真实客户端负责验证动作节奏、交互焦点和网络表现。

P1.1 可先使用作业对象已有的计数，补充少量明确的跳过原因，再由现有 HUD 刷新逻辑读取。
P1.2 先验证目标游戏版本的控制设置接入方式，保留现有输入作用域和清理机制。
这些改动继续沿用现有 `core`、`game`、`rules`、`features`、`ui` 的职责划分。
任务互斥、取消和时间上限由 [执行器](../mods/fieldkit/scripts/fieldkit/core/executor.lua) 统一管理，动作选择与派发继续经过原生接口。

P2 的候选按以下依赖关系评估。
一键整理可以建立在已确认的库存移动之上，先限定为自身库存和背包，并明确装备、光标物品和固定槽位的处理方式。
箱子内容高亮需要明确哪些内容来自当前打开的容器，哪些只是上次观察记录。
打包内容记忆涉及内容归属、包裹身份、拆包和跨世界记录，适合在持久化规则明确后再排期。
这些候选来自群鸟绘卷的作者功能说明；其页面对代码再分发设有限制，具体实现应依据原生接口和 Fieldkit 现有模块设计。

调研结束时，AQ 4.4 和群鸟绘卷春篇 12.1 的源码差异、实际验收客户端的游戏版本仍待补齐。
线上核对完成的是作者页面和更新说明，最新源码快照尚未更新。
首轮交付物应包含版本记录、已确认问题的修复、对应自动回归、客户端验收结果，以及与验收结果一致的安装包和说明。

## 2026-10-30 执行记录

本轮交付 Fieldkit **0.16.0**，继续以游戏脚本快照 `758848` 为原生行为依据。
执行环境无法运行图形客户端，使用原生 Lua 函数和状态图做自动回归；实际速度、手柄显示和真实多人网络表现仍待实机验收。
ActionQueue 的更新记录及本地快照参数用于确定检查方向，没有复制其实现。
尝试通过 SteamCMD 下载 AQ 4.4 时，进程在当前环境因 `Bad system call` 退出，最新源码未取得，不能宣称已完成版本源码差异核对。

| 计划项 | 已交付内容 | 验证与边界 |
| --- | --- | --- |
| P0.1 连续作业节奏 | 提取 `game/action_state.lua`；确认上一击后，按原生续接保护决定下一击，使用执行器已有的 0.1 秒检查间隔。 | 测试直接加载主机和客户端动作处理函数，覆盖普通角色、伐木能力、海狸和旋转模块；尚无实机耗时对比。 |
| P0.2 结果确认 | 保留派发、执行证据和耗料结果的区分；修复自动维修在工作动画期间可能抢占输入的问题，并在实际派发前再次检查。 | 全量回归覆盖库存与装备延迟、工具耗尽、连续投入、实体续接、晾晒和取消；真实网络抖动未实测。 |
| P1.1 任务反馈 | 交互、施工和库存队列显示进度；记录跳过原因；保留最近一次结束、失败、取消或超时结果。 | 复用各功能已有计数，快照不持有实体；校验追加、取消清理与目标变化后的结果。 |
| P1.2 改键与冲突 | 工具箱可修改七个快捷键、取消绑定、查看内部冲突并恢复模组配置；覆盖值保存到本地。 | 复用 `modinfo.lua` 候选值；测试资料延迟加载、非法值、冲突、聊天焦点、按住旧键和作用域释放。 |
| P2 库存工作流选型 | 选择自身物品栏与背包整理，明确下面的范围与回执要求。 | 本轮完成评估；整理功能尚未实现。 |

海狸的原生 `gnaw` 状态在第 6 帧执行动作，第 7 帧解除 `gnawing`，并在第 8 帧处理原生连续操作。
回归测试在原生第 7 帧回调解除保护后验证 Fieldkit 可以续接，同时验证缺少下一击结果时不会重复派发。
这些是脚本时点与自动化检查，不代表真实客户端的每秒动作数或性能提升比例。
原有 `working_animation_owns_input` 测试继续保护没有明确续接条件的工作动画。

结构检查覆盖当前 65 个 Fieldkit 模块的直接依赖，没有发现循环引用。
新增共享模块分别承担动作就绪、进度快照和快捷键生命周期；调用方复用现有执行器、作用域、存储及原生回执。
地图覆盖层与范围渲染器改由 `application.lua` 组装传入，`core`、`game`、`rules` 不再直接引用 UI 或功能编排模块。
不同功能的完成证据仍在对应工作流内处理，避免把库存计数、动作回显和制造结果混成同一个判断。

## P2 选型：自身物品栏与背包整理

优先选择整理，因为输入和作用范围可由当前库存直接确定，已有 `take`、`put`、`move` 与数量回执可复用。
箱子内容高亮需要额外区分正在读取与历史缓存，打包内容记忆需要新的包裹身份和持久化规则，依赖更多。

| 项目 | 首版约定 |
| --- | --- |
| 输入 | 工具箱中的一次明确启动；开始时鼠标为空，库存可读且没有同步中的请求。 |
| 范围 | 自身主物品栏和当前背包分别整理；装备槽、锁定槽、特殊专用槽、嵌套容器和外部箱子保持原位。 |
| 排序 | 按物品类型、皮肤和原槽位稳定排列；首版只移动整堆，保留数量、耐久和物品身份。 |
| 满背包 | 用光标作为临时位置，通过原生 `SwapActiveItemWithSlot` 完成置换；不能依赖一定存在空槽。 |
| 取消 | 停止追加请求，保留已确认的位置；光标中若仍有物品，明确提示玩家手动放回，不自动丢弃或猜测回滚。 |
| 成功证据 | 每次操作等待两端稳定的物品身份和数量，再验证目标槽位顺序及空光标；客户端预测不算成功。 |
| 失败条件 | 容器关闭、背包替换、槽位限制改变、身份或数量异常、超时都结束整理并报告当前位置。 |

核对原生实现发现，交换接口会先更新客户端预测槽位；服务端还会拒绝可合堆物品之间的直接交换。
现有 `game/inventory.lua` 尚无交换两端身份的回执，因此不能直接把排序结果交给一串交换请求。
后续实现应先补充这个原生适配及主机、预测客户端、无预测客户端的延迟与取消回归，再接入 `features/storage.lua` 和已有进度展示。

## 本轮验证与交付

`tests/rust/check_client_mod.py` 在 Lua 5.1 和 LuaJIT 下各通过 **44 个功能测试文件、115 个 Lua 文件语法检查**。
新增回归直接使用原生动作处理器和键盘事件路由，并覆盖维修派发前发生的输入变化。
安装包位于 `dist/fieldkit.zip`，使用方法和当前行为见 [Fieldkit 说明](../mods/fieldkit/README.md)。
实机速度、手柄布局、真实多人网络，以及最新参考模组源码差异仍是未完成的验收项。

[aq]: https://steamcommunity.com/sharedfiles/filedetails/?id=2873533916
[aq-changes]: https://steamcommunity.com/sharedfiles/filedetails/changelog/2873533916
[spring]: https://steamcommunity.com/sharedfiles/filedetails/?id=3161117403
[summer]: https://steamcommunity.com/sharedfiles/filedetails/?id=3161119380
[scroller]: https://steamcommunity.com/sharedfiles/filedetails/?id=3753606785
[walking]: https://steamcommunity.com/sharedfiles/filedetails/?id=2849308125
[ground]: https://steamcommunity.com/sharedfiles/filedetails/?id=2480826828
[placement]: https://steamcommunity.com/sharedfiles/filedetails/?id=351325790
[following]: https://steamcommunity.com/sharedfiles/filedetails/?id=1835465557
[minimap]: https://steamcommunity.com/sharedfiles/filedetails/?id=345692228
[cooking]: https://steamcommunity.com/sharedfiles/filedetails/?id=2033458869
[repeater]: https://steamcommunity.com/sharedfiles/filedetails/?id=2336105249
[status]: https://steamcommunity.com/sharedfiles/filedetails/?id=376333686
[nightmare]: https://steamcommunity.com/sharedfiles/filedetails/?id=1298780476
[beefalo]: https://steamcommunity.com/sharedfiles/filedetails/?id=2858947939
[ranges]: https://steamcommunity.com/sharedfiles/filedetails/?id=3066199391
[bosses]: https://steamcommunity.com/sharedfiles/filedetails/?id=1120124958
[loot]: https://steamcommunity.com/sharedfiles/filedetails/?id=3371086303
[names]: https://steamcommunity.com/sharedfiles/filedetails/?id=2917322514
[filters]: https://steamcommunity.com/sharedfiles/filedetails/?id=3115280970
[announce]: https://steamcommunity.com/sharedfiles/filedetails/?id=2784715091
[gestures]: https://steamcommunity.com/sharedfiles/filedetails/?id=352373173
