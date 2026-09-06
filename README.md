# Don't Starve Together Dedicated Server Image

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

Default image: `quay.io/wh2099/dst-server:latest`

This project runs a DST cluster as one Pod with one long-lived Agent container per shard.
The master container coordinates the cluster and exposes RPC.
Every Agent owns one restartable game process.

## Quick Start

1. Install [Podman](https://docs.podman.io/en/latest/index.html).
2. Create a dedicated-server token on [Klei's server management page](https://accounts.klei.com/account/game/servers?game=DontStarveTogether).
3. Export the token and generate the room configuration and Quadlet units:

   ```shell
   export DST_SERVER_CLUSTER_TOKEN='replace-with-cluster-token'
   uv run python -m scripts.generate_rooms \
     --userns 'keep-id:uid=1000,gid=1000' \
     --cluster-root "${HOME}/.local/share/dst" \
     --quadlet-dir "${HOME}/.config/containers/systemd"
   ```

   Use `--token-file /run/secrets/dst_cluster_token` instead when the token is mounted as a secret.
   An explicit token file takes precedence over the environment variable.

   Pass room numbers after the module name to generate only selected rooms, for example `0 20 139`.
   With no room numbers, the script generates the declared rooms `000–139`.

   Each room number selects one ten-port host slot in `30000–32999`.
   A room supports at most four shards and publishes only the ports its shards use.
   Concurrent rooms must use distinct slots.
4. Reload the rootless systemd manager and start a generated Pod:

   ```shell
   systemctl --user daemon-reload
   systemctl --user start dst-000-pod.service
   ```

Run the rootless commands above as the regular user who owns the cluster directory.
The `--userns` option maps that user to the container's `steam` user (UID/GID `1000`) through the Pod's `UserNS` setting.
For a rootful deployment, run the generator as root:

```shell
uv run python -m scripts.generate_rooms \
  --volume-idmap 'uids=0-1000-1;gids=0-1000-1' \
  --cluster-root /srv/dst \
  --quadlet-dir /etc/containers/systemd
```

The idmapped mounts preserve root ownership on the host while containers see UID/GID `1000`.
Omit `--user` from its systemctl commands.
Mapping options are empty by default; the generator does not choose them automatically.
See [Container users and directory permissions](docs/configuration.md#容器用户与目录权限) for the generated settings and requirements.
For rootful containers that reuse the host's DNS over TLS, see [Container DNS](docs/configuration.md#容器-dns).
Mod downloads use the native game-server updater by default, with up to five attempts sharing one 30-minute deadline.
Set `DST_SERVER_MOD_UPDATER=steamcmd` to use the independent SteamCMD backend during startup.
See [Mod updaters](docs/mods.md) for SDK usage, compatibility checks, and backend selection.

Generated configurations default to `:latest`; use `--image quay.io/wh2099/dst-server:beta` for the beta channel.
Both tags follow their release channels; the generator does not resolve or pin image digests or game versions.
Container units use `Pull=always` to check the registry on every start.
`TimeoutStartSec=1800` allows 30 minutes for downloads.
`systemctl daemon-reload` reloads configuration; running rooms use the updated image on their next restart.

## Operations

```shell
systemctl --user status dst-000-pod.service
journalctl --user -u dst-000-forest.service -f
systemctl --user restart dst-000-pod.service
systemctl --user stop dst-000-pod.service
```

Stopping a room does not implicitly save it.
Call the cluster save RPC before stopping when a fresh snapshot is required.

Each Agent also creates a `console` FIFO as a recovery interface.
The master FIFO is at the cluster root and secondary FIFOs are in their shard directories:

```shell
echo 'c_announce("Server maintenance is coming.")' > "${HOME}/.local/share/dst/000/console"
echo 'c_save()' > "${HOME}/.local/share/dst/000/cave/console"
```

The FIFO can execute arbitrary server Lua and must remain in the same trust boundary as the game process.

## Save Files

See [Configuration files](docs/configuration.md#配置文件) for the overall cluster layout.
Each shard has its own `save/` directory containing world and player snapshots, indexes, settings, and temporary data.
The `forest` and `cave` shards save their worlds and players separately, using different world session IDs.
A session ID identifies a shard's generated world, not a player's login.
The following vanilla layout uses encoded player paths, placeholder IDs, and one example snapshot per directory.
Some auxiliary files and directories are optional or empty.

```text
<shard>/save/
├── shardindex
├── shardindex_time
├── session/
│   └── <session-id>/
│       ├── 0000000001
│       ├── 0000000001.meta
│       └── <encoded-user-id>/
│           ├── 0000000001
│           ├── 0000000001.meta
│           └── savelocation
├── profile
├── modindex
├── boot_modindex
├── cached_userid
├── server_temp/
│   └── server_save
├── client_temp/
├── event_match_stats/
├── world_presets/
└── mod_config_data/
```

`shardindex` identifies the saved world and its settings; the actual world and player states live in `session/`.
Vanilla saves five fields in this index:

| Field | Contents |
| --- | --- |
| `world` | World options, including location, presets, and overrides; it does not contain the map itself. |
| `server` | Saved server settings, such as game mode, player limit, name, password, online mode, and `encode_user_path`. |
| `session_id` | The world identifier used by `session/<session-id>/`. |
| `enabled_mods` | Mods enabled for this save and their configuration. |
| `version` | Index schema version, distinct from the game build version. |

`shardindex_time` stores `created` and `saved`, the creation and most recent save times in Unix seconds.
Saving preserves the existing `created` value and updates `saved` to the current real time.
Neither value represents an in-game day.

Files with numeric names and no extension are the core snapshots.
World snapshots sit directly under the world session directory, while player snapshots sit in its player subdirectories.
Each accompanying `.meta` file provides a small summary:

| File | Contents |
| --- | --- |
| World snapshot, such as `<session-id>/0000000001` | Tiles, roads, map dimensions and topology; persistent entities such as structures, creatures, and ground items; world and network component state; Mod records; and an optional list of online players. |
| World `.meta` | `clock` and `seasons` summaries, including the day counter, day phase, season, and season progress, for rollback listings. |
| Player snapshot, such as `<encoded-user-id>/0000000001` | Character, position, age, skins, and saved component state, such as health, hunger, sanity, inventory, equipment, backpack contents, learned recipes, and character-specific data. |
| Player `.meta` | Vanilla writes only `character = player.prefab`; the full player state remains in the snapshot. |
| Player `savelocation` | An optional native binary file recording snapshot numbers and shard IDs to locate the player's save on the appropriate shard. |

The world snapshot's internal `savedata.meta` records the game build, random seed, world type, and save version.
It differs from the external `.meta` summary.
Numeric filenames are save sequence numbers, not day numbers; the displayed day is `clock.cycles + 1`.
Manual saves can advance the sequence without advancing a day.
Player snapshot numbers can have gaps and need not match every world snapshot.
World saves serialize the current `AllPlayers`, and some player events save separately.
The engine selects the appropriate player session file during loading.

The auxiliary paths serve different purposes:

| Path | Purpose and contents |
| --- | --- |
| `profile` | A runtime profile whose logical contents are JSON, including controls, startup information, favorite Mods, custom presets, and hint state; player characters are saved in `session/`. |
| `modindex` | A Lua table of Mod management state, including known Mods, enabled and temporarily disabled states, and API versions; this file can exist even when no Mods are enabled. |
| `boot_modindex` | A Mod startup marker containing `loading` or `done`, tracking whether loading completed. |
| `cached_userid` | The native engine's cache of the server account's `KU_…` user ID, distinct from the cluster token and encoded player directory names. |
| `server_temp/server_save` | A temporary map copy created during world initialization with entities, snapshots, tiles, navigation, and some network state removed; it cannot restore a complete world. |
| `client_temp/` | A native client temporary directory whose files are managed by the engine as needed. |
| `event_match_stats/` | Event match statistics in CSV form, including results, rounds, time, and scores; the vanilla writing branch excludes dedicated servers. |
| `world_presets/` | Saved custom presets: `.wsp` files hold world settings and `.wgp` files hold world generation settings, including the base preset, overrides, name, description, and version. |
| `mod_config_data/` | Mod configuration options and selected values, usually in `modconfiguration_<mod-name>` files; Mods may also add their own data. |

The SDK defaults to `encode_user_path = true` and always writes the configured value to `server.ini`.
An explicit `false` is preserved.
When encoding is enabled, online player directories use 12-character encoded Klei IDs.
When changing existing saves, keep `[ACCOUNT].encode_user_path` in `server.ini` consistent with the player directory names.
Also update `server.encode_user_path` in `shardindex` to match.
Preserve all player snapshots, `.meta` files, and `savelocation` records when migrating player directories.
Enabling path encoding changes the directory name used to locate a save, not the player's Klei user ID.
Klei IDs stored as account identities, such as `cached_userid`, remain unchanged.
The tables describe logical contents; KLEI wrappers, compression, and trailing null bytes depend on the writing interface.
These Mod management files and directories belong to vanilla Mod support and may exist even when no Mods are enabled.
Entities and components supply saved data through `OnSave()`; Mods can extend records and create additional files.

Index fields are defined by [ShardIndex](dst-scripts/scripts/shardindex.lua).
World snapshots and summaries are generated by [SaveGame](dst-scripts/scripts/mainfunctions.lua).
Player snapshots and summaries are written by [SerializeUserSession](dst-scripts/scripts/networking.lua).
Component data is collected in [entity save records](dst-scripts/scripts/entityscript.lua).
Cross-shard player lookup is handled by the [save-slot loading flow](dst-scripts/scripts/saveindex.lua).

## Runtime Architecture

The master container runs `dst-server master`.
Secondary containers run `dst-server serve <shard>` and register with the master over a Pod-internal socket.

All containers mount the same cluster directory at `/cluster` and use the image's game installation at `/install`.
The master exposes the cluster RPC socket at `/cluster/.dst-server.sock` with owner-only access.

The controller waits for the complete configured shard roster before preparing or starting any game process.
Cluster startup updates the required server Mods before starting game processes.
Cluster `restart()` stops every game process before updating their shared Mods and starting them again.
An explicit Mod refresh uses `stop()`, `update_mods()`, then `start()`; the last call reuses that successful update.
Adopting running Agents and recovering an individual shard reuse the installed Mods.

Each Agent supervises its own game process and retries bounded transient failures.
An exhausted retry budget or a lost Agent causes the controller to stop the remaining game processes.
After retry-budget fail-close, the Agent daemons and public RPC stay available for diagnosis and recovery.
Losing the master container disconnects RPC until systemd restarts the master and its bound secondaries.

Each Agent sends a native watchdog notification every 60 seconds through Podman's systemd notification socket.
Systemd restarts a container after five minutes without a notification; no heartbeat file or periodic check process is used.
See [Runtime architecture](docs/runtime.md) for lifecycle states and failure boundaries.

## Cluster RPC

Manage a deployed Pod through its Cap'n Proto Unix socket:

```python
import asyncio

from dst_server.rpc import ClusterClient, rpc_runtime


async def main() -> None:
    async with rpc_runtime():
        async with await ClusterClient.connect("/cluster/.dst-server.sock") as cluster:
            status = await cluster.status()
            print(status)
            print(await cluster.shard(status.master).status())


asyncio.run(main())
```

A host client uses the same socket through the mounted cluster path.
The API covers cluster and shard lifecycle, configuration revisions, game queries, administration, saves, raw Lua, and events.
Mutating calls are not automatically replayed when completion becomes indeterminate.

Applications that deliberately own one game process can use the in-process `Server` API.

## Telemetry

The default `critical` profile installs management RPC and key game events.
Set `DST_SERVER_TELEMETRY_PROFILE=off|critical|history` to select the event profile.
Standard `OTEL_EXPORTER_OTLP_*` variables configure export transport but do not enable event hooks.

Install `dst-server[otel]` for OTLP export or `dst-server[klei]` for Klei build and lobby services.
See [Game events and OpenTelemetry](docs/telemetry.md) for the data and failure boundaries.

## Lua Annotations

`dst-annotations` generates LSP-compatible Lua definitions from DST components or `modutil.lua`.

```console
dst-annotations dst-scripts/scripts/components --output components_def.lua
```

## Documentation

- [Guides: configuration, runtime, telemetry and Mods](docs/README.md)
- [Save archive exports](docs/configuration.md#导出存档)
- [DST Lua source index](dst-scripts/index/README.md)
