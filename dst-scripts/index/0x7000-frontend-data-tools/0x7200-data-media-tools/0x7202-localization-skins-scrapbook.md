# `0x72020000` 本地化皮肤与 Scrapbook

本页把文本、本地化、皮肤资产和 Scrapbook 分成三条数据展示链路。
它们都服务展示层，但皮肤会继续影响 prefab 的 build、image 和 sound 选择。
Scrapbook 的条目数据是生成快照，seen/viewed/inspected 状态是运行时资料。

## `0x72021111` 本页定位 / 要回答的运行时问题 / 哪些表是静态，哪些表会被翻译或生成 / 验证点

`STRINGS` 是基础文本表。
`TranslateStringTable(STRINGS)` 会递归替换文本。
`skin_assets` 是资产列表，`prefabskins.lua` 是 prefab 到 skin 名称的映射。
`prefabskin.lua` 才是把 skin 应用到实体外观与部分音效的运行时工具层。
`scrapbookdata.lua` 是由 `d_createscrapbookdata()` 生成的数据快照。

## `0x72021211` 本页定位 / 展示数据与 Gameplay 状态 / 只读展示不等于权威状态 / 边界条件

Scrapbook screen 会读取 `TheScrapbookPartitions` 的 seen/viewed 状态。
但条目的展示数据来自 `screens/redux/scrapbookdata.lua`。

## `0x72022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/strings.lua` | `STRINGS` | 基础文本表 |
| `scripts/translator.lua` | `Translator` | PO 文件加载和字符串查找 |
| `scripts/translator.lua` | `TranslateStringTable` | 递归翻译文本表 |
| `scripts/main.lua` | `TranslateStringTable(STRINGS)` | 启动阶段应用翻译 |
| `scripts/skin_assets.lua` | `skin_assets` | 皮肤动态资产列表 |
| `scripts/skin_strings.lua` | skin strings | 皮肤名称与描述文本 |
| `scripts/prefabskins.lua` | `PREFAB_SKINS` | prefab 到 skin 列表映射 |
| `scripts/prefabskin.lua` | `SetSkinMode` / helpers | skin 应用到实体外观 |
| `scripts/skinsutils.lua` | skins helpers | 皮肤查询与展示辅助 |
| `scripts/skins_defs_data.lua` | skin item data | 皮肤物品元数据 |
| `scripts/scrapbook_prefabs.lua` | `PREFABS` | 参与 Scrapbook 的 prefab 白名单 |
| `scripts/screens/redux/scrapbookdata.lua` | generated table | Scrapbook 条目数据 |
| `scripts/screens/redux/scrapbookscreen.lua` | `ScrapbookScreen` | 前端图鉴 screen |
| `scripts/scrapbookpartitions.lua` | `TheScrapbookPartitions` | seen/viewed 分区状态 |

### `0x72022111` 主锚点 / `scripts/translator.lua` / 搜索信号

先搜 `function TranslateStringTable`。
再搜 `local function DoTranslateStringTable`，确认它递归遍历文本表 path。

### `0x72022211` Scrapbook 锚点 / `scripts/screens/redux/scrapbookscreen.lua` / 验证点

`ScrapbookScreen` 顶部会 `require("screens/redux/scrapbookdata")`。
`SetPlayerKnowledge()` 会用 `TheScrapbookPartitions:GetLevelFor(prefab)` 给每个条目写入 `knownlevel`。

## `0x72023000` 运行流程

~~~mermaid
flowchart TD
    A["strings.lua STRINGS"]
    B["translator.lua LanguageTranslator"]
    A --> C["main.lua TranslateStringTable( STRINGS )"]
    B --> C
    C --> D["screens and widgets read STRINGS"]
    E["skin_assets / skin_strings / skins_defs_data"]
    F["prefabskins maps prefab to skins"]
    G["skinsutils queries ownership/display"]
    H["prefabskin applies build/image/sound"]
    E --> G
    F --> G
    G --> H
    I["scrapbook_prefabs whitelist"]
    I --> J["debugcommands d_createscrapbookdata"]
    J --> K["screens/redux/scrapbookdata.lua"]
    K --> L["ScrapbookScreen dataset"]
    M["TheScrapbookPartitions storage"]
    M --> L
~~~

### `0x72023111` 文本翻译阶段 / `Translator:LoadPOFile` / 验证点

`LoadPOFile` 支持 reference 字段和 `msgctxt` 字段。
翻译结果存入 `self.languages[lang]`。

### `0x72023211` 皮肤展示阶段 / `skin_assets` / 验证点

`skin_assets.lua` 返回 `Asset("DYNAMIC_ANIM", ...)` 与 `Asset("PKGREF", ...)` 这类动态资产列表。
实际展示还要结合 `skin_strings.lua`、`skins_defs_data.lua`、`prefabskins.lua` 和 `skinsutils.lua`。
实体换肤则继续进入 `prefabskin.lua`，例如设置 inventory image 或覆盖动画 symbol。
皮肤列表包含 `walrushat_minigolf_green`、`walrushat_minigolf_purple` 和 `walrushat_minigolf_red`。
这三个皮肤由对应的 `tophat_minigolf_*` granted item 链路发放，并通过 `AddSkinLinkInput` 绑定。
它们也加入了 `ITEM_DISPLAY_BLACKLIST`，避免以普通展示物品形式出现。

### `0x72023311` Scrapbook 展示阶段 / `scrapbookdata.lua` / 验证点

文件头注明它由 `d_createscrapbookdata()` 生成。
因此它更像可审计数据快照，而不是手写 gameplay 逻辑。
运行时解锁、已读和角色检查状态不写在这个快照里，而是从 `scrapbookpartitions.lua` 读取。
Scrapbook 数据记录 `chesspiece_vault_pillar_guard` build、golden pickaxe finiteuses、Vault 物件动画名和 Vault 依赖。
`prefabs/vault_key_activator.lua` 设置 `scrapbook_anim`，作为生成数据的动画来源。

### `0x72023411` 文本展示阶段 / WX-78 钓具描述 / 验证点

`speech_wx78.lua` 包含 spoon lure 的蓝色和绿色描述。
`languages/strings.pot`、`languages/chinese_s.po` 和 `languages/chinese_t.po` 提供对应 msgid。
因此排查文本时不能只看角色 speech 文件，也要看翻译模板中的对应 msgid。

## `0x72024111` 结构细节 / `TranslateStringTable` / Path 作为翻译 Key / 需要核对的字段

`DoTranslateStringTable` 用递归 path 构造唯一 key。
翻译时会优先查 `LanguageTranslator:GetTranslatedString(path)`。

## `0x72024211` 结构细节 / `ScrapbookScreen` / 数据过滤与 Known Level / 需要核对的字段

`SetPlayerKnowledge` 会给数据条目写入 `knownlevel`。
`FilterData` 和 `BuildItemGrid` 再根据文本、类别和已知状态过滤展示。

## `0x72024311` 结构细节 / 皮肤边界 / 资产、文本和拥有状态 / 边界条件

皮肤资产、文本和 `PREFAB_SKINS` 只说明“存在什么皮肤”。
玩家是否拥有、是否可选和是否受活动限制，要继续追 `TheInventory:CheckOwnership`、collection UI 和 `skinsutils.lua`。
实体最终显示异常时，应继续查 `prefabskin.lua` 中对应 prefab 的 override 分支。

## `0x72025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "STRINGS =|TranslateStringTable\\( STRINGS \\)|function TranslateStringTable" \
  scripts/strings.lua scripts/main.lua scripts/translator.lua

rg -n "local skin_assets|return skin_assets|PREFAB_SKINS|GetSkinData|ChangeImageName|OverrideItemSkinSymbol" \
  scripts/skin_assets.lua \
  scripts/prefabskins.lua \
  scripts/prefabskin.lua \
  scripts/skinsutils.lua

rg -n "walrushat_minigolf|AddSkinLinkInput|ITEM_DISPLAY_BLACKLIST" \
  scripts/prefabs/skinprefabs.lua \
  scripts/prefabskins.lua \
  scripts/skin_assets.lua \
  scripts/skin_strings.lua \
  scripts/skins_defs_data.lua \
  scripts/item_blacklist.lua

rg -n "d_createscrapbookdata|scrapbookdata|TheScrapbookPartitions|SetPlayerKnowledge" \
  scripts/debugcommands.lua \
  scripts/screens/redux/scrapbookdata.lua \
  scripts/screens/redux/scrapbookscreen.lua

rg -n "vault_key_activator|scrapbook_anim|goldenpickaxe|chesspiece_vault_pillar_guard|OCEANFISHINGLURE_SPOON" \
  scripts/prefabs/vault_key_activator.lua \
  scripts/screens/redux/scrapbookdata.lua \
  scripts/speech_wx78.lua \
  scripts/languages/strings.pot
~~~

### `0x72025111` 推荐顺序 / 最小闭环

先抽样 `STRINGS.UI.SCRAPBOOK.TITLE`。
确认它在 `strings.lua` 定义，经 `TranslateStringTable` 处理，然后被 `ScrapbookScreen` 使用。
再抽样一个 scrapbook 条目，确认展示数据来自 `scrapbookdata.lua`。
最后抽样一个皮肤 prefab，从 `prefabskins.lua` 找 skin 名称，再到 `prefabskin.lua` 看实际应用。
