# Mod 管理

集群在启动游戏分片前准备共享 Mod，默认使用 DST 原生更新器。
安装项目见 [README](../README.md)，目录和启用选项见[配置指南](configuration.md)。

## 选择更新器

| `DST_SERVER_MOD_UPDATER` | 行为 | 适用场景 |
| --- | --- | --- |
| `native`（默认） | 执行游戏的 `-only_update_server_mods`，检查明确的下载结果。 | 沿用游戏原生下载流程。 |
| `steamcmd` | 用 SteamCMD 下载并安装，准备过程不启动游戏二进制。 | 下载列表由静态项目或合集声明组成。 |

在运行集群服务的进程环境中设置：

```sh
export DST_SERVER_MOD_UPDATER=steamcmd
export DST_SERVER_STEAMCMD=/path/to/steamcmd.sh
```

使用 Quadlet 时，将变量写入 `.container` 的 `[Container]`，例如 `Environment=DST_SERVER_MOD_UPDATER=steamcmd`，再重新加载并重启服务。
宿主 shell 中的 `export` 不会修改已有容器的环境。
将示例路径替换为实际可执行文件；省略 `DST_SERVER_STEAMCMD` 时，先使用 `STEAMCMDDIR/steamcmd.sh`，再使用 `PATH` 中的 `steamcmd`。
已指定但不存在或不可执行的路径会报错，不会继续尝试其他来源。
两条路径每次更新默认最多尝试 5 次，重试共享 30 分钟总期限。
原生更新器即使退出码为零也可能下载失败，因此还会检查完成标记和错误日志。
重试复用下载缓存，不改变游戏内部的 Workshop 等待计时器。

## 声明下载与启用选项

共享下载清单位于 `mods/dedicated_server_mods_setup.lua`。
容器与配置 SDK 统一使用双引号直接调用：

```lua
ServerModSetup("1803285852")
-- ServerModCollectionSetup("1234567890") -- 替换为实际合集 ID 后取消注释。
```

容器与 `ClusterController` 先用配置 SDK 读取 setup，两个后端都只接受声明式 Lua、双引号 ID 和至多一个末尾 return 值。
独立 SteamCMD 准备路径也只提取静态字符串调用，拒绝变量、循环、条件和计算表达式。
直接调用低层原生 `mods.update()` 时，setup 脚本交由游戏执行；切换集群后端不会放宽配置 SDK 的限制。
准备阶段还会把各分片 `modoverrides.lua` 中显式启用的 Workshop 项目补入共享清单。
下载完成不会自动启用 Mod，分片的启用状态和配置仍由 `modoverrides.lua` 决定。
`modinfo.lua`、`modmain.lua` 及其他 Mod Lua 由游戏执行，Python 安装器不读取 Lua 版本字段来判断更新。

## 启动与手动更新

整服首次启动、`stop()` 后的 `start()` 会检查更新，`restart()` 会先停完全部游戏进程再更新。
手动刷新使用 `stop()` → `update_mods()` → `start()`，最后一步复用已经成功完成的准备结果。
`update_mods()` 要求全部分片 agent 已连接，且所有游戏进程均已停止。
接管现有运行进程和恢复单个分片时复用已安装内容，不更新正在使用的共享 Mod。
停止或更新失败会阻止本次重启继续启动游戏，下一次启动会重新准备尚未成功的更新。
停止和重启不会隐式保存游戏，需要新快照时先保存；调用方式见[运行控制](runtime.md)。

## 独立 Workshop SDK

安装本项目并确保 `steamcmd` 位于 `PATH` 后，可在 Linux 上独立下载；运行前先停止使用目标 `mods` 目录的游戏进程：

```python
import asyncio
from pathlib import Path

from dst_server.steamcmd import SteamCMD
from dst_server.workshop import WorkshopUpdater


async def main() -> None:
    updater = WorkshopUpdater(SteamCMD("steamcmd"), Path("mods").resolve())
    installed = await updater.update([1803285852, 466732225], attempts=5)
    print(installed)


asyncio.run(main())
```

返回值是已完成安装的 ID 排序元组，上例为 `(466732225, 1803285852)`。
传入 `collections=[合集数字ID]` 可递归展开合集并去重，使用的 [GetCollectionDetails][webapi] 接口不要求 API key。

## 文件格式与失败处理

SteamCMD 缓存位于 `mods/ugc/steamcmd`，ACF、manifest 和更新状态均由 SteamCMD 管理，Python 不另存安装 revision 数据库。

| SteamCMD 产物 | SDK 安装方式 |
| --- | --- |
| legacy 文件，常见后缀为 `_legacy.bin`，内容实际为 ZIP | 校验并解压到 `mods/workshop-<ID>/`。 |
| UGC 内容目录 | 完整复制到 `mods/workshop-<ID>/`，替换旧目录中的全部内容。 |

格式取决于实际产物，`workshop-` 只是安装目录前缀；Valve 也[区分 legacy 文件与内容目录][ugc-install]。
SDK 只安装 SteamCMD 明确确认完成的项目，并拒绝越界路径、符号链接和缺少 `modinfo.lua` 的内容。
每项先在暂存目录准备，成功后切换安装目录；该项准备失败时保留旧安装。
安装按条目提交，后续条目失败不会回退已经完成的条目。
目录切换被强制中断后，下次调用会在联网前恢复尚未发布成功的旧目录。
更新期间持有目录独占锁；取消会清理下载进程，并等待正在进行的文件安装结束后释放锁。

## 外部下载方案取舍

| 方案 | 能力与取舍 |
| --- | --- |
| [SteamCMD][steamcmd] | 本项目外部更新器首选，复用已有下载和进程管理，已验证 DST 两种产物。 |
| [Python Steam 客户端][python-steam] | 可访问 Steam 内容协议，但该 Workshop 入口面向 SteamPipe，legacy 安装和异步集成仍需补齐。 |
| [SteamKit2][steamkit] / [DepotDownloader][depot] | 分别提供 .NET 协议库和下载 CLI，需要新增工具及 DST 安装集成。 |
| [Steamworks API][ugc-download] + ctypes | 可由匿名游戏服务器下载，但需自行维护原生库、ABI 和回调生命周期。 |
| 仅 [Web API][webapi] + HTTP | 适合详情查询和合集展开，不能单独承担所有 Workshop 内容格式的下载。 |

真实验证涵盖 legacy `466732225` 和 UGC `1803285852` 的匿名下载、缓存复用及离线游戏加载，不代表任意 Mod 或在线状态都兼容。
回归边界见 [SDK 测试](../tests/test_workshop.py)、[原生更新测试](../tests/test_native_mod_update.py)和[启动控制测试](../tests/test_controller.py)。
日志和运行状态的观测方式见[遥测指南](telemetry.md)。

[webapi]: https://partner.steamgames.com/doc/webapi/ISteamRemoteStorage#GetCollectionDetails
[ugc-install]: https://partner.steamgames.com/doc/api/ISteamUGC#GetItemInstallInfo
[ugc-download]: https://partner.steamgames.com/doc/api/ISteamUGC#DownloadItem
[steamcmd]: https://partner.steamgames.com/doc/sdk/uploading/distributing_gs
[python-steam]: https://github.com/ValvePython/steam/blob/v1.4.4/steam/client/cdn.py#L893
[steamkit]: https://github.com/SteamRE/SteamKit
[depot]: https://github.com/SteamRE/DepotDownloader
