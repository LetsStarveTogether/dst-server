# Don't Starve Together Dedicated Server

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

A Rust SDK, Agent and CLI for Don't Starve Together, with Python bindings.
Each room runs in one container: one Agent supervises every configured shard and remains available while the games are closed.
Published binaries and Python wheels support Linux x86_64 and CPython 3.14.
The control socket is for trusted callers on the same host.

Version `0.4.0` replaces the earlier Python implementation.
See the [migration guide](docs/migration.md) for API changes, verification and known limitations.
Internal readiness does not establish public network reachability.

Install the Python SDK with `pip install dst-server==0.4.0` in a CPython 3.14 environment.
The [GitHub release](https://github.com/LetsStarveTogether/dst-server/releases/tag/v0.4.0) also includes a standalone Linux x86_64 CLI.

## Build and Create a Room

Install Rust 1.99, a C/C++ build toolchain, Cap'n Proto, and [uv](https://docs.astral.sh/uv/).
Host deployment additionally requires Podman with Quadlet and systemd.
From this checkout:

```sh
cargo build --locked --release -p dst-server --bin dst-server
export PATH="$PWD/target/release:$PATH"
uv sync --python 3.14
dst-server --help
uv run python -m dst_server --help
```

The native binary and the Python entry point call the same Rust CLI.
Build the game image from this revision, supplying the exact game build that SteamCMD will install:

```sh
podman build --target game --build-arg GAME_VERSION=756039 \
  -t localhost/dst-server:rust .
```

The build checks the installed game version and packages the matching SDK Lua bundle.
Use the matching published image when creating rooms, or select your locally built image.

The default deployment image is `quay.io/wh2099/dst-server:latest`.
Set `deployment.image` or `--image` to `quay.io/wh2099/dst-server:beta` for beta.
Generated Quadlets keep these tags and set `AutoUpdate=registry` for both published channels.
Quadlets use `Pull=never`; pull the selected image before the first start.
Run `systemctl enable --now podman-auto-update.timer` on the host to enable periodic checks.
The timer updates containers and restarts them when a new image is available.

Create a Klei server token and store it in `/run/secrets/dst_cluster_token`.
Run the following with the account that owns the host room directories and systemd services:

```python
import asyncio
from pathlib import Path

from dst_server import Host
from dst_server.settings import Room, build_template


async def main():
    cluster = build_template(
        "pure_survival",
        number=299,
        token=Path("/run/secrets/dst_cluster_token").read_text().strip(),
    )
    room = Room(
        number=299,
        template="pure_survival",
        cluster=cluster,
        deployment={
            "image": "localhost/dst-server:rust",
            "volume_idmap": "uids=0-1000-1;gids=0-1000-1",
        },
    )
    await Host("/srv/dst", "/etc/containers/systemd").create(room)


asyncio.run(main())
```

This creates `/srv/dst/299` and `dst-299.container` without starting the service.
The illustrated ID mapping gives image UID/GID 1000 access to root-owned room files.
Choose ownership or mapping for your host.
Then start the service:

```sh
dst-server host start --room 299
dst-server host status --room 299
dst-server call list_players --socket /srv/dst/299/.dst-agent.sock
journalctl -u dst-299.service -n 100 -f
```

Use `host --root PATH --quadlet-dir PATH` for alternate directories, and `host --user` for the user systemd manager.
Room numbers range from `000` to `299` independently of gameplay templates.
The packaged fleet contains `000–099` and `200–219`:

```sh
dst-server template list
dst-server host fleet --room 000,030,209 \
  --token-file /run/secrets/dst_cluster_token --image localhost/dst-server:rust \
  --volume-idmap 'uids=0-1000-1;gids=0-1000-1'
```

## CLI

| Command | Purpose |
| --- | --- |
| `host` | Create, provision, edit, inspect and operate host services; batch by room, template or all rooms. |
| `call` | Invoke a room or shard operation through its Agent socket. |
| `describe` | Print method names, scope, argument schemas and default timeouts. |
| `subscribe` | Read bounded `logs`, `lifecycle` or `events` batches and their discarded counts. |
| `logs`, `archive` | Query or follow historical logs; export and upload consistent room archives. |
| `console` | Execute trusted Lua on an explicit shard, from text, a file or interactive input. |
| `config`, `template`, `inspect` | Validate configuration, inspect schemas and templates, discover native topology. |
| `scripts`, `annotations`, `completion` | Build or verify script bundles, generate Lua annotations and shell completion. |
| `agent` | Run the room supervisor, normally as the container entry point. |

Commands print JSON; `--json` uses compact JSON and structured errors.
A batch preserves individual results and fails its exit status when any target fails.
JSON arguments accept inline text, `@filename`, or `-` for stdin.

```sh
dst-server --json host list
dst-server host show 299 --field /cluster/settings/max_players
dst-server host schema
dst-server host diagnose --room 299
dst-server describe
dst-server call save --socket /srv/dst/299/.dst-agent.sock
dst-server call get_player '{"userid":"KU_example"}' --socket /srv/dst/299/.dst-agent.sock
dst-server subscribe lifecycle --socket /srv/dst/299/.dst-agent.sock
```

`call stop` stops games while leaving the Agent available; the opening schedule can start them again.
`host stop --room 299` stops the container service, including its Agent.
Use the latter before offline configuration changes:

```sh
dst-server host stop --room 299
dst-server host edit --room 299 --set '/cluster/settings/max_players=9'
dst-server host start --room 299
```

A closed game can still have a live Agent holding the room lock.
Use Agent operations for maintenance while it is running.
Changing world-generation settings preserves existing saves; `regenerate` creates new worlds.

## Rust and Python SDKs

The native API exposes typed requests without exposing Cap'n Proto generated types:

```rust
use dst_server::{model::{Envelope, Request, Target}, rpc::Client};

async fn inspect_room() -> dst_server::model::Result<serde_json::Value> {
    let client = Client::connect("/srv/dst/299/.dst-agent.sock").await?;
    let result = client.call(Envelope::new(Target::Room, Request::Status {})?).await;
    client.close().await?;
    result
}
```

Python exposes asynchronous calls and explicit connection and subscription cleanup:

```python
import asyncio
from dst_server import Client


async def main():
    async with await Client.connect("/srv/dst/299/.dst-agent.sock") as client:
        print(await client.call("status"))
        print(await client.call("save"))
        async with await client.subscribe("events") as events:
            print(await events.next(max_items=100))


asyncio.run(main())
```

Use `describe()` or `dst-server describe --shard` for the exact method contract.
Operations cover world queries, saves, rollback, regeneration, pause and player control.
They also cover migration, permissions, Mods and exports.
Player operations locate `userid` across shards and verify its identity before execution.
Pass `shard="forest"` when an operation needs an explicit shard.
Entity references contain world session and runtime generation; old references become invalid after reload.

Each room permits one mutation at a time and returns `busy` for a conflicting operation.
Accepted mutations belong to the Agent.
Cancellation, disconnection and caller timeouts stop waiting without undoing the operation.
Check `status` after an unknown result before deciding whether to retry.
Save success requires native completion on every shard.
Normal shutdown requires exit code zero, process reaping and output drainage, with no force or protocol fault.
Failed shutdown retains each shard's exit status and confirmed saved snapshot.
Signal termination uses the negative signal number.
The default stop wait is 220 seconds, including its notice; changing the notice delay adjusts that budget.
Mods that disable saving can stop cleanly with `saved_snapshot: null`.
An explicit save returns an error when no new snapshot is confirmed.
Migration success requires confirmation in the destination world.
Console success means the Lua code returned; an asynchronous effect started by that code may still be running.

## Configuration and Files

```text
/srv/dst/299/
├── cluster.ini
├── cluster_token.txt
├── adminlist.txt / blocklist.txt / whitelist.txt
├── .dst-control.json
├── .dst-room.lock
├── .dst-agent.sock
├── mods/
│   ├── dedicated_server_mods_setup.lua
│   ├── modsettings.lua
│   └── ugc/
├── forest/
│   ├── server.ini
│   ├── worldgenoverride.lua
│   ├── modoverrides.lua
│   └── save/
└── cave/
    └── server.ini, world overrides, Mods and saves
```

Native INI/Lua files define game configuration; the Quadlet defines deployment.
`.dst-control.json` stores the template label, policy, activity and recovery state.
It is not a second copy of game settings.
Keep it across container replacement to retain recovery limits and maintenance progress.
Never remove or replace `.dst-room.lock` while a room exists.

Every directory with `server.ini` is an enabled shard.
Names are arbitrary, with exactly one master.
Keys and IDs must be consistent, with conflict-free game, query and shard ports.
Port allocation uses the actual shard set, an allocation lock and a persistent map; the previous four-shard limit is removed.
The default host allocation pool is `30000–65535`.
All shards share the container network and room Mods; each process has separate control pipes and readers.

Python configuration factories validate through Rust:

```python
from dst_server.settings import ClusterSettings, RoomStore, build_template

settings = ClusterSettings(max_players=9, cluster_name="Friday games")
cluster = build_template("pure_endless", number=299, settings=settings)
print(cluster.dump(defaults=True))
print(settings.schema())

store = RoomStore("/srv/dst")
room = store.load(299)
updated = room.edit("/cluster/settings/cluster_description", "Friday games")
# After stopping the entire service:
# store.save(updated)
```

`dump()` redacts token, shared key and password; `secrets=True` explicitly returns them.
Rendered native files and `files()` contain real credentials and must be handled accordingly.
Defaults are applied on demand; omitted fields and explicit `None` remain distinct.
Frozen schemas, defaults and presets are packaged with Rust, including 12 templates and all 120 fleet rooms.
Rust owns validation and file operations; Python provides bindings, conversion and asynchronous wrappers.

Offline writes use the shared room lock, reject symlinks and commit recoverable file transactions with synchronization.
General configuration saves preserve live admin, ban and whitelist files.
Use permission operations for online changes.
Structured Lua reads parse Lua 5.1 literals without execution.
Dynamic configurations can be loaded by the game; this API cannot structurally edit them.

## Opening Hours, Maintenance and Recovery

The default policy uses `Asia/Shanghai`, opens all day, enables automatic Mod maintenance and disables idle world regeneration.
A policy can be sent to a running Agent:

```sh
dst-server call set_policy - --socket /srv/dst/299/.dst-agent.sock <<'JSON'
{
  "policy": {
    "timezone": "Asia/Shanghai",
    "schedule": [{"start": "18:00", "end": "00:00"}],
    "mod_auto_update": true,
    "idle_regeneration": false
  }
}
JSON
```

The Agent checks schedules every 30 seconds, including windows across midnight.
At closing time it stops all games and stays available for the next opening.
Pending Mod maintenance runs before reopening.
The default maintenance countdown is 60 seconds with announcements every 30 seconds.
Failed downloads or validation receive two additional attempts, five minutes apart, then leave games closed.
An explicit maintenance request or a later opening can retry.

A required shard crash or sustained control failure stops the whole room.
Abnormal process exits, forced termination and IPC faults require container replacement.
Recovery state persists across that replacement.
Retryable failures allow two restarts of the current save, 30 seconds apart.
Only confirmed save-loading failure can then try one previous complete room snapshot.
The loss must be proven to be at most one game day per world.
Targets are fixed before mutation; interrupted recovery resumes those same targets.
Unknown state, missing targets, unsupported failure causes or failed recovery leave the room closed.
Thirty minutes of stable operation restores the budget.
Exhausted rooms get another opportunity at the next opening, or explicit start for an always-open room.

Idle regeneration runs only when enabled, during opening hours, with every shard ready and reliably empty.
Its retention periods are 6, 24, 36, 72 and 168 hours according to world age.
Interrupted observations or changed worlds restart the retention period; clean scheduled closure preserves it.

## Logs, Exports and Utilities

The default game event profile is `history`; `agent --profile` also accepts `critical` and `off`.
Events are bounded and best effort.
Source gaps, subscriber overflow, output loss and export rejection have separate counters.
A subscription is not a replay log or a durable task trigger.

OTLP uses gRPC for logs, metrics and traces.
Signal-specific settings override common `OTEL_EXPORTER_OTLP_*` settings.
Their prefixes are `OTEL_EXPORTER_OTLP_LOGS_*`, `OTEL_EXPORTER_OTLP_METRICS_*` and `OTEL_EXPORTER_OTLP_TRACES_*`.
Configure endpoint, headers, TLS certificates, compression and timeout explicitly; timeout values are milliseconds.
Set a signal's `OTEL_*_EXPORTER=none`, or `OTEL_SDK_DISABLED=true` for all signals, to disable export.
Structured log transport preserves null, integers, floats, arrays and objects.
Netdata's flattened query projection has separate type limitations.

`dst_server.logs` provides `JournalLogs`, `JournalQuery`, `JournalStream`, `NetdataLogs` and `NetdataLogQuery`.
The CLI accepts those same query fields, including cursors, direction and filters:

```sh
dst-server logs journal --unit dst-299.service --query '{"limit":100,"since":"today"}'
dst-server logs journal --unit dst-299.service --follow
dst-server logs telemetry '{"since":1790899200,"until":1790985600,"limit":100}'
```

A follow stream emits one record at a time; interrupting a log query waits for its reader process to exit.
`dst_server.events` exposes the native event schema and validation through `event_schema()` and `validate_event()`.
`dst_server.KleiClient` provides bounded build, region, lobby and room requests with per-query batch failures.
HTTP failures are distinct from successful empty results.

Archive export prepares all shards while games are stopped and preserves native world/player data and Mod progress.
It removes credentials, permission lists and SDK runtime files.
Compression uses 7z with Zstd level 3 by default and one concurrent compressor.
Uploads use the native object-store implementation and explicitly abort incomplete multipart uploads on failure or cancellation.
Cleanup failures remain visible in the result and logs.
Python exposes `dst_server.archive.export_cluster` and `ClusterArchive.save/upload`.
Export requires stopped games and uses the live Agent or an exclusive offline lock.
For an offline local export:

```sh
dst-server host stop --room 299
dst-server archive export 299 --output /tmp/room-299.7z
```

The output path must not already exist.
See [the migration guide](docs/migration.md) for entry points and evidence.

```sh
dst-server scripts build /path/to/native/scripts.zip --output /tmp/scripts.zip
dst-server scripts verify /tmp/scripts.zip --source /path/to/native/scripts.zip
dst-server annotations components /path/to/scripts/components --output /tmp/components.lua
dst-server annotations modutil /path/to/scripts/modutil.lua --output /tmp/modutil.lua
```

## Development and Validation

The workspace contains [`crates/dst-server`](crates/dst-server) and [`crates/dst-server-python`](crates/dst-server-python).
Python wrappers live under [`python/dst_server`](python/dst_server), with Lua resources under [`resources/lua`](resources/lua).
The container runs the Rust binary directly.

```sh
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked -p dst-server --all-targets
uv run ruff check python tests/rust tools
uv run ty check python
uv run python tests/rust/check_lua.py
uv build --python 3.14 --out-dir dist --no-sources
```

CI builds a wheel from the source distribution and installs it in an isolated environment.
It then runs native binding and process tests.
Lua contracts run under Lua 5.1 and LuaJIT.
Game build `756039` has an unresolved [native shutdown failure](docs/migration.md#known-native-shutdown-failure) observed with five connected shards.
Gorge and Forge deliberately disable saving; the SDK reports an unconfirmed save and can still close them cleanly.
Real game probes use disposable directories and containers; they must not target live rooms.
Build manifests record SDK, Lua, protocol, native game and artifact identities.
See the [migration guide](docs/migration.md) for verification and the deployment procedure.
