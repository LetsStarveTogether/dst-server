# Don't Starve Together Dedicated Server

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

English | [简体中文](README.zh-Hans.md)

A Rust SDK, Agent and CLI for Don't Starve Together, with Python bindings.
Each room runs in one container with a resident Agent supervising every configured shard.
The Agent starts and stops games according to the room policy and keeps its control socket available between openings.
Published binaries and Python wheels support Linux x86_64 and CPython 3.15.
The control socket is for trusted callers on the same host.

Version `0.4.0` replaced the earlier Python implementation.
The Agent owns daily opening and closing without an external scheduler.
See the [migration guide](docs/migration.md) for API changes, verification and known limitations.
Internal readiness does not establish public network reachability.

Install the Python SDK with `pip install dst-server==0.5.0` in a CPython 3.15 environment.
The [GitHub release](https://github.com/LetsStarveTogether/dst-server/releases/tag/v0.5.0) also includes a native Linux x86_64 CLI, which requires libarchive 3.7 or newer.
Python wheels bundle libarchive and its required libraries, with their copyright and license texts under `.dist-info/licenses/`.

## Build and Create a Room

Install the latest stable Rust, a C/C++ build toolchain, Cap'n Proto, pkg-config, libarchive development files, and [uv](https://docs.astral.sh/uv/).
Host deployment additionally requires Podman with Quadlet and systemd.
From this checkout:

```sh
cargo build --locked --release -p dst-server --bin dst-server
export PATH="$PWD/target/release:$PATH"
uv sync
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
The packaged fleet contains 121 rooms: `000–100` and `200–219`.
Generated room names use the prefix `LST-朗诵团`, followed by the room number and gameplay label.
Room `100`, `LST-朗诵团-100-测试尝鲜`, uses the official Beta game build with the pure endless configuration.
Its forest uses the native `ENDLESS` preset, and its cave uses the matching overrides from the pure endless template.

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
| `logs`, `archive` | Query or follow historical logs; import, export and upload native room saves. |
| `console` | Execute trusted Lua on an explicit shard, from text, a file or interactive input. |
| `config`, `template`, `inspect` | Validate configuration, inspect templates, discover native topology. |
| `scripts`, `annotations`, `completion` | Build or verify script bundles, generate Lua annotations and shell completion. |
| `agent` | Run the room supervisor, normally as the container entry point. |

Commands print JSON; `--json` uses compact JSON and structured errors.
A batch preserves individual results and fails its exit status when any target fails.
JSON arguments accept inline text, `@filename`, or `-` for stdin.

```sh
dst-server --json host list
dst-server host show 299 --field /cluster/settings/max_players
dst-server config validate ShardSettings '{}' --resolved
dst-server host diagnose --room 299
dst-server describe
dst-server call save --socket /srv/dst/299/.dst-agent.sock
dst-server call get_player '{"userid":"KU_example"}' --socket /srv/dst/299/.dst-agent.sock
dst-server subscribe lifecycle --socket /srv/dst/299/.dst-agent.sock
```

`call stop` gracefully closes every shard, then exits the Agent and container with code `0`.
`host stop --room 299` stops the container service, including its Agent.
Stopping the container also stops automatic scheduling until its service is started again.
`call restart` stops and starts games within the same Agent and waits for readiness during opening hours.
`call start` requests policy reconciliation and resets exhausted recovery for an always-open room.
Use `host start` or `host restart` to wait for all shards to become ready.
Outside opening hours, startup leaves the Agent available with the games stopped.
Recovery that remains exhausted is reported as an error.
Stop the service before offline configuration changes:

```sh
dst-server host stop --room 299
dst-server host edit --room 299 --set '/cluster/settings/max_players=9'
dst-server host start --room 299
```

After container exit, `host status` reads the saved policy, current schedule, maintenance and recovery state.
Changing world-generation settings preserves existing saves; `regenerate` creates new worlds.

## Rust and Python SDKs

RPC uses native Cap'n Proto fields for requests, errors, results and subscription records.
Dynamic game and MOD data uses a recursive value union; numbers, text, lists and objects are carried as binary fields.
Rust's `serde_json::Value` and Python dictionaries represent SDK values in memory.

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
Check `status`, or `host status` after container exit, before deciding whether to retry an unknown result.
Save returns `true` when the native command accepts the request; disk persistence may finish later.
Graceful shutdown sends one native `c_shutdown()` to each shard and lets the game perform its final save.
The game may save more than once within its native shutdown path.
Normal shutdown requires exit code zero, process reaping and output drainage, with no force or protocol fault.
Failed shutdown retains each shard's exit status, forced termination flag and output drainage result.
Signal termination uses the negative signal number.
The default stop wait is 220 seconds, including its notice; changing the notice delay adjusts that budget.
Mods that disable saving can accept save requests and stop cleanly without creating a snapshot.
Migration returns the native acceptance boolean; query player location to follow its progress.
Console success means the Lua code returned; an asynchronous effect started by that code may still be running.

SDK-generated unique IDs use canonical uppercase ULIDs from Rust's `ulid` crate.
This covers launch nonces, process instances, events and logs, presence observations, requests and operations.
Service instances use the same format.
Each event keeps the same ID across subscribers, local output, OTLP export and retries.
Process IDs survive world reloads and change when a new process starts.
Presence observation IDs change when the process, world generation or continuous observation changes.
Lua reports its local observation epoch, and Rust assigns the public ULID.
Native game IDs, room numbers, system IDs and OpenTelemetry trace/span IDs follow their owning protocols.

## Configuration and Files

```text
/srv/dst/299/
├── cluster.ini
├── cluster_token.txt
├── adminlist.txt / blocklist.txt / whitelist.txt
├── .dst-server.json
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
`.dst-server.json` stores the template label, policy, activity, recovery state and shared Lua launch options.
Before starting games, the Agent updates its `driver` field and writes generated options to each shard's `dst_server_driver.json`.
Lua reads the shard-local launch file through the native storage API, which rejects paths outside the shard.
An existing `.dst-control.json` is renamed under the room lock on first access.
Published ports are read from the Quadlet; the SDK no longer maintains a separate port registry.
It is not a second copy of game settings.
Keep it across container replacement to retain recovery limits and maintenance progress.
Never remove or replace `.dst-room.lock` while a room exists.

Every directory with `server.ini` is an enabled shard.
Names are arbitrary, with exactly one master.
Keys and IDs must be consistent, with conflict-free game, query and shard ports.
Port allocation uses the actual shard set, an allocation lock and a persistent map; the previous four-shard limit is removed.
Packaged fleet rooms use host UDP ports based on `30000 + room_number * 10`.
Master game/query offsets are `0/1`, and secondary offsets are `2/3`.
A conflicting fixed port fails deployment.
Custom deployments without explicit mappings use the default host allocation pool `30000–65535`.
All shards share the container network and room Mods; each process has separate control pipes and readers.

Python configuration factories validate through Rust:

```python
from dst_server.settings import ClusterSettings, RoomStore, build_template

settings = ClusterSettings(max_players=9, cluster_name="Friday games")
cluster = build_template("pure_endless", number=299, settings=settings)
print(cluster.dump(defaults=True))
print(settings.render())

store = RoomStore("/srv/dst")
room = store.load(299)
updated = room.edit("/cluster/settings/cluster_description", "Friday games")
# After stopping the entire service:
# store.save(updated)
```

`dump()` redacts token, shared key and password; `secrets=True` explicitly returns them.
Rendered native files and `files()` contain real credentials and must be handled accordingly.
Defaults are applied on demand; omitted fields and explicit `None` remain distinct.
Rust types define configuration fields, defaults and INI sections; Serde and `garde` validate input.
`encode_user_path` defaults to `false` and is always written to `server.ini`; an explicit `true` remains enabled.
The packaged presets include 12 templates and all 121 fleet rooms.
Rust owns validation and file operations; Python provides bindings, conversion and asynchronous wrappers.

Offline writes use the shared room lock, reject symlinks and replace files individually.
An I/O failure can leave some files updated; fix the error and reapply the configuration.
General configuration saves preserve live admin, ban and whitelist files.
Use permission operations for online changes.
Structured Lua reads parse Lua 5.1 literals without execution.
Dynamic configurations can be loaded by the game; this API cannot structurally edit them.

## Player Management and Votes

Account operations and votes address the whole room.
`Client` offers named methods; `Host` accepts the same arguments after a room number.

```python
await client.ban("KU_example", seconds=600)
await client.ban("KU_example")  # Replace the timed ban with a permanent ban.
await client.unban("KU_example")
await client.set_admin("KU_example", enabled=True)
await client.whitelist("KU_example")
permissions = await client.read_permissions()
disconnected = await client.kick("KU_example")

commands = await client.list_vote_commands()
status = await client.start_vote(
    "kick", starter_userid="KU_starter", target_userid="KU_target"
)
status = await client.get_vote()
cancelled = await client.cancel_vote()
```

Permission writes return `None`; repeated writes succeed.
`blocklist()`, `is_blocked()`, `is_admin()` and `is_whitelisted()` read their respective state.
`unwhitelist()` removes whitelist membership.
Permanent permission edits also work while games are stopped, or through `Host` with an inactive service.
`read_permissions()`, `blocklist()` and `is_blocked()` read permanent permission lists.
Native temporary bans are separate; `unban()` removes permanent entries and temporary bans expire on their own.
A permanent ban adds a persistent entry, including for a temporarily banned account.
Existing permanent ban timestamps and profile metadata are preserved.
`kick()` checks connections on every shard, including clients choosing or loading a character and migrating clients.
It returns `False` if the client is absent; shard failures or unconfirmed disconnections raise an error.

Votes use the standard native world voter and registered vote commands, including Mod commands.
The starter and any required target must be connected.
Native access restrictions, minimum player count, player age, custom checks and cooldowns apply.
Starting a vote returns its observed state; it does not guarantee that the vote will pass.
`get_vote()` returns `enabled` and nullable `current`, including countdown, options, counts and voter selections.
Selections are one-based; pending and ineligible voters have `selection=None`, with eligibility in `can_vote`.
`cancel_vote()` returns `False` when idle and applies the native cancellation cooldown when active.
Vote control works with telemetry disabled; modes using a different voter return `unsupported`.

## Opening Hours, Maintenance and Recovery

The default policy uses system local time (`timezone: "local"`) and opens all day.
Automatic Mod maintenance is enabled; idle world regeneration is disabled.
Generated Quadlets set `Timezone=local` so the container inherits the host timezone.
Set a policy's `timezone` to an IANA name such as `Asia/Shanghai` to override the schedule timezone.
A policy can be sent to a running Agent:

```sh
dst-server call set_policy - --socket /srv/dst/299/.dst-agent.sock <<'JSON'
{
  "policy": {
    "timezone": "local",
    "schedule": [{"start": "18:00", "end": "00:00"}],
    "mod_auto_update": true,
    "idle_regeneration": false
  }
}
JSON
```

The Agent checks opening hours at startup and every 30 seconds while running, including windows across midnight.
At closing time it stops all games and remains available for the next opening.
Startup outside opening hours keeps the Agent running with no game processes.
Changes to policy times or timezone trigger reconciliation in the same Agent.
Stop the container service to suspend automatic operation.
Pending Mod maintenance completes before games open; enabled automatic maintenance checks Mods at each new opening.
While games run, the Agent checks enabled Workshop items every five minutes against their installed manifests.
Checks run independently of room operations and feed confirmed content changes into the same maintenance queue.
`status.agent.mod_check` reports the last check, unavailable items and errors; failed checks do not stop games.
`call update_mods` queues maintenance in the current Agent and returns before it completes.
With `restart: false`, the Agent exits after the update.
Explicit Mod maintenance may run outside opening hours, but games still follow the schedule.
The default maintenance countdown is 60 seconds with announcements every 30 seconds.
Failed downloads or validation receive two additional attempts, five minutes apart, then leave games closed.
An explicit maintenance request or a later opening can retry.

A required shard crash or sustained control failure stops the whole room.
Abnormal process exits, forced termination and IPC faults persist recovery state and exit with code `1` for container replacement.
Normal game restart and Mod maintenance reuse the Agent.
Generated Quadlets use `Restart=on-failure`; see [resident Agent deployment](docs/migration.md#resident-agent-deployment) when upgrading.
Retryable failures allow two restarts of the current save, 30 seconds apart.
The latest snapshot numbers across all required shards may differ by at most `MAX_SNAPSHOT_GAP = 3`.
Native shard synchronization may change these numbers during a restart.
Only confirmed save-loading failure can then try one previous complete room snapshot.
That target must precede the failed snapshot and be no more than three snapshot numbers behind any shard's latest save.
The limit counts snapshot numbers, including manual saves; it does not measure game days or require `.meta` clock data.
Targets are fixed before mutation; interrupted recovery resumes those same targets.
Recovery state omits empty fields and keeps current snapshot identities only inside a pending retry.
Interrupted rollback rechecks every shard against the fixed target, without a separate progress list.
Unknown state, missing targets, unsupported failure causes or failed recovery leave the room closed.
Thirty minutes of stable operation restores the budget.
An exhausted room keeps its Agent available and gets another opportunity at the next opening.
An always-open room can also recover through explicit start.

Idle regeneration runs only when enabled, during opening hours, with every shard ready and reliably empty.
Its retention periods are 6, 24, 36, 72 and 168 hours according to world age.
Interrupted observations or changed worlds restart the retention period; clean scheduled closure preserves it.

## Logs, Exports and Utilities

The default game event profile is `history`; `agent --profile` also accepts `critical` and `off`.
History keeps work completion and item activity, omitting individual tool swings and ordinary PvE hits.
Combat details cover players and entities attributed to players; deaths and spell actions remain recorded.
History records container transfers as changes to the selected slot; failed attempts and automatic inventory churn are omitted.
Local portals, including wormholes and temporary map exits, emit `dst.player.teleport_started` when native activation succeeds.
Client authentication and disconnection events record `userid`, `steam_id`, `name`, `admin` and `moderator`.
`userid` is the Klei ID; `steam_id` is a SteamID64 string.
`dst.server.presence` includes these client records at startup and every 60 seconds.
Its `players` entries include character `prefab` and `is_ghost`.
Names and permissions refresh from the native client table; disconnection uses the last observation.
A new authentication clears the previous identity.
Names are bounded to 256 UTF-8 bytes; unavailable names, permissions or valid Steam IDs are null.
`list_players` and `get_player` expose the same identity fields, including lobby connections.
`dst.player.loaded` records the observed name, Steam ID, ghost state and `age_seconds`, including shard arrivals.
Death and revival events also include `age_seconds`, the native age component's cumulative age, or null when unavailable.
Presence snapshots include `world` season, phase, moon phase, nightmare phase and weather flags.
Weather flags cover rain, snow, acid rain and lunar hail; caves use their own phase and moon phase.
Equip, unequip and selected container-slot transfers include available item `condition` values.
The fields are `uses_percent`, `fuel_percent`, `freshness_percent` and `armor_percent`.
These values preserve native fractions, normally 0–1; unavailable components are omitted.

Accepted SDK kick, ban, unban, administrator and whitelist mutations emit `dst.admin.operation` after completion.
This includes operations on stopped rooms.
The lifecycle stream and logs record the target `userid`, method, supplied ban duration, operation ID and start time.
They also record status, nullable boolean result and error code.
Status is `success`, `failure` or `unknown`; success describes method completion.
Permission writes have a null result; a false kick result means the client was absent.
Events are bounded and best effort.
Source gaps, subscriber overflow, output loss and export rejection have separate counters.
RPC uses a shared Tokio broadcast cache per stream: the latest 128 records, at most 64 KiB each (8 MiB of payload per stream).
Slow readers report overwritten records through `dropped`; oversized records also count as lost.
Each batch returns at most 16 records (1 MiB), and the Agent accepts up to 128 subscriptions.
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
dst-server logs telemetry '{"since":1790899200,"until":1790985600,"userid":"KU_example"}'
```

Use `userid` to match a player's identity across actors, targets, deaths, votes, presence and SDK administration.
This also works with existing history.
It can be combined with time, service and field filters; `userid` and free-text `query` are mutually exclusive.
Records retain the name observed at that time; older records without the new metadata are not backfilled.

```python
from dst_server.logs import NetdataLogQuery, NetdataLogs

history = await NetdataLogs().query(
    NetdataLogQuery(since=1790899200, until=1790985600, userid="KU_example")
)
```

A follow stream emits one record at a time; interrupting a log query waits for its reader process to exit.
`dst_server.events` lists supported event names with `event_names()` and validates records through `validate_event()`.
Rust enums and payload types define the event contract, including required nulls and exact numeric values.
`dst_server.KleiClient` provides bounded build, region, lobby and room requests with per-query batch failures.
HTTP failures are distinct from successful empty results.
Season, mode, intent and character prefab retain unknown strings from game updates or Mods.
Additional response fields are also preserved.

Rust's [`dst_server::steam`](crates/dst-server/src/steam.rs) provides anonymous Workshop metadata queries.
Call `Client::published_file_details(&[PublishedFileId(...)])` for `GetPublishedFileDetails`.
Each `PublishedFile` retains its ID and a `Result<PublishedFileDetails, ResultCode>`.
Item failures remain distinct from request failures.
Metadata has typed IDs, content handles, UTC timestamps, visibility, URLs and tags, with full 64-bit precision.
Calls accept up to 100 IDs and restore request order, including repeated IDs.
Incomplete or mismatched responses return errors.
Client clones share the HTTP connection pool.
Calls have an 8 MiB response limit, a 30 second total budget and at most two retries for temporary failures.
Retries honor `Retry-After` seconds and all three HTTP date formats within the call budget.
`Error::Transport` carries a `TransportError` category.
Display, debug output and error sources exclude URLs and upstream error text.
The Agent uses this client for Workshop update detection alongside native game notifications.

Archive export reads persistent world/player saves and Mod progress while games keep running.
It captures the selected files in memory and creates a seekable 7z archive using the encoder's LZMA2 level 9 preset.
Compression levels range from 1 to 9; dictionary and match parameters use the encoder's presets.
The archive can be extracted with standard 7-Zip tools.
Each process runs one export compressor at a time; no staging directory or transfer file is written.
Exports include room/shard settings, world generation, Mod configuration, native sessions and `shardindex`.
Recipe progress, worldjump saves, and ReForged achievements and experience are also included.
It excludes downloaded Mods/UGC, `server_temp`, `client_temp`, logs, backups, indexes/profile caches and SDK runtime files.
Source credentials and permission lists are excluded, including when the source has no token file.
Each export generates a fresh random `cluster_key` in `cluster.ini` for all shards to share.
Basic export preserves the source shard names and player path encoding.
The optional `client_compatible=True` (`--client-compatible`) requires exactly two shards: one master and one secondary.
It places them under `Cluster_<number>/Master` and `Cluster_<number>/Caves`.
It enables `encode_user_path` in both `server.ini` and `shardindex` and converts unencoded player directories.
All player snapshots, metadata and `savelocation` records are retained.
Already encoded player directories retain their names.
This option is off by default and cannot be combined with a custom `room_id`.
Mod download lists, activation settings, overrides and the saved `enabled_mods` remain included.
Players install the Mods themselves.
Client hosting does not require the original server token; a dedicated online server needs its own token.
Reading a running room does not force a save or guarantee all files come from the same instant.
Uploads use the native object-store implementation and explicitly abort incomplete multipart uploads on failure or cancellation.
Cleanup failures remain visible in the result and logs.
Python exposes `dst_server.archive.export_cluster` and `ClusterArchive.save/upload`.
Both SDK and CLI read the room directory directly, without requiring an Agent or systemd.
Export returns a memory stream by default; saving and uploading are explicit operations on the same archive.

```python
async with export_cluster(host, 299, client_compatible=True) as archive:
    contents = archive.stream.read()
```

Within that context, use `await archive.save(path)` for a new local file.
Use `await archive.upload(bucket=..., endpoint=...)` for R2/S3.

```sh
dst-server archive export 299 --output /tmp/room-299.7z
dst-server archive export 299 --client-compatible --output /tmp/room-299-client.7z
dst-server archive upload 299 --client-compatible --bucket saves --endpoint "$AWS_ENDPOINT_URL_S3"
```

The output path must not already exist.
`import_cluster(source)` returns an `ImportedCluster` held in memory.
It accepts a directory, archive path, or `ClusterArchive`; format detection uses file contents rather than the filename.
Supported inputs include ZIP (stored/Deflate), 7z (LZMA/LZMA2 and previous Zstandard exports), RAR4/RAR5, and TAR.
TAR supports gzip, bzip2, XZ and Zstandard compression.
RAR5 solid archives are supported; RAR4 solid archives, encryption and multipart archives are unsupported.
Inputs contain one room directly or inside one enclosing directory.
Native settings and shard indexes determine the layout automatically, including custom shard directory names.
INI settings take precedence; indexes supply missing room, world and Mod configuration.
The master is identified from explicit shard settings, the `Master` name, or a unique forest world.
Ambiguous layouts require an explicit `SHARD.is_master` in `server.ini`.
Import preserves shard directory names and restores encoded or raw Klei player directories to `KU_…_` paths.
It sets `encode_user_path=false` in both INI and shard index, retaining all snapshots and metadata.
Import and export share file selection, configuration rendering, index sanitization and path validation.
Imported credentials and permission lists are removed, and each parse generates a fresh random shared `cluster_key`.
Read `imported.configuration`, enumerate `imported.files`, and access bytes with `imported.read_file(path)`.
`await imported.save(destination)` publishes the same parsed files and key to a new directory.
`save()` rejects existing destinations.
The CLI parses in memory with `dst-server archive import SOURCE`; add `--output DESTINATION` to save.
Its JSON result includes the configuration and selected file paths.
Import does not register a service or start the game.
Archives are limited to 100,000 entries and 2 GiB unpacked; native configuration files are limited to 1 MiB each.
Paths inside the room allow at most 16 components, 255 UTF-8 bytes per component and 4096 bytes overall.
An optional enclosing directory is excluded from these limits.
Legacy `saveindex` and client sessions using legacy paths must first be migrated by the game.

```python
from dst_server.archive import import_cluster

imported = await import_cluster("client-save.zip")
configuration = imported.configuration
world = imported.read_file("Master/save/session/0123456789ABCDEF/0000000001")
await imported.save("/tmp/restored")
```

To update an existing room, stop its Agent and all game processes, then call `await imported.apply(path, level="runtime")`.
It returns the resulting configuration and supports three cumulative levels:

| Level | Replaced content |
| --- | --- |
| `data` | World and player saves, snapshots, indexes and saved game progress. |
| `runtime` (default) | `data`, Mod settings and downloads, `modoverrides.lua`, `worldgenoverride.lua` and `leveldataoverride.lua`. |
| `all` | `runtime`, `cluster.ini` and each shard's `server.ini`, including ports. |

Unselected configuration files, existing tokens and permission lists retain their original bytes.
Each selected save directory is replaced completely, and absent imported override files are removed at `runtime` or `all`.
Shard counts must match, and each source shard needs a session index with a nonempty latest native world snapshot.
Snapshot names use ten digits and positive 32-bit IDs; invalid IDs and empty latest snapshots are rejected.
Matching uses directory names, then explicit source IDs, then a unique remaining master/secondary role.
Generated IDs cannot resolve ambiguity; ambiguous matches fail before writing.
Player paths and shard indexes use the resulting destination `encode_user_path` setting.
The room lock prevents applying while its Agent owns the room.
The complete result is staged beside the destination and published in one directory exchange.
This requires a writable parent on the same filesystem.
Run the import from a working directory outside the target room; targets containing the caller's working directory are rejected.
Staging starts with private permissions, and discarded read-only directories are made writable only for cleanup.
Retained directories preserve their access and default ACLs; failure to preserve them aborts publication.
Errors after publication explicitly say `import applied` and identify any retained old directory.
An abandoned Agent socket is discarded after confirming that it has no listener.
Saved Agent run targets, recovery and policy execution state are cleared; policy configuration is retained.
Changing ports with `all` also requires reviewing the deployment's published ports.

```sh
dst-server archive import client-save.zip --into /srv/dst/rooms/007
dst-server archive import client-save.zip --into /srv/dst/rooms/007 --level data
```

`--into` and `--output` are mutually exclusive; `--level` requires `--into`.
CLI destination paths must be valid UTF-8 and are checked before writing.
An apply result includes the destination path, level and effective configuration.

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
cargo nextest run --locked -p dst-server --all-targets
uv run ruff check python tests/rust tools
uv run ty check python
uv run --locked --only-group dev pytest tests/lua
uv build --out-dir dist --no-sources
```

CI builds a wheel from the source distribution and installs it in an isolated environment.
It then runs native binding and process tests.
Lua contracts run under Lua 5.1 and LuaJIT, with each scenario selectable through pytest.
See the [testing guide](docs/testing.md) for tool setup, focused commands and test design principles.
Game build `756039` has an unresolved [native shutdown failure](docs/migration.md#known-native-shutdown-failure) observed with five connected shards.
Gorge and Forge deliberately disable saving; accepted save requests do not establish persistence.
Real game probes use disposable directories and containers; they must not target live rooms.
Build manifests record SDK, Lua, protocol, native game and artifact identities.
See the [migration guide](docs/migration.md) for verification and the deployment procedure.
