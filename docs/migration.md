# Migration Guide

This guide covers the Rust implementation and the resident Agent lifecycle.
[English README](../README.md) · [简体中文 README](../README.zh-Hans.md)

Each room has a resident Agent that owns opening hours, game processes, maintenance and recovery.
Scheduled closure stops the games and keeps the Agent, control socket and room lock available for the next opening.
See [resident Agent deployment](#resident-agent-deployment) when upgrading an existing installation.

## Interface and Deployment Changes

| Area | Current interface and behavior |
| --- | --- |
| Implementation | `dst-server` core crate contains models, validation, operations, files, services, CLI and Agent; `dst-server-python` binds it through PyO3. |
| Python package | `dst_server.Client`, `Host`, `KleiClient`, `dst_server.settings` and `dst_server.logs`; configuration factories return immutable native values. |
| Protocol | Cap'n Proto over a local Unix socket; requests, errors, results and subscription records use native schema types. |
| Scope | One room container, one Agent, one game process per discovered shard. |
| Discovery | Native `cluster.ini` and each enabled shard's `server.ini`, including arbitrary shard names and more than four shards. |
| Quadlet | One `dst-NNN.container` produces `dst-NNN.service`; `RunInit=true` handles container signal forwarding and orphan reaping. |
| Ports | Packaged fleet rooms use `30000 + room_number * 10`, with master game/query offsets `0/1` and secondary offsets `2/3`; allocation validates conflicts and persists the mappings under a host lock. |
| Configuration | Native INI/Lua remain authoritative; `.dst-server.json` holds policy, activity, maintenance, recovery and shared Lua launch options. |
| Ownership | The Agent holds `.dst-room.lock` until container exit; offline writers require exclusive ownership. |
| Socket | `/cluster/.dst-agent.sock` in the container, normally `/srv/dst/NNN/.dst-agent.sock` on the host. |
| Host lifecycle | `host start/stop/restart` operate the whole systemd service; start/restart normally wait for game readiness. |
| Agent lifecycle | Scheduled closure keeps the Agent available; `call restart` restarts games within it; `call start` reconciles policy and permits an explicit recovery retry for an always-open room. |
| Offline status | `host status` exposes saved `policy`, current `schedule`, `maintenance` and `recovery` after Agent exit, with `game: null`. |
| CLI | Native and Python entry points share Rust parsing and dispatch; `describe` provides the current operation schemas. |

The release targets Linux x86_64, CPython 3.15 and trusted callers on the same host.
The socket has mode `0600`; the server checks peer credentials and limits connections.
RPC protocol 2 replaces JSON payload blobs with typed commands and recursive native values.
Protocol 3 changes account and vote requests and uses a new Cap'n Proto interface identity.
Older peers are rejected instead of interpreting `set_admin.enabled` as the previous inverted flag.
Upgrade the Agent and SDK together; the RPC interface has a new ID and requires matching versions.
Dynamic numbers use `Int64`, `UInt64` and `Float64`.
Exact numbers outside those representations use decimal text in a dedicated numeric field.
Game event payloads now use Rust enums and `garde` validation, preserving required nulls and exact JSON numbers.
The `event_schema()` API has been removed; use `event_names()` to list supported names and `validate_event()` to check data.
Presence event `clients` entries now contain `{userid, steam_id, name, admin, moderator}` instead of bare Klei ID strings.
Authentication and disconnection use the same client record; all fields except `userid` accept null when unavailable.
Player queries expose these identity fields, including nullable names and permissions.
Presence players also require `prefab` and `is_ghost`.
Presence requires a `world` object with nullable environment fields.
Loaded events require nullable `steam_id`, `name` and `age_seconds`, plus `is_ghost`.
Death and revival events require nullable `age_seconds`.
Item references may carry an optional `condition` object with available native condition fractions.
Update event consumers together with the Agent and game scripts; historical records are not rewritten.
SDK administration emits `dst.admin.operation` through lifecycle subscriptions and logs, including while games are stopped.
This operational record is separate from the Lua game events validated by `validate_event()`.
`NetdataLogQuery(userid="KU_example", since=...)` searches player identity roles in new and historical logs.
It cannot be combined with `query`.
Archive export reads the room directory directly and builds its 7z/LZMA2 archive in memory, including while games run.
Export no longer uses Agent `export_archive` / `release_archive` requests or transfer artifacts.
Callers need read access to the native room directory.
Basic exports now preserve native shard names and player path encoding.
Every export generates a fresh random shared shard key in `cluster.ini`.
The former `encode_user_path` export argument is replaced by `client_compatible`, which defaults to `false`.
Client export requires exactly one master and one secondary.
It writes `Cluster_<number>/Master` and `Cluster_<number>/Caves`.
It converts player paths together with their INI/index flags.
It cannot be combined with a custom `room_id`.
Rust callers pass the room number to `archive::prepare(reader, number, options)`.
Both modes retain Mod configuration and return an archive in memory; disk saving and R2/S3 upload remain explicit operations.
`archive::import_cluster(ImportSource)` parses one room into an `ImportedCluster` in memory.
It accepts directories, ZIP, 7z, RAR4/RAR5 and compressed TAR streams, detecting the format from contents.
ZIP supports stored/Deflate; 7z supports LZMA/LZMA2 and previous Zstandard exports.
TAR supports gzip, bzip2, XZ and Zstandard; RAR5 supports solid archives.
RAR4 solid archives, encrypted archives and multipart archives are unsupported.
Python exposes `import_cluster(source)`, returning the configuration, file paths and `read_file(path)` access.
A `ClusterArchive` can be passed directly without saving it first.
Call `await imported.save(destination)` in Python or `imported.save(destination)` in Rust to publish a new directory.
The CLI uses `archive import SOURCE [--output DESTINATION]`, with no disk output by default.
To update an existing, stopped room, use `await imported.apply(path, level="runtime")` in Python.
Rust uses `imported.apply(path, ImportLevel::Runtime)`.
The CLI uses `archive import SOURCE --into PATH [--level data|runtime|all]`.
CLI destination paths must be valid UTF-8 and are validated before writing.
The cumulative levels replace saves (`data`) and add Mod/world/level overrides (`runtime`, the default).
The `all` level also replaces `cluster.ini` and `server.ini`.
Unselected configuration files, tokens and permission lists retain their bytes.
Save directories are replaced completely, and missing selected overrides are removed.
Shard counts must match; names, explicit source IDs, then unique remaining roles determine the mapping.
Automatically assigned IDs never resolve ambiguity.
Each shard needs an index with a nonempty latest native world snapshot; incomplete saves and ambiguous matches are rejected.
Snapshot names use ten digits and positive 32-bit IDs; invalid IDs and empty latest snapshots are rejected.
Player path encoding follows the resulting destination settings.
The Agent and game processes must exit first; the room lock excludes concurrent managed writes.
Applying stages the result beside the room and exchanges directories atomically, retaining the lock inode.
It requires a writable parent on the same filesystem.
The caller's working directory must be outside the target room to survive the exchange and cleanup.
Staging is private from creation, and discarded directory copies are made writable for cleanup.
Retained directories preserve their access and default ACLs; preservation errors abort publication.
Post-publication errors explicitly report that the import was applied and identify retained old files.
Stale Agent sockets are discarded only after confirming that they have no listener.
It clears saved Agent run targets, recovery and policy execution state while retaining policy configuration.
An `all` import can change ports, so deployment port mappings need review.
The old destination argument, `ImportOptions` and `from_client` switch are removed.
Both directions share native file filtering, configuration rendering, index sanitization and path validation.
Import accepts one room at the root or one directory below it; multiple rooms are rejected.
The `save()` method requires a new destination.
INI files and shard indexes supply settings automatically, including custom shard directory names.
INI settings take precedence and directory names are preserved.
Explicit roles, the `Master` name or a unique forest world determine the master; ambiguous roles are rejected.
Encoded and raw player paths become case-sensitive `KU_…_` paths, with `encode_user_path=false` in INI and index.
Imported credentials and permission lists are removed; each parse generates a fresh shared shard key.
Saving the parsed result retains that same key.
Source builds require pkg-config and libarchive development files; native binaries require libarchive 3.7 or newer.
Python wheels include the required shared libraries and their copyright and license texts under `.dist-info/licenses/`.
Service registration and game startup remain separate host operations.
There is no public RPC listener or remotely authenticated control service.

The old Python API and command groups are replaced.
Translate calls using `describe()` and the native configuration models.
Importing former controller or per-shard Agent classes is unsupported.
Use `host run` for shared host request objects, for example:

```sh
dst-server host run '{"operation":"call","request":{"method":"whitelist","arguments":{"userid":"KU_example"}}}' --room 299
dst-server host run '{"operation":"update_mods","restart":true}' --room 299
```

CLI output is JSON, with compact output and structured errors under `--json`.
Selecting `--room 000-009,299`, `--template NAME` or `--all` is explicit.
A batch includes each result and returns a failing exit status if any operation fails.
`Host.list()` and `host list` use the same ordered batch outcomes: `{number, result: {ok, value/error}}`.
Read a successful room's status from `entry["result"]["value"]`; a failed room remains in the list.

## Configuration Migration

Rust configuration types define fields, defaults, constraints and INI sections together.
Serde checks the input structure, and `garde` checks field constraints before the native relationship checks run.
Use `dst-server config validate MODEL VALUE --resolved` to validate input and inspect its effective defaults.
The configuration Schema APIs and `config schema` / `host schema` commands have been removed.
The packaged resources contain gameplay presets and 12 templates.
The packaged fleet contains 121 rooms numbered `000–100` and `200–219`.
Shared presets define each room's gameplay family and schedule.
Room `100` uses the Beta game build and pure endless template under the same room and port rules.

Configuration preserves missing fields separately from explicit nulls.
INI booleans and integers are validated; fractional values cannot silently become integer IDs or ports.
`encode_user_path` defaults to `false` and every generated `server.ini` contains the setting.
Offline saves and archive normalization also write it explicitly.
An explicitly configured `true` is preserved except when an archive conversion requests a different target layout.
Lua literals are statically parsed as Lua 5.1 with bounds on input size, tokens and nesting.
Duplicate keys, mixed table shapes, executable expressions and unrepresentable numbers are rejected.
Dynamic Lua accepted by the game remains a game input.
Structured editing reports unsupported input instead of executing it.
Ambiguous world tables require explicit `world_kinds` or `level_kinds` when loading through Python.

`dump()` and normal model serialization redact tokens, passwords and shared keys.
Use `dump(secrets=True)` only when a complete configuration is needed.
`files()`, rendering and native writes contain real credentials.
General configuration edits preserve existing permission lists and shared keys.
Replacing permission files requires explicit offline intent.
Disabling a shard removes its active configuration while preserving its saved worlds.
Host edits write native configuration, control policy and the Quadlet separately.
After an edit fails, read the resulting configuration before retrying.
Configuration files are replaced individually; interrupted edits are reapplied by the caller.

A policy now has this shape:

```json
{
  "timezone": "local",
  "schedule": [{"start": "18:00", "end": "00:00"}],
  "mod_auto_update": true,
  "idle_regeneration": false
}
```

`.dst-server.json` stores it under `policy` alongside the Agent's durable state.
The SDK renames the former `.dst-control.json` under the room lock, preserving its contents.
If both names exist, resolve the conflicting files before opening the room.
Before launch, the Agent writes the shared `driver` object to each shard's `dst_server_driver.json` for Lua.
The native storage API rejects traversal to the room-level state file.
These files contain only generated driver options and are replaced on every launch.
The Quadlet owns published port mappings, replacing `.dst-ports.json`.
Allocation keeps the host lock until the Quadlet is saved.
Omitting `timezone`, or setting it to `local`, uses system local time.
An explicit IANA timezone remains supported.
Existing policies with `timezone: "Asia/Shanghai"` keep that override until changed to `local`.
Translate legacy `schedule` and `recycle` settings explicitly.
Policy times and timezone are the only schedule configuration.
Use `host stop` to stop the resident Agent and suspend automatic operation.
Never delete the new recovery state to force a retry; use the supported start or maintenance operations.
Load-failure evidence in `agent_run.failure` now uses `{kind: "load_failure", shards: {name: {session_id, world_file}}}`.
Saved evidence in the former single-shard format requires migration before upgrading; it is not accepted automatically.

Later deployment of an existing room requires these steps:

1. Stop the old Pod and every old shard unit, then confirm all old processes have exited.
2. Confirm the existing backup policy covers the room data and record the previous image reference.
3. Validate the native topology, convert its policy, and review the new single-container Quadlet and port mappings.
4. Grant the container account access to the room directory and shared `mods` bind mount.
5. Start the new service with exclusive ownership of the directory, then verify all shards and player connections.
6. Retire the old units after the deployment checks; never run old and new writers against the same saves.

This repository does not automatically replace an existing Pod installation.
Validate the generated services and each room's readiness during deployment.

## Resident Agent Deployment

Upgrade the Agent image and SDK together.
Keep `Timezone=local` in the Quadlet's `[Container]` section and `Restart=on-failure` in `[Service]`.
Remove the former `SuccessExitStatus=8` and `RestartForceExitStatus=8` directives.
Normal restart and Mod maintenance now run within the Agent.
Disable any previously installed room opening or closing timers so the Agent owns the complete schedule.
Run `systemctl daemon-reload` after updating the Quadlet.
The running Agent needs a service restart to use the new image; reloading systemd alone does not replace it.

The Agent stays available when the schedule is closed, Mod maintenance has failed, or recovery is exhausted.
It checks the policy on startup, every 30 seconds, and after configuration or maintenance changes.
At an opening it completes pending maintenance and recovery before starting all configured shards.
At closure it saves and stops all games, keeping the container and control socket available.
Changing policy times or timezone takes effect through the same scheduler.

`host start` and `host restart` wait for all shards to be ready during opening hours.
Outside opening hours they wait for an available Agent with stopped games.
Use `host stop` to stop the container and suspend automatic operation.
`call stop` also shuts down the Agent after stopping games.
These explicit stops exit with code `0`.
Abnormal game exits, forced termination and persistent control faults exit with code `1` for recovery in a fresh container.

`call restart` normally waits for replacement games to become ready within the same Agent.
`call update_mods` persists the maintenance request and replies with `queued: true`.
The current Agent runs the updater and then starts games if requested and permitted by the schedule.
With `restart: false`, it exits after completing maintenance.
A queued maintenance reply does not confirm game readiness; use Host operations to wait for the result.

## Operations, Closure and Recovery

| Operation | Confirmation required |
| --- | --- |
| Host start or restart | Every configured shard loaded, control available, expected shard links ready; outside opening hours, an available Agent with stopped games. |
| Save | The native command accepts the request and returns `true`; persistence can finish later. |
| Stop | Every child exited with code zero, was reaped without forced termination, and drained its output without a protocol fault. |
| Reset or rollback | Requested worlds reloaded and the new runtime generation is ready. |
| Regenerate | New world identities and complete room readiness. |
| Player migration | The native command returns its acceptance boolean; use player lookup to observe arrival. |
| Player modification | Execution result from the identified player; lost confirmation becomes an unknown outcome. |
| Console | The submitted Lua chunk returned; asynchronous game work needs its own confirmation. |

The Agent owns accepted operations independently of client waiters.
The default stop wait is 220 seconds, including its 60-second notice; a different notice delay adjusts that budget.
Shutdown sends one native `c_shutdown()` per shard, allowing the game to perform its final save.
It then waits for exit, process reaping and output drainage.
Mods that disable saving can accept save requests and stop cleanly without creating a snapshot.
An abnormal exit or forced termination fails normal Stop.
Its error details retain every shard's exit status, forced termination flag and output drainage result.
Signal termination uses the negative signal number, such as `-6` for SIGABRT.
Explicit Kill succeeds when every child is reaped and its output drained, and reports forced termination.
Save and stop results do not confirm disk persistence.
There is one concurrent mutation per room; queries remain available and conflicts return `busy`.
A timed-out or disconnected mutation is never automatically resent.
Current operation and last result are process-local status, not a durable replay queue.
Player lookup distinguishes disconnected, loading, migrating and conflicting identities.
Entity references bind world session and runtime generation.
While games are running, ban, unban, blocklist and permission checks use the native master as their authoritative source.
A build `756039` test found that a secondary forwards `Ban` but retains a stale local blacklist until reloading permissions.
Unban changes a local list, so writing independent cached lists could replace newer shared data.
Account operations now require room scope; naming a shard is rejected.
While games are stopped, the Agent handles permanent bans and whitelist edits under the room lock.
Timed bans require running games.
`Host.permission()` and the `permission` host operation are removed.
Use `ban`, `unban`, `whitelist`, `unwhitelist`, `set_admin` and the separate query methods on `Client` or `Host`.
`Host.call()` uses the shared permission implementation when the service is inactive.
`set_admin` requires `enabled`, replacing the inverted `remove` argument.
Permission writes return null (`None` in Python), including repeated writes; queries return the current lists or membership.
A permanent ban adds a persistent entry even when the account has a native temporary ban.
Native temporary bans are absent from `GetBlacklist()` and permission files.
Blacklist queries cover permanent bans; `unban` removes permanent entries and temporary bans expire independently.
Existing permanent ban timestamps and profile metadata are retained.
`kick` targets client connections across all shards and no longer requires a loaded player entity.
It returns a boolean for confirmed disconnection or absence, and errors for incomplete or partial outcomes.

`list_vote_commands`, `get_vote`, `start_vote` and `cancel_vote` operate on the master world voter.
`start_vote` takes `command`, `starter_userid` and an optional `target_userid` required by user-targeted commands.
Native vote restrictions and cancellation cooldowns apply; no ballots or passing result are fabricated.
The response contains `enabled` and nullable `current`, with countdown, options and voters.
Vote selections are one-based, or null for pending and ineligible voters; `can_vote` distinguishes eligibility.
Control state is maintained even with telemetry off.
Custom modes without the standard world voter return `unsupported`.
Upgrade the Agent, Lua bundle and Python SDK together for these request and return-value changes.

A required shard failure stops the entire room.
Scheduled closure keeps the Agent running; explicit stop exits the container with code `0`.
Abnormal exits, forced termination or an IPC fault require a fresh container, with the recovery decision persisted first.
Known configuration, disk, permission or Mod problems remain closed and report the cause.
Retryable faults receive two current-save restarts, 30 seconds apart.
Current saves may differ by at most `MAX_SNAPSHOT_GAP = 3` across all required shards.
Larger gaps remain closed with `snapshot_gap_exceeded`.
Snapshot changes during a started attempt do not block the next retry.
Startup also waits through the world reload caused by native shard synchronization.
An unstarted retry still requires its recorded saves.
After those fail, only a proven load failure may try one previous complete room snapshot.
Every target must exist in the same session and precede the failed snapshot.
It must be no more than three snapshot numbers behind any shard's latest save.
This replaces the one-game-day limit and its dependency on `.meta` clock data.
Snapshot numbers also advance for manual saves, so this limit does not guarantee a particular number of game days.
Recovery records absolute targets before applying them and resumes those same targets after interruption.
The compact JSON stores current save identities inside `retry` and omits absent optional fields.
Preparation rechecks all shards and records completion once, after every native receipt is verified.
It never repeatedly reinterprets "previous snapshot".

Thirty stable minutes restore the recovery budget.
An exhausted room keeps its Agent available; a scheduled room gets a new opportunity at the next opening.
Repeated checks within the same opening do not reset it.
An always-open room requires an explicit Host start after exhaustion.
Container startup reads the persisted recovery state and preserves its remaining budget.
After container exit, use Host status to inspect that state and the current schedule.

Schedules are checked at startup and every 30 seconds while running.
They use system local time unless an IANA timezone is explicitly configured.
Clock baselines persist between policy ticks and reset when the Agent restarts.
With automatic Mod maintenance enabled, setup code queues a download before the first opening.
Changing the Mod setup through Configure also queues maintenance before reopening.
While games run, a separate task checks enabled Workshop items every five minutes, with a 30-second check budget.
It compares installed content handles and update times; cached remote details do not prove installation.
Unavailable or banned items do not trigger maintenance, and network errors appear in `status.agent.mod_check`.
Observations crossed by room operations, installation changes or policy changes are discarded.
Default Mod announcements use a 60-second countdown and 30-second intervals.
One native download attempt has a 30-minute budget; policy permits two extra attempts five minutes apart.
Closing takes precedence over automatic maintenance, which remains queued for the next opening.
Explicit maintenance may run outside opening hours; the Agent waits for the next opening when games cannot open.
Only `DST_SERVER_MOD_PROXY` supplies the native updater proxy.
Idle regeneration is off by default and requires an open, ready, reliably empty room.
Its world-age retention periods are 6, 24, 36, 72 and 168 hours.

## Feature Mapping and Reproducible Checks

These entries identify implementation and focused verification.
They do not imply that every real game mode, real player interaction or production load has passed acceptance.

| Capability | Rust entry points | Python / CLI | Focused evidence |
| --- | --- | --- | --- |
| Configuration and defaults | `settings`, `configuration`, `files` | `dst_server.settings`, `config`, `inspect` | 12 template native-file round trips; strict numbers; secrets; symlink rejection; partial writes and explicit retries. |
| Rooms and presets | `rooms::RoomStore`, `rooms::build_template` | `Room`, `RoomStore`, `build_template`, `fleet_room` | All 121 slots; JSON Pointer edits; native Room validation; permission and save preservation. |
| Host and deployment | `host`, `deployment`, `host_operations` | `Host`, `host` | Quadlet generation, allocation persistence, subprocess fixtures, batch results and diagnostics. |
| Game and player operations | `model`, `room`, `driver`, `rpc` | `Client.call`, `call`, `describe`, `console` | Model/pipe/RPC contracts; Agent lifecycle tests; isolated real-game 1/2/5-world smoke. |
| Save and recovery | `recovery`, `preloader`; Lua load observer | `save`, `rollback*`, lifecycle status | Fixed-target idempotence, invalid-session refusal, player restoration and native catalog checks. |
| Mods and policy | `mods`, `policy`, `agent` | `update_mods`, `set_policy`, Host operations | Native updater subprocess ownership; cancellation/reaping; windows, retry limits and closure decisions. |
| Archives and upload | `archive::prepare`, `archive::import_cluster`, `PreparedArchive::compress`, `ClusterArchive::start_upload` | `dst_server.archive.export_cluster/import_cluster`, `ClusterArchive`, `archive import/export/upload` | Memory parsing and explicit saving; ZIP/7z/RAR/TAR decoding; automatic configuration reconstruction; progress and encoding; unsafe paths and existing destinations; export while running; cache exclusions; RAM streams; sanitization; multipart success, failure and abort. |
| Live events and OTLP | `events`, `telemetry`, `observability`, `rpc::EventHub` | `Client.subscribe`, `subscribe`, `event_names`, `validate_event` | gRPC loopback type preservation, retry/RetryInfo, partial rejection, overflow and shutdown. |
| Historical logs | `logs` | `JournalLogs`, `NetdataLogs`, `Host.journal`, `logs journal/telemetry` | Cursor/limit/error handling, native process cancellation and reaping. |
| Klei services | `external::KleiClient` | `KleiClient` | Local HTTP fixtures for bounds, errors, authentication and per-query batch outcomes. |
| Lua utilities | `lua`, `scripts`, `annotations` | `dst_server.settings`, `scripts`, `annotations` | Static literal bounds; ZIP duplicate/path/hash checks; repeatable upgrades; annotation return inference. |

Core tests live in [`crates/dst-server/tests`](../crates/dst-server/tests) and module unit tests.
Python installation checks live in [`tests/rust`](../tests/rust).
The [testing guide](testing.md) describes nextest, Lua scenario selection and test design principles.
The CLI examples in the READMEs were checked against the actual `--help` output.
No external service or game operation is triggered merely to check a help example.

## Native Verification

[`tests/rust/native_acceptance.py`](../tests/rust/native_acceptance.py) checks native 1/2/5-world save, stop and restart.
It also checks absolute snapshot restoration, repeat restoration, invalid-session refusal and synthetic player recovery.
[`tests/rust/native_lifecycle.py`](../tests/rust/native_lifecycle.py) exercises Agent termination, child cleanup and recovery limits.
[`tests/rust/probe_modes.py`](../tests/rust/probe_modes.py) checks templates against the real game and their enabled Mods.
Gorge and Forge deliberately disable saving; mode checks validate accepted requests and clean closure.
[`tests/rust/run_filesystem_failures.py`](../tests/rust/run_filesystem_failures.py) exercises real EFBIG and ENOSPC failures.
It checks partial writes and explicit retries.
Run native checks in disposable directories and containers.
Synthetic players and simulated fleets establish their tested behaviors; connected player interactions require actual clients.

## Known Native Shutdown Failure

Game build `756039` has intermittently crashed while closing a room with five connected shards.
The observed sequence is a requested Stop, the native `Shutting down` log, then SIGABRT (`-6`) or SIGSEGV (`-11`).
SIGABRT cases reported `corrupted double-linked list` during native exit and memory cleanup.
Some crash stacks reached `RakString.cpp` cleanup; others retained only libc `exit` and `free` frames.
These locations identify where corruption was detected; the earlier invalid memory operation is unknown.

The failure reproduced with the original Steam game and Lua bundle, no SDK Agent, no Mods and no connected players.
Waiting for native initialization and every command's completion did not prevent it.
Separate shard containers, freshly generated native worlds and sequential shutdown each reproduced it in separate controls.
Both the former Python shutdown path and the Rust shutdown path encountered native failures.
Ten two-shard cycles completed with all 20 exits zero, which is insufficient to establish a safe shard-count threshold.
The game build, shared runtime, network environment and shard interactions remain possible contributors.
No validated correction or specific upstream cause has been established.

The Agent reports abnormal Stop as failure and requires a fresh container.
It retains exit signals and process cleanup results.
A crash alone does not establish save corruption; inspect the native snapshot catalog when persistence matters.
Short successful runs do not establish that this intermittent failure is fixed.
SDK acceptance covers correct error reporting and cleanup when a game process crashes.

## Build and Release Resources

The Rust toolchain and Cargo lockfile are checked in.
The Python package has no runtime Python business dependencies.
Lua resources, the Cap'n Proto protocol schema and gameplay presets are compiled or packaged with the SDK.
Container builds install the native Agent and patch the game script ZIP using that same revision.
Certificate roots and timezone data are supplied by the runtime image and linked libraries.

[`tools/build_manifest.py`](../tools/build_manifest.py) records SDK/Python versions and source revisions.
It also records Lua, protocol and artifact digests, plus the game version.
The `distributions` container target builds native and Python artifacts; the `game` target creates the room runtime image.
CI checks Rust format/lints/tests, Lua 5.1 and LuaJIT contracts, and Python wrappers.
It also checks source distribution/wheel installation and CLI discovery.
Optional CI jobs run native game probes in disposable containers.
Use the deployment procedure above when replacing an existing installation.
