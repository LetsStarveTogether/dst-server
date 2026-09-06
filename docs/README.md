# 项目文档

首次部署见项目 [快速开始](../README.zh-Hans.md#快速开始)。
后续按要完成的任务查阅：

| 文档 | 解决的问题 |
| --- | --- |
| [配置与部署](configuration.md) | 配置目录、分片端口、世界设置、配置 SDK 和容器 DNS。 |
| [运行机制](runtime.md) | Controller、Agent 与游戏进程的关系，以及启动、保存、重启和故障恢复。 |
| [遥测](telemetry.md) | 选择事件 profile、配置 OTLP、查询记录与判断投递故障。 |
| [Mod 更新](mods.md) | 使用默认原生更新器或独立 SteamCMD SDK，处理旧版与 UGC 模组。 |

源码导航使用 [DST Lua 索引](../dst-scripts/index/README.md)。
开发检查命令见 [justfile](../justfile)，公共入口与操作示例见项目 [README](../README.zh-Hans.md)。
