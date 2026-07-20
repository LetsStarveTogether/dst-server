# Don't Starve Together Dedicated Server Image

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

![Steam store header](https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/322330/header_schinese.jpg?t=1736195686)

Container image: `quay.io/wh2099/dst-server`

The image runs every shard in one DST cluster and keeps the familiar console FIFO.

## Quick Start

1. Install a container runtime such as [Podman](https://docs.podman.io/en/latest/index.html).
2. Create and download a dedicated-server configuration from [Klei's server management page](https://accounts.klei.com/account/game/servers?game=DontStarveTogether).
3. Extract the archive on the host.

   The directory mounted at `/cluster` must directly contain `cluster.ini`, `cluster_token.txt`, and the shard directories:

   ```text
   Cluster_1/
   ├── cluster.ini
   ├── cluster_token.txt
   ├── Master/
   │   └── server.ini
   └── Caves/
       └── server.ini
   ```

4. Start the container, replacing the host path with the extracted cluster directory:

   ```shell
   sudo podman run \
     --name dst \
     --detach \
     --network host \
     --volume "${HOME}/Cluster_1:/cluster" \
     quay.io/wh2099/dst-server:latest
   ```

## Operations

```shell
podman logs dst
podman stop dst
podman start dst
podman restart dst
```

Stopping the container terminates every shard gracefully, and DST saves during shutdown.

The entrypoint creates a named `console` pipe for the master shard at the cluster root.
Each secondary shard receives one in its own directory:

```shell
echo 'c_announce("Server maintenance is coming.")' > "${HOME}/Cluster_1/console"
echo 'c_save()' > "${HOME}/Cluster_1/Caves/console"
```

Common lifecycle commands include `c_reset()`, `c_regenerateworld()`, `c_save()`, and `c_shutdown(false)`.
Use `c_announce("...")` to send a message and `c_listallplayers()` to inspect players.

## What the Entrypoint Does

On startup, [`entrypoint.sh`](entrypoint.sh) validates the cluster and prepares permission and Mod files.
It updates Workshop content once, discovers every shard, creates the console FIFOs, and starts the shard processes.
The image uses the fixed `/install` and `/cluster` paths shown above.

## Python SDK

The `dst-server` package starts and controls one Linux DST shard process.

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

The typed API covers world and player queries, inventory, administration, confirmed saves, and raw Lua.
It also exposes lifecycle and game events.

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
