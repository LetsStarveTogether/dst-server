# Migration Guide

This guide covers the `0.4.0` Rust rewrite and the `0.5.0` container lifecycle changes.
[English README](../README.md) · [简体中文 README](../README.zh-Hans.md)

Upgrading from `0.4.x` requires the [container exit configuration](#container-exit-contract).
Scheduled rooms also require an [external opening timer](#external-opening-timer).

An external scheduler opens room containers.
Each Agent enforces closing hours, performs maintenance and supervises one game run.
Closing the room exits its Agent and container.
Existing installations require the deployment procedure below.

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
| Agent lifecycle | `call stop` exits the container; `call restart` requests a fresh container; `call start` only reports an already running room. |
| Offline status | `host status` exposes saved `policy`, current `schedule`, `maintenance` and `recovery` after Agent exit, with `game: null`. |
| CLI | Native and Python entry points share Rust parsing and dispatch; `describe` provides the current operation schemas. |

The release targets Linux x86_64, CPython 3.14 and trusted callers on the same host.
The socket has mode `0600`; the server checks peer credentials and limits connections.
RPC protocol 2 replaces JSON payload blobs with typed commands and recursive native values.
Upgrade the Agent and SDK together; the RPC interface has a new ID and requires matching versions.
Dynamic numbers use `Int64`, `UInt64` and `Float64`.
Exact numbers outside those representations use decimal text in a dedicated numeric field.
Game event payloads now use Rust enums and `garde` validation, preserving required nulls and exact JSON numbers.
The `event_schema()` API has been removed; use `event_names()` to list supported names and `validate_event()` to check data.
Archive export reads the room directory directly and builds its 7z/Zstd archive in memory, including while games run.
Export no longer uses Agent `export_archive` / `release_archive` requests or transfer artifacts.
Callers need read access to the native room directory.
Basic exports now preserve native shard names and player path encoding.
The former `encode_user_path` export argument is replaced by `client_compatible`, which defaults to `false`.
Client export requires exactly one master and one secondary.
It writes `Cluster_<number>/Master` and `Cluster_<number>/Caves`.
It converts player paths together with their INI/index flags.
It cannot be combined with a custom `room_id`.
Rust callers pass the room number to `archive::prepare(reader, number, options)`.
Both modes retain Mod configuration and return an archive in memory; disk saving and R2/S3 upload remain explicit operations.
`archive::import_cluster(ImportSource, destination, ImportOptions)` rebuilds a new native directory.
It accepts a directory, ZIP or 7z stream.
Python exposes `import_cluster(source, destination, from_client=False)`, returning a `Configuration`.
A `ClusterArchive` can be passed directly without saving it first.
The CLI uses `archive import SOURCE --output DESTINATION [--from-client]`.
Both directions share native file filtering, configuration rendering, index sanitization and path validation.
Import accepts one room at the root or one directory below it; multiple rooms and existing destinations are rejected.
Client import accepts `Master` and optional `Caves` and reconstructs missing configuration from shard indexes.
It maps shard directories and configuration names to `forest` and `cave`.
It restores case-sensitive `KU_…_` player paths and sets `encode_user_path=false` in both INI and shard index.
Imported credentials and permission lists are removed; the new directory gets a fresh shared shard key.
Service registration and game startup remain separate host operations.
There is no public RPC listener or remotely authenticated control service.

The old Python API and command groups are replaced.
Translate calls using `describe()` and the native configuration models.
Importing former controller or per-shard Agent classes is unsupported.
Use `host run` for shared host request objects, for example:

```sh
dst-server host run '{"operation":"permission","kind":"whitelist","userid":"KU_example"}' --room 299
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
The external scheduler can start the service after any stop, including `host stop`.
Manage that scheduler separately when its next trigger should be suppressed.
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

## Container Exit Contract

Before upgrading an existing room, stop its service and confirm the container has exited.
Add `Timezone=local` to its `[Container]` section so Podman gives the container the host timezone.
Newly generated Quadlets include this setting; managed files without it fail deployment validation.
In the existing `dst-NNN.container` file, add or replace these two lines in `[Service]`, keeping its existing `Restart=on-failure`:

```ini
SuccessExitStatus=8
RestartForceExitStatus=8
```

Upgrade the Agent image and these directives together, then run `systemctl daemon-reload`.
Then start the room, for example with `dst-server host start --room 299`.
Newly generated Quadlets already contain both directives.
Existing files without both `8` directives fail deployment validation.
Code `0` closes the container; the external scheduler can start it later.
Code `8` requests an intentional replacement.
This is a project convention chosen to avoid systemd's predefined LSB status names for codes `2` through `7`.
The new container follows the complete startup path for readiness, pending maintenance and recovery.
Code `1` records a runtime fault for recovery in a fresh container.

`call restart` replies after stopping the current games and requesting replacement.
`call update_mods` persists the maintenance request and replies with `queued: true`.
The next container runs the updater and then starts games if requested and currently permitted.
With `restart: false`, it exits after completing maintenance.
These replies do not confirm that the next container is ready.
Use `host start`, `host restart` or `host wait` when game readiness is required.
Startup outside opening hours exits normally before launching games, and Host reports the closed schedule.
Host reports recovery that remains exhausted or unsafe as an error.

## External Opening Timer

For the `18:00–00:00` policy above, create `/etc/systemd/system/dst-299-opening.timer` on the host:

```ini
[Unit]
Description=Open DST room 299

[Timer]
OnCalendar=*-*-* 18:00:00
Unit=dst-299.service
Persistent=true

[Install]
WantedBy=timers.target
```

The timer activates the existing Quadlet service directly.
Enable and inspect it with:

```sh
systemd-analyze calendar '*-*-* 18:00:00'
systemctl daemon-reload
systemctl enable --now dst-299-opening.timer
systemctl list-timers dst-299-opening.timer
```

For user services, place the timer in `~/.config/systemd/user/` and use `systemctl --user`.
Configure an `OnCalendar` entry for each opening and update the timer separately whenever policy times or timezone change.
This example uses the host's local time.
When the policy specifies an IANA timezone, append that same timezone to `OnCalendar`.
Calendar expressions accept an IANA timezone, as documented in [systemd.time](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html).
`Persistent=true` catches up a missed activation when the timer becomes active again.
The Agent still checks the current opening hours before starting games.
A timer trigger leaves an already active service running, as documented in [systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html).
Either `call stop` or `host stop` leaves this timer enabled for its next activation.
To suspend future openings, manage the timer directly with `systemctl disable --now dst-299-opening.timer`.
Enabling it again may catch up a missed activation.

## Operations, Closure and Recovery

| Operation | Confirmation required |
| --- | --- |
| Host start or restart | Every configured shard loaded, control available, expected shard links ready; outside opening hours, successful exit with the room closed. |
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
Ban, unban, blocklist and permission checks use the native master as their authoritative source.
A build `756039` test found that a secondary forwards `Ban` but retains a stale local blacklist until reloading permissions.
Unban changes a local list, so writing independent cached lists could replace newer shared data.
The SDK routes these operations through the master even when the request names a secondary.

A required shard failure stops the entire room.
Normal closure exits the Agent and container with code `0`.
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
An exhausted scheduled room gets a new opportunity at the next opening.
Repeated checks within the same opening do not reset it.
An always-open room requires an explicit Host start after exhaustion.
Container startup reads the persisted recovery state and preserves its remaining budget.
After container exit, use Host status to inspect that state and the current schedule.

Schedules are checked at startup and every 30 seconds while running.
They use system local time unless an IANA timezone is explicitly configured.
Clock baselines persist between policy ticks and reset when the Agent restarts.
With automatic Mod maintenance enabled, setup code queues a download before the first opening.
Changing the Mod setup through Configure also queues maintenance before reopening.
Default Mod announcements use a 60-second countdown and 30-second intervals.
One native download attempt has a 30-minute budget; policy permits two extra attempts five minutes apart.
Closing takes precedence over automatic maintenance, which remains queued for the next opening.
Explicit maintenance may run outside opening hours and exits after updating if games cannot open.
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
| Archives and upload | `archive::prepare`, `archive::import_cluster`, `PreparedArchive::compress`, `ClusterArchive::start_upload` | `dst_server.archive.export_cluster/import_cluster`, `ClusterArchive`, `archive import/export/upload` | Native directory and ZIP/7z round trips; client configuration reconstruction; progress and encoding; unsafe paths and existing destinations; export while running; cache exclusions; RAM streams; sanitization; multipart success, failure and abort. |
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
