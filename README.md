# Don't Starve Together Dedicated Server Image

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

![Steam store header](https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/322330/header_schinese.jpg?t=1736195686)

Container image: `quay.io/wh2099/dst-server`

The image can run one shard or an embedded multi-process cluster and keeps the familiar console FIFO.

The recommended systemd deployment uses one Pod per cluster.
A standalone preparation container runs before one worker container per shard.

## Quick Start

1. Install a container runtime such as [Podman](https://docs.podman.io/en/latest/index.html).
2. Create a dedicated-server token on [Klei's server management page](https://accounts.klei.com/account/game/servers?game=DontStarveTogether).
3. Expose the token to the generator as `DST_SERVER_CLUSTER_TOKEN` through your secret manager.
   Then generate all typed room configurations and Quadlet units:

   ```shell
   uv run python -m scripts.generate_rooms \
     --cluster-root "${HOME}/.local/share/dst" \
     --quadlet-dir "${HOME}/.config/containers/systemd"
   ```

   Alternatively, pass a mounted secret file with `--token-file /run/secrets/dst_cluster_token`.
   An explicit token file takes precedence over the environment variable.

   Pass room numbers after the module name to generate only those rooms, such as `0 20 139`.

   The script generates rooms `000–139` from the twelve declared room ranges.
   These cover pure and semi-pure survival/endless, AFK, and lights-out survival/endless.
   Island adventure, Hamlet, adventure, Gorge, and Forge complete the list.

   The script uses the room number as its port slot.
   `RoomPortAllocation` still supports programmatic offsets within slots `000–299`.

   For shard ordinal `S`, the external game port is `30000 + 600 × S + slot`.
   Its Steam query port is `30300 + 600 × S + slot`.

   For example, room `007` uses `30007/30307` for Master and `30607/30907` for Caves.

   Four shards occupy `30000–32399`, within the Kubernetes
   [default NodePort range](https://kubernetes.io/docs/concepts/services-networking/service/#type-nodeport).

   Each slot supports up to four shards; NodePorts are cluster-wide resources and must not collide with other Services.

   The Pod owns every published port.
   Shard containers share the Pod network namespace and use their generated internal ports.
   Each worker passes its published game port through `--external-port`.
   Klei advertises that reachable host port while `server.ini` keeps the usual internal port.

Every image uses `:<game-version>` as its primary tag.
`:latest` and `:beta` remain moving channel aliases.

4. Reload the rootless systemd user manager and start the generated Pod:

   ```shell
   systemctl --user daemon-reload
   systemctl --user start dst-room-000-pod.service
   ```

For a rootful deployment, pass `/etc/containers/systemd` as `--quadlet-dir` and `/srv/dst` as `--cluster-root`.
Omit `--user` from the systemctl commands.

On a fresh rootful host whose default network still uses `podman0` and `10.88.0.0/16`, install the repository's configuration:

```shell
sudo install -Dm0644 deploy/podman/podman.json /etc/containers/networks/podman.json
```

If the target file exists or the host uses different network values, do not overwrite it.
Preserve its values and change only `dns_enabled` to `true`.
The repository file keeps Podman's native network defaults.
It only enables Aardvark DNS on the default bridge.
Podman's own guide uses the same [export-and-persist flow for changing the in-memory default network](https://github.com/podman-container-tools/podman/blob/main/docs/tutorials/basic_networking.md#default-network).
Stop and recreate any existing containers or Pods that use the default network after installing it.
When migrating from the former shared Quadlet network, stop the rooms and `dst-server-network.service` first.
Remove the old `dst-server.network` source, then reload systemd.

The Pod unit is attached to `default.target`, so it starts with that systemd manager's default target.

## Operations

```shell
systemctl --user status dst-room-000-pod.service
journalctl --user -u dst-room-000-forest.service -f
systemctl --user restart dst-room-000-pod.service
systemctl --user stop dst-room-000-pod.service
```

The SDK has a 30-second graceful shutdown deadline.
Generated workers allow 40 seconds in Podman and 50 seconds in systemd.

Each worker asks its shard to save and stop.
An unresponsive shard is force-killed after the grace period.

Each worker creates a named `console` pipe for its shard.
The master pipe is at the cluster root and each secondary pipe is in its shard directory:

```shell
echo 'c_announce("Server maintenance is coming.")' > "${HOME}/.local/share/dst/room-000/console"
echo 'c_save()' > "${HOME}/.local/share/dst/room-000/cave/console"
```

Common lifecycle commands include `c_reset()`, `c_regenerateworld()`, `c_save()`, and `c_shutdown(false)`.
Use `c_announce("...")` to send a message and `c_listallplayers()` to inspect players.

## What the Supervisor Does

`dst-server prepare` first runs in a standalone one-shot container.
It validates the cluster, prepares permission and Mod files, and updates Workshop content once.
Only after it succeeds does systemd start the Pod and each `dst-server run <shard>` worker.
The command starts exactly one shard through the `-cloudserver` protocol.
All containers mount the same cluster tree at `/cluster`.
The image uses the fixed `/install` and `/cluster` paths shown above.

Running the image without a subcommand retains the embedded supervisor.
It discovers and starts the complete cluster in one container.

## Python SDK

The `dst-server` package controls one Linux DST shard through `Server` or a complete cluster through `Cluster`.

```python
import asyncio

from dst_server import Server, ServerConfig


async def main() -> None:
    async with Server(ServerConfig(shard="Master")) as server:
        world = await server.game.world.state()
        players = await server.game.players.list()
        await server.game.world.announce(
            f"Day {world.day}: {len(players)} player(s) online"
        )
        await server.save()


asyncio.run(main())
```

### Typed Cluster Configuration

Cluster files use explicit world models and Lua-literal-safe Mod option mappings:

`RoomPreset` composes cluster settings, shards, worlds, and Mods for a complete room; see [room-level presets](docs/dedicated-server-configuration.md#房间级组合-preset).

```python
import asyncio
from pathlib import Path
from pydantic import SecretStr

from dst_server.cluster import (
    CaveOverrides,
    Cluster,
    ClusterConfig,
    ForestOverrides,
    ModOverride,
    ModOverrides,
    ModSettings,
    ShardConfig,
    ShardSettings,
    WorldgenOverride,
)


cluster = ClusterConfig(
    shards={
        "Master": ShardConfig(
            settings=ShardSettings(is_master=True, id=1),
            world=WorldgenOverride.forest(
                overrides=ForestOverrides(day="longday"),
            ),
            mods=ModOverrides(
                entries={
                    "workshop-351325790": ModOverride(
                        enabled=True,
                        configuration_options={"difficulty": "hard"},
                    ),
                },
            ),
        ),
    },
    token=SecretStr("replace-with-cluster-token"),
    mod_settings=ModSettings(disable_local_mod_warning=True),
)


async def run_cluster() -> None:
    path = Path("/srv/dst/Cluster_1")
    async with Cluster(
        path,
        config=cluster,
        log_handler=lambda shard, line: print(f"[{shard}] {line}"),
    ) as runtime:
        print(await runtime.execute_all("print(TheWorld ~= nil)"))
        print(await runtime["Master"].game.world.state())
        await runtime.save()


asyncio.run(run_cluster())
```

Use `WorldgenOverride.cave(overrides=CaveOverrides(...))` for cave-specific options.
`ModSettings` types `force_enabled`, `debug_print`, `mod_errors`, `disable_mod_disabling`, and `disable_local_mod_warning`.
Omitting `mod_settings` preserves an existing file, while explicitly passing `ModSettings()` clears these settings.
The setup file only downloads Mods; `ModOverride.enabled=True` or `ModSettings.force_enabled` enables them.
Native `saved_server` Mod values can shadow matching overrides, and the SDK does not delete that persistent game data.

Passing `config` to `Cluster` validates and saves the complete tree before startup.
Omitting it strictly loads the existing tree through `ClusterConfig.load()`.
Custom or ambiguous world fields require their typed registry.
The runtime exposes each shard as a `Server` and routes logs as `(shard, line)`.
It reads lifecycle and game events per shard and supports per-shard or concurrent raw commands.
Cluster-wide saves trigger only master once and wait for independent confirmation from every shard.

`Cluster` remains the embedded multi-process API for applications that want to own the complete runtime.
The recommended Quadlet deployment instead gives each shard an independent `Server` process and systemd unit.

### Typed Quadlet Configuration

Pod and container units support the same load, immutable edit, and atomic save flow as game configuration:

```python
from pathlib import Path

from dst_server.cluster import QuadletApplication

directory = Path.home() / ".config/containers/systemd"
application = QuadletApplication.load(directory, name="dst-room-000")
application = application.replace(
    pod=application.pod.replace(description="DST survival room 000"),
)
application.save(directory)
```

`QuadletApplication` lets the Pod and standalone prepare container use Podman's default network.
It does not generate a shared network unit for every deployment.
Rootful deployments enable DNS through the global `podman.json` above; rootless Podman keeps its native network defaults.
Reload systemd after changing saved Quadlet sources.
Changing or removing a published game port through `application.replace(pod=...)` also updates the matching worker.
Pass `workers` explicitly to preserve a deliberately different `--external-port`.

The typed API covers world and player queries, inventory, administration, confirmed saves, and raw Lua.
It also exposes lifecycle and game events.

Runtime blocklist queries and `unban()` apply only to the selected shard process and do not imply cross-shard synchronization.
Whitelist query and mutation use the master shard.
`is_admin()` returns the connected player's state or `None` when that player is offline.
Administrator list mutation remains a configuration-file operation.

The four reload methods are `reset()`, `regenerate()`, `regenerate_shard()`, and `rollback()`.
They return only after a newer FD5 generation has been observed and its driver is ready.
Their default 30-second completion timeout covers the trigger RPC, generation transition, and reinstall.
If Python observes a generation change after writing another typed command, the SDK raises `IndeterminateCommandError`.
It never replays the command automatically.

Finite management operations use one total deadline rather than a separate timeout for each wait.
The final encoded FD3 command line is limited to 64 KiB.
One FD4 frame is limited to 64 KiB of payload and 1,024 payload lines.

By default, the SDK installs the management RPC driver and the `critical` game-event profile.
OTLP environment variables configure export transport but do not enable game-event hooks.
Select `history` when fuller game-event coverage is required:

```python
from dst_server import ServerConfig
from dst_server.telemetry import TelemetrySettings

config = ServerConfig(
    shard="Master",
    telemetry=TelemetrySettings(profile="history"),
)
```

Use `off` to disable game-event telemetry.
Set `actions=()` to keep the other history events without wrapping `BufferedAction.Do`.

The container entrypoint reads the same choice from `DST_SERVER_TELEMETRY_PROFILE`.
For logs-only export to a local Netdata 2.11 Agent, see [Game events and OpenTelemetry](docs/opentelemetry-game-events.md).
The rootful Quadlet examples already use that host-only logs endpoint.

Install `dst-server[otel]` for OTLP export or `dst-server[klei]` for Klei build and lobby services.

## Lua Annotations

The `dst-annotations` command generates LSP-compatible Lua definitions from DST components or `modutil.lua`.

```console
dst-annotations dst-scripts/scripts/components --output components_def.lua
dst-annotations dst-scripts/scripts/modutil.lua --output modutil_def.lua
```

## Documentation

Game server reference:

- [Cluster architecture and configuration files](docs/dedicated-server-configuration.md)
- [Dedicated server command-line options](docs/dedicated-server-options.md)

Technical design:

- [`-cloudserver` bidirectional IPC](docs/cloudserver-ipc.md)
- [Game events and OpenTelemetry](docs/opentelemetry-game-events.md)

Source reference:

- [DST Lua source index](dst-scripts/index/README.md)

The detailed documentation is maintained in Simplified Chinese.
