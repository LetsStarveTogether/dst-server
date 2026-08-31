# Don't Starve Together Dedicated Server Image

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

Container image: `quay.io/wh2099/dst-server`

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

   Reserve the range from automatic Linux port allocation before starting rooms:

   ```shell
   sudo install -Dm0755 deploy/sysctl/dst-server-reserve-ports /usr/libexec/dst-server-reserve-ports
   sudo install -Dm0644 deploy/sysctl/dst-server-port-reservation.service /etc/systemd/system/dst-server-port-reservation.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now dst-server-port-reservation.service
   ```

4. Reload the rootless systemd manager and start a generated Pod:

   ```shell
   systemctl --user daemon-reload
   systemctl --user start dst-room-000-pod.service
   ```

For a rootful deployment, use `/srv/dst` as `--cluster-root` and `/etc/containers/systemd` as `--quadlet-dir`.
Omit `--user` from its systemctl commands.

Images use `:<game-version>` as their stable version tag.
`:latest` and `:beta` are moving channel aliases.

## Operations

```shell
systemctl --user status dst-room-000-pod.service
journalctl --user -u dst-room-000-forest.service -f
systemctl --user restart dst-room-000-pod.service
systemctl --user stop dst-room-000-pod.service
```

Stopping a room does not implicitly save it.
Call the cluster save RPC before stopping when a fresh snapshot is required.

Each Agent also creates a `console` FIFO as a recovery interface.
The master FIFO is at the cluster root and secondary FIFOs are in their shard directories:

```shell
echo 'c_announce("Server maintenance is coming.")' > "${HOME}/.local/share/dst/room-000/console"
echo 'c_save()' > "${HOME}/.local/share/dst/room-000/cave/console"
```

The FIFO can execute arbitrary server Lua and must remain in the same trust boundary as the game process.

## Runtime Architecture

The master container runs `dst-server master`.
Secondary containers run `dst-server serve <shard>` and register with the master over a Pod-internal socket.

All containers mount the same cluster directory at `/cluster` and use the image's game installation at `/install`.
The master exposes the cluster RPC socket at `/cluster/.dst-server.sock` with owner-only access.

The controller waits for the complete configured shard roster before preparing or starting any game process.
The first preparation of a configuration revision validates the shared tree and updates its required server Mods.
An explicit Mod refresh requires `stop()`, `update_mods()`, then `start()`.

Each Agent supervises its own game process and retries bounded transient failures.
An exhausted retry budget or a lost Agent causes the controller to stop the remaining game processes.
After retry-budget fail-close, the Agent daemons and public RPC stay available for diagnosis and recovery.
Losing the master container disconnects RPC until systemd restarts the master and its bound secondaries.

Container health checks monitor each Agent heartbeat, and systemd restarts failed containers.
See [Runtime architecture](docs/python-sdk-telemetry-flow.md) for lifecycle states and failure boundaries.

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
See [Game events and OpenTelemetry](docs/opentelemetry-game-events.md) for the data and failure boundaries.

## Lua Annotations

`dst-annotations` generates LSP-compatible Lua definitions from DST components or `modutil.lua`.

```console
dst-annotations dst-scripts/scripts/components --output components_def.lua
```

## Documentation

- [Cluster architecture and configuration](docs/dedicated-server-configuration.md)
- [Dedicated server command-line options](docs/dedicated-server-options.md)
- [`-cloudserver` IPC contract](docs/cloudserver-ipc.md)
- [Runtime architecture](docs/python-sdk-telemetry-flow.md)
- [Game events and OpenTelemetry](docs/opentelemetry-game-events.md)
- [DST Lua source index](dst-scripts/index/README.md)

Detailed project documentation is maintained in Simplified Chinese.
