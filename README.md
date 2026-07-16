# Don't Starve Together Dedicated Server Container Image

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

![Steam store header](https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/322330/header_schinese.jpg?t=1736195686)

## Image quay.io/wh2099/dst-server

## Key Features

- Don't Starve Together
- Dedicated Server
- Container Image
- Steam platform
- 64-bit Linux system
- Minimal and clean by the [KISS principle](https://en.wikipedia.org/wiki/KISS_principle)

## First Install

1. Make sure your server has a container runtime installed, such as [Podman](https://docs.podman.io/en/latest/index.html).
2. Create a path for saved server data.

    ```shell
    mkdir -p "${HOME}/cluster"
    ```

    - You can choose another path. If you do, update the container command below accordingly.

3. Upload the cluster configuration folder.

   Follow Klei's [Configure and download the server settings](https://forums.kleientertainment.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/#:~:text=3.%20Configure%20and%20download%20the%20server%20settings) guide. Extract the downloaded archive and upload the whole configuration folder into the storage path created above.
   - The cluster folder name is not important, but the storage path should contain exactly one cluster folder. Common names are `Cluster_1` and `MyDediServer`.
   - To customize the world, add a `worldgenoverride.lua` to each shard that needs it.
     A client-exported `leveldataoverride.lua` is optional.
     See [Configuration Files](#configuration-files).
   - `cluster_token.txt` is dedicated-server specific and must be generated from Klei's [server management page](https://accounts.klei.com/account/game/servers?game=DontStarveTogether).

4. Pull the image and start the container.

    ```shell
    sudo podman run --name dst -d --network host -v "${HOME}/cluster:/cluster" quay.io/wh2099/dst-server:latest
    ```

## Maintenance

### Basic Commands

These are regular container control commands.

- Stop and save automatically: `podman stop dst`
- Start after the first run: `podman start dst`
- Restart: `podman restart dst`
- View logs: `podman logs dst`

### Game Console Commands

You can send console commands to the server through the `console` named pipe in the cluster or shard folder. For example:

```shell
echo 'c_announce("Server maintenance is coming. Please prepare in advance.")' > "Cluster_1/console"
```

Common console commands:

- Reload the world: `c_reset()`
- Regenerate the world: `c_regenerateworld()`
- Save manually: `c_save()`
- Shut down without saving: `c_shutdown(false)`
- Send an announcement: `c_announce("Announcement text")`
- List players: `c_listallplayers()`
- Unlock crafting: `c_freecrafting()`
- ...

### Cluster Architecture

> A Don't Starve Together dedicated server commonly runs as a shard cluster. Each world is an independent shard process, and exactly one shard acts as the master. The master shard and all secondary shards form one cluster. The common Forest + Caves setup is a two-shard cluster: Forest and Caves run as separate processes, with Forest as the master.

A multi-layer world usually means a cluster with more than two shards.

When the container starts, this project automatically detects the configuration directory structure mounted at `/cluster`. You do not need to manually specify the cluster or shard names. Just make sure the configuration directory follows these rules:

- The configuration directory contains exactly one cluster configuration folder. The folder name is unrestricted, commonly `Cluster_1`.
- The cluster configuration folder must contain `cluster.ini`.
- The cluster configuration folder must contain `cluster_token.txt`.
- The cluster configuration folder may contain only these folder types:
  - `mods`, the mod folder. It is optional and generated automatically after startup.
  - Shard configuration folders. Names are unrestricted, commonly `Master` and `Caves`.
- Each shard configuration folder must contain `server.ini`.

This project starts every shard folder it finds, so multi-layer worlds do not need multiple container starts. Put all shard configuration folders under the same cluster folder.

## Configuration Files

This section describes the dedicated server configuration file structure and variables. Commented values are defaults.

Skip this section if you do not need the details.

```text
Cluster_1  # Cluster mode. Forest and Caves are separate server processes.
├── cluster.ini  # Cluster configuration
├── cluster_token.txt  # Cluster authentication token
├── adminlist.txt  # Admin list, one Klei ID (KU_XXXXXXXX) per line
├── blocklist.txt  # Ban list, one SteamID64 per line
├── whitelist.txt  # Privileged player list, one Klei ID (KU_XXXXXXXX) per line
├── Master  # Master server process, usually Forest
│   ├── server.ini  # Server configuration
│   ├── modoverrides.lua  # Mod settings
│   ├── worldgenoverride.lua  # Optional user-editable world generation and world settings
│   ├── leveldataoverride.lua  # Optional game-managed complete level data
│   └── save  # Save data
│       └── ...
├── Caves  # Caves server process
│   ├── server.ini  # Server configuration
│   ├── modoverrides.lua  # Mod settings
│   ├── worldgenoverride.lua  # Optional user-editable world generation and world settings
│   ├── leveldataoverride.lua  # Optional game-managed complete level data
│   └── save  # Save data
│       └── ...

mods  # Mod-related files under the game server install directory
└── dedicated_server_mods_setup.lua  # Mod loading setup
```

### World Configuration Overrides

Each shard reads its world configuration independently.
Place a separate `worldgenoverride.lua` in every shard directory that needs customized settings.

`leveldataoverride.lua` is optional and primarily game-managed.
It contains a complete, materialized level definition used to transfer settings from a client to cluster servers.
If present, it replaces the shard's existing or default level data and must not be a partial patch.
If absent, startup falls back to the supplied or default level data and continues to load `worldgenoverride.lua`.
Normal dedicated-server configuration therefore does not need this file.

Despite its historical name, `worldgenoverride.lua` controls both world generation and runtime world settings.
It is loaded after `leveldataoverride.lua`, so its presets and explicit `overrides` have final precedence.
If both `worldgen_preset` and `settings_preset` resolve successfully, they form a complete replacement.
Otherwise, the available preset data and explicit `overrides` are merged into the current baseline.
Set `override_enabled = true` or the file is ignored.

A standard Forest + Caves cluster can omit both `leveldataoverride.lua` files and use these two files:

`Master/worldgenoverride.lua`:

```lua
return {
    override_enabled = true,
    worldgen_preset = "SURVIVAL_TOGETHER",
    settings_preset = "SURVIVAL_TOGETHER",
    overrides = {},
}
```

`Caves/worldgenoverride.lua`:

```lua
return {
    override_enabled = true,
    worldgen_preset = "DST_CAVE",
    settings_preset = "DST_CAVE",
    overrides = {},
}
```

An enabled `worldgenoverride.lua` may be rewritten during saves to synchronize current overrides, so stop the shard before editing it.
The file configures an existing shard; it does not create or enable one.
The Caves shard still needs its own directory and valid `server.ini`, and `[SHARD] / shard_enabled` must be enabled in `cluster.ini`.

### cluster.ini

```ini
[MISC]
; Maximum snapshots to keep.
; These snapshots are created on each save and are available from the Rollback tab on the Host Game screen.
; max_snapshots = 6

; Allow Lua commands from the command prompt or terminal where the server is running.
; console_enabled = true


[SHARD]
; Enable server sharding.
; For multi-level servers this must be set to true.
; For single-level servers it can be omitted.
; shard_enabled = false

; Network address listened on by the master server for secondary shard connections.
; If all servers in the cluster run on the same machine, use 127.0.0.1.
; If servers run on different machines, use 0.0.0.0.
; This only needs to be set for the master server and can be set in cluster.ini or the master's server.ini.
; Can be overridden in server.ini.
; bind_ip = 127.0.0.1

; IP address used by secondary shards when connecting to the master shard.
; If all servers in the cluster run on the same machine, use 127.0.0.1.
; Can be overridden in server.ini.
; master_ip =


; UDP port listened on by the master server and used by secondary shards when connecting to it.
; Set the same value for all shards in cluster.ini, or omit it entirely to use the default.
; This must differ from server_port for any shard running on the same machine as the master shard.
; Can be overridden in server.ini.
; master_port = 10888

; Password used to authenticate secondary servers with the master server.
; If servers that need to connect to each other run on different machines, this value must be the same on every machine.
; For servers on the same machine, setting it only in cluster.ini is enough.
; Can be overridden in server.ini.
; cluster_key =


[STEAM]
; When true, only players belonging to the Steam group specified by steam_group_id can connect.
; steam_group_only = false

; Steam group ID used by steam_group_only and steam_group_admins.
; steam_group_id = 0

; When true, administrators of the Steam group specified by steam_group_id also become server admins.
; steam_group_admins = false


[NETWORK]
; Create an offline cluster.
; The server will not be publicly listed. Only local network players can join, and Steam-related features are disabled.
; offline_cluster = false

; Number of server updates sent to clients per second.
; Increasing this improves precision but uses more bandwidth.
; The recommended default is 15.
; Change this only for LAN games, and use a number that divides 60, such as 15, 20, or 30.
; tick_rate = 15

; Number of slots reserved for whitelisted players.
; Add a Klei UserId to whitelist.txt to whitelist a player.
; This is only valid in the master shard's cluster.ini.
; whitelist_slots = 0

; Password players must enter to join the cluster.
; Empty or omitted means no password.
; This is only valid in the master shard's cluster.ini.
; cluster_password =

; Cluster name shown in the server list.
; This is only valid in the master shard's cluster.ini.
; cluster_name =

; Cluster description shown in the server information area of the Browse Games screen.
; This is only valid in the master shard's cluster.ini.
; cluster_description =

; When true, the server accepts connections only from the same LAN.
; This is only valid in the master shard's cluster.ini.
; lan_only_cluster = false

; When false, the game no longer autosaves at the end of each day.
; The game still saves on shutdown and can be manually saved with c_save().
; autosaver_enabled = true

; Cluster language.
; Chinese: zh
; cluster_language = en

[GAMEPLAY]
; Maximum number of players connected to the cluster at the same time.
; This is only valid in the master shard's cluster.ini.
; max_players = 16

; Player-versus-player combat, also known as friendly fire.
; pvp = false

; Cluster game mode.
; This field corresponds to the Game Mode field in the Create Game screen.
; Common valid values are listed below, without the comments in parentheses:
; survival  (Survival)
; endless  (Endless)
; wilderness  (Wilderness)
; For Forge, Gorge, or similar mods, this may need a mod-specific value. Check the corresponding mod documentation.
; game_mode = survival

; Pause the server when no players are connected.
; pause_when_empty = false

; Enable voting.
; vote_enabled = true
```

### server.ini

```ini
[SHARD]
; Set a shard as the cluster's master shard.
; Every cluster must have exactly one master server.
; Set this to true in the master's server.ini.
; Set this to false in every other server.ini.
; is_master =

; Shard name shown in log files.
; Ignored for the master server. The master server name is always [SHDMASTER].
; name =

; This field is generated automatically for non-master servers and is used internally to uniquely identify a server.
; Changing or deleting it while any character is in the world managed by this server can cause problems.
; id =


[STEAM]
; Internal port used by Steam.
; Make sure every server running on the same machine uses a different value.
; authentication_port = 8766

; Internal port used by Steam.
; Make sure every server running on the same machine uses a different value.
; master_server_port = 27016


[NETWORK]
; UDP port listened on by this server.
; If you run a multi-level cluster, use a different value for each server on the same machine.
; This port must be between 10998 and 11018 inclusive for LAN players to see it in their server list.
; On some operating systems, ports below 1024 require privileged users.
; server_port = 10999


[ACCOUNT]
; Whether to encode user save paths.
; If true, Klei IDs are used directly as paths, requiring a case-sensitive filesystem.
; If false, player data uses encoded folder names and does not depend on filesystem case sensitivity.
; encode_user_path = false
```

### dedicated_server_mods_setup.lua

```lua
-- Two leading hyphens mark a comment and are not executed.
-- There are two functions for installing mods: ServerModSetup and ServerModCollectionSetup.
-- This script runs on startup and downloads the specified mods into the mods directory.
-- ServerModSetup takes the Steam Workshop item ID as a string.
    -- For a mod or collection page, the number at the end of the URL is the ID.
    -- Example mod: https://steamcommunity.com/sharedfiles/filedetails/?id=351325790
 -- ServerModSetup("351325790")
    -- Example collection: https://steamcommunity.com/sharedfiles/filedetails/?id=2594933855
 -- ServerModCollectionSetup("2594933855")
```

### Dedicated Server Startup Options

These options belong to `dontstarve_dedicated_server_nullrenderer`, not to Podman.
This image selects the storage, cluster, shard, mod, and parent-process options automatically, so normal container use does not require them.
Command-line values override matching settings in `cluster.ini` or `server.ini`.

This summary reconciles Klei's [current command-line guide](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide) with the original [2016 forum guide](https://forums.kleientertainment.com/forums/topic/64743-dedicated-server-command-line-options-guide/), [mod-update announcement](https://forums.kleientertainment.com/forums/topic/51981-game-update-129926-3122015/), [UGC-directory announcement](https://forums.kleientertainment.com/forums/topic/128047-game-update-456207/), [`-region` workaround](https://forums.kleientertainment.com/klei-bug-tracker/dont-starve-together/503-failed-to-send-server-listings-r33770/), [Linux setup guide](https://forums.kleientertainment.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/), and developer [`-cloudserver` explanation](https://forums.kleientertainment.com/forums/topic/118972-unix-python-web-portal-for-dedicated-dst-server/#findComment-1344090).

| Option | Description |
| --- | --- |
| `-persistent_storage_root <absolute_path>` | Sets the root containing the configuration directory. The defaults are the user's Documents `Klei` folder on Windows, `~/Documents/Klei` on macOS, and `~/.klei` on Linux. |
| `-conf_dir <name>` | Sets the configuration directory name without slashes. The default is `DoNotStarveTogether`. |
| `-cluster <name>` | Selects the cluster directory containing `cluster.ini`. The default is `Cluster_1`. |
| `-shard <name>` | Selects the shard directory containing `server.ini`. The default is `Master`. |
| `-offline` | Starts an unlisted LAN-only server without Steam features. |
| `-disabledatacollection` | Disables data collection and therefore restricts the server to offline mode. |
| `-bind_ip <address>` | Changes the address used to listen for player connections. Most servers do not need this advanced option. |
| `-region <region>` | Overrides the automatically selected lobby registration region. Known values are `CHINA`, `SING`, `EU`, and `US`; Klei documented `EU` only as a temporary workaround, and the current guide omits this option, so normally leave it unset. |
| `-port <port>` | Sets the UDP player port and overrides `[NETWORK] / server_port`. Valid values are 1024–65535; each shard needs a different port, and LAN discovery requires 10998–11018. |
| `-players <count>` | Sets the maximum player count and overrides `[GAMEPLAY] / max_players`. Valid values are 1–64. |
| `-steam_master_server_port <port>` | Sets Steam's internal master-server port and overrides `[STEAM] / master_server_port`. Use a different 1024–65535 port for every server process on the same host. |
| `-steam_authentication_port <port>` | Sets Steam's internal authentication port and overrides `[STEAM] / authentication_port`. Use a different 1024–65535 port for every server process on the same host. |
| `-backup_log_count <count>` | Sets the number of log backups to keep. The default is 100. |
| `-backup_log_period <seconds>` | Sets the interval between log backups. The default is 86400 seconds. |
| `-tick <rate>` | Sets client updates per second and overrides `[NETWORK] / tick_rate`. Valid values are 15–60; Klei recommends the default 15 and, for LAN changes, a divisor of 60 such as 20 or 30. |
| `-fo` | Makes the server friends-only. |
| `-only_update_server_mods` | Updates the mods listed in `dedicated_server_mods_setup.lua`, then exits without starting a world. |
| `-skip_update_server_mods` | Skips the server-mod update during startup. |
| `-ugc_directory <path>` | Overrides the directory used for v2/UGC mods. |
| `-token <token>` | Passes the cluster token directly instead of reading `cluster_token.txt`. Prefer the file to avoid exposing the secret through process arguments. |
| `-allow_ioopenwrite_sandbox_escape` | Allows mods and tools to use `io.open` and `io.write`; enable it only for trusted code. |
| `-monitor_parent_process <pid>` | Stops the server automatically when the specified parent process dies. |
| `-cloudserver` | Enables Linux-only IPC: file descriptor 3 accepts newline-terminated Lua, descriptor 4 returns results ending in `DST_RemoteCommandDone`, and descriptor 5 emits server events. This image does not enable it. |

The selected shard configuration path is `<persistent_storage_root>/<conf_dir>/<cluster>/<shard>/server.ini`.
`-console` is deprecated; use `[MISC] / console_enabled` and standard input instead.
`-backup_logs` was replaced by `-backup_log_count`; use `-backup_log_period` separately to control the interval.

## References

- [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)
- [Dedicated Server Quick Setup Guide - Linux](https://forums.kleientertainment.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/)
- [Dedicated Server Settings Guide](https://forums.kleientertainment.com/forums/topic/64552-dedicated-server-settings-guide/)
- [Dedicated Server Command Line Options Guide](https://forums.kleientertainment.com/forums/topic/64743-dedicated-server-command-line-options-guide/)
- [Understanding Shards and Migration Portals](https://forums.kleientertainment.com/forums/topic/59174-understanding-shards-and-migration-portals/)
- [worldgenoverride.lua with the new post-Caves settings](https://forums.kleientertainment.com/forums/topic/53014-worldgenoverridelua-with-the-new-post-caves-settings/)
- [[Server Admin] Associate your server with a steam group](https://forums.kleientertainment.com/forums/topic/55994-server-admin-associate-your-server-with-a-steam-group/)
- [Update 126042 - 2/6/2015](https://forums.kleientertainment.com/forums/topic/50615-update-126042-262015/)
- [[Game Update] - 455519](https://forums.kleientertainment.com/forums/topic/127822-game-update-455519/)
