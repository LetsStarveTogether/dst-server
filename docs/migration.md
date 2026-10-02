# Rust Migration

This guide describes the `0.4.0` Rust rewrite.
[English README](../README.md) · [简体中文 README](../README.zh-Hans.md)

Schedules, Mod maintenance and idle regeneration run in the resident Agent.
Existing installations require the deployment procedure below.

## Interface and Deployment Changes

| Area | Current interface and behavior |
| --- | --- |
| Implementation | `dst-server` core crate contains models, validation, operations, files, services, CLI and Agent; `dst-server-python` binds it through PyO3. |
| Python package | `dst_server.Client`, `Host`, `KleiClient`, `dst_server.settings` and `dst_server.logs`; configuration factories return immutable native values. |
| Protocol | Cap'n Proto over a local Unix socket; clients see native request/result types. |
| Scope | One room container, one Agent, one game process per discovered shard. |
| Discovery | Native `cluster.ini` and each enabled shard's `server.ini`, including arbitrary shard names and more than four shards. |
| Quadlet | One `dst-NNN.container` produces `dst-NNN.service`; `RunInit=true` handles container signal forwarding and orphan reaping. |
| Ports | Allocate the actual required UDP publications from a locked host pool, then persist them; explicit mappings are supported. |
| Configuration | Native INI/Lua remain authoritative; `.dst-control.json` holds policy, activity, maintenance and recovery state. |
| Ownership | The Agent holds `.dst-room.lock` for its lifetime, including closure; offline writers require exclusive ownership. |
| Socket | `/cluster/.dst-agent.sock` in the container, normally `/srv/dst/NNN/.dst-agent.sock` on the host. |
| Host lifecycle | `host start/stop/restart` operate the whole systemd service. |
| Game lifecycle | `call start/stop/restart` operate games through the resident Agent. |
| CLI | Native and Python entry points share Rust parsing and dispatch; `describe` provides the current operation schemas. |

The release targets Linux x86_64, CPython 3.14 and trusted callers on the same host.
The socket has mode `0600`; the server checks peer credentials and limits connections.
Archive transfers use local artifacts, so mounting only the socket is insufficient for retrieving an exported archive.
There is no public RPC listener or remotely authenticated control service.

The old Python API and command groups are replaced.
Translate calls using `describe()` and the new typed configuration schema.
Importing former controller or per-shard Agent classes is unsupported.
Use `host run` for shared host request objects, for example:

```sh
dst-server host run '{"operation":"permission","kind":"whitelist","userid":"KU_example"}' --room 299
dst-server host run '{"operation":"update_mods","restart":true}' --room 299
```

CLI output is JSON, with compact output and structured errors under `--json`.
Selecting `--room 000-009,299`, `--template NAME` or `--all` is explicit.
A batch includes each result and returns a failing exit status if any operation fails.

## Configuration Migration

The packaged schemas and defaults define the new contract.
Use `dst-server config schema ClusterConfig` or `dst-server host schema` to inspect it.
Schemas, defaults, INI mappings and 12 templates are embedded in Rust resources.
The 120 fleet definitions retain their room numbers, gameplay families and schedules.

Configuration preserves missing fields separately from explicit nulls.
INI booleans and integers are validated; fractional values cannot silently become integer IDs or ports.
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
The Agent recovers published native configuration transactions before game startup.

A policy now has this shape:

```json
{
  "timezone": "Asia/Shanghai",
  "schedule": [{"start": "18:00", "end": "00:00"}],
  "mod_auto_update": true,
  "idle_regeneration": false
}
```

`.dst-control.json` stores it under `policy` alongside the Agent's durable state.
Translate legacy `schedule`, `recycle` and paused-state assumptions explicitly.
Manual game closure shares scheduled closure semantics, and a later policy check may reopen games.
For a lasting administrative stop, stop the host service.
Never delete the new recovery state to force a retry; use the supported start or maintenance operations.

Later deployment of an existing room requires these steps:

1. Stop the old Pod and every old shard unit, then confirm all old processes have exited.
2. Confirm the existing backup policy covers the room data and record the previous image reference.
3. Validate the native topology, convert its policy, and review the new single-container Quadlet and port mappings.
4. Grant the container account access to the room directory and shared `mods` bind mount.
5. Start the new service with exclusive ownership of the directory, then verify all shards and player connections.
6. Retire the old units after the deployment checks; never run old and new writers against the same saves.

This repository does not automatically replace an existing Pod installation.
Validate the generated services and each room's readiness during deployment.

## Operations, Closure and Recovery

| Operation | Confirmation required |
| --- | --- |
| Start or restart | Every configured shard loaded, control available, expected shard links ready. |
| Save | Each shard's native final save callback and the corresponding target snapshot. |
| Stop | Every child exited with code zero, was reaped without forced termination, and drained its output without a protocol fault. |
| Reset or rollback | Requested worlds reloaded and the new runtime generation is ready. |
| Regenerate | New world identities and complete room readiness. |
| Player migration | The destination world reports the same player. |
| Player modification | Execution result from the identified player; lost confirmation becomes an unknown outcome. |
| Console | The submitted Lua chunk returned; asynchronous game work needs its own confirmation. |

The Agent owns accepted operations independently of client waiters.
The default stop wait is 220 seconds, including its 60-second notice; a different notice delay adjusts that budget.
Clean shutdown can return `saved_snapshot: null` when a Mod disables saving.
An abnormal exit or forced termination fails normal Stop.
Its error details retain every shard's exit status and confirmed saved snapshot.
Signal termination uses the negative signal number, such as `-6` for SIGABRT.
Explicit Kill succeeds when every child is reaped and its output drained, and reports forced termination.
A saved snapshot requires the separate native save proof.
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
Normal closure can reopen within the same container.
Abnormal exits, forced termination or an IPC fault require a fresh container, with the recovery decision persisted first.
Known configuration, disk, permission or Mod problems remain closed and report the cause.
Retryable faults receive two current-save restarts, 30 seconds apart.
After those fail, only a proven load failure may try one previous complete room snapshot.
Every target must exist, retain the same session, and have verifiable loss of no more than one complete game day.
Recovery records absolute targets before applying them and resumes those same targets after interruption.
It never repeatedly reinterprets "previous snapshot".

Thirty stable minutes restore the recovery budget.
An exhausted scheduled room gets a new opportunity at the next opening.
Repeated checks within the same opening do not reset it.
An always-open room requires explicit start after exhaustion.
The Agent remains available while games are closed.

Schedules are checked every 30 seconds with an explicit IANA timezone.
Clock baselines persist between policy ticks and reset when the Agent restarts.
With automatic Mod maintenance enabled, setup code queues a download before the first opening.
Changing the Mod setup through Configure also queues maintenance before reopening.
Default Mod announcements use a 60-second countdown and 30-second intervals.
One native download attempt has a 30-minute budget; policy permits two extra attempts five minutes apart.
Closing takes precedence over pending maintenance, which remains queued for the next opening.
Only `DST_SERVER_MOD_PROXY` supplies the native updater proxy.
Idle regeneration is off by default and requires an open, ready, reliably empty room.
Its world-age retention periods are 6, 24, 36, 72 and 168 hours.

## Feature Mapping and Reproducible Checks

These entries identify implementation and focused verification.
They do not imply that every real game mode, real player interaction or production load has passed acceptance.

| Capability | Rust entry points | Python / CLI | Focused evidence |
| --- | --- | --- | --- |
| Configuration and defaults | `settings`, `configuration`, `files` | `dst_server.settings`, `config`, `inspect` | 12 template native-file round trips; strict numbers; secrets; symlink rejection; interrupted transaction recovery. |
| Rooms and presets | `rooms::RoomStore`, `rooms::build_template` | `Room`, `RoomStore`, `build_template`, `fleet_room` | All 120 slots; JSON Pointer edits; current Room schema; permission and save preservation. |
| Host and deployment | `host`, `deployment`, `host_operations` | `Host`, `host` | Quadlet generation, allocation persistence, subprocess fixtures, batch results and diagnostics. |
| Game and player operations | `model`, `room`, `driver`, `rpc` | `Client.call`, `call`, `describe`, `console` | Model/pipe/RPC contracts; Agent lifecycle tests; isolated real-game 1/2/5-world smoke. |
| Save and recovery | `recovery`, `preloader`; Lua save/load observers | `save`, `rollback*`, lifecycle status | Fixed-target idempotence, invalid-session refusal, player restoration and native save completion. |
| Mods and policy | `mods`, `policy`, `agent` | `update_mods`, `set_policy`, Host operations | Native updater subprocess ownership; cancellation/reaping; windows, retry limits and closure decisions. |
| Archives and upload | `archive::prepare`, `PreparedArchive::compress`, `ClusterArchive::start_upload` | `dst_server.archive.export_cluster`, `ClusterArchive`, `archive export/upload`; Agent `export_archive` / `release_archive` | Real 7z/Zstd readback; native bytes and progress; sanitization; multipart success, failure and abort. |
| Live events and OTLP | `events`, `telemetry`, `observability`, `rpc::EventHub` | `Client.subscribe`, `subscribe`, `event_schema`, `validate_event` | gRPC loopback type preservation, retry/RetryInfo, partial rejection, overflow and shutdown. |
| Historical logs | `logs` | `JournalLogs`, `NetdataLogs`, `Host.journal`, `logs journal/telemetry` | Cursor/limit/error handling, native process cancellation and reaping. |
| Klei services | `external::KleiClient` | `KleiClient` | Local HTTP fixtures for bounds, errors, authentication and per-query batch outcomes. |
| Lua utilities | `lua`, `scripts`, `annotations` | `dst_server.settings`, `scripts`, `annotations` | Static literal bounds; ZIP duplicate/path/hash checks; repeatable upgrades; annotation return inference. |

Core tests live in [`crates/dst-server/tests`](../crates/dst-server/tests) and module unit tests.
Python installation checks live in [`tests/rust`](../tests/rust).
The CLI examples in the READMEs were checked against the actual `--help` output.
No external service or game operation is triggered merely to check a help example.

## Native Verification

[`tests/rust/native_acceptance.py`](../tests/rust/native_acceptance.py) checks native 1/2/5-world save, stop and restart.
It also checks absolute snapshot restoration, repeat restoration, invalid-session refusal and synthetic player recovery.
[`tests/rust/native_lifecycle.py`](../tests/rust/native_lifecycle.py) exercises Agent termination, child cleanup and recovery limits.
[`tests/rust/probe_modes.py`](../tests/rust/probe_modes.py) checks templates against the real game and their enabled Mods.
Gorge and Forge deliberately disable saving, so their expected result is an unconfirmed save followed by clean closure.
Use `--save-expectation unconfirmed` for those modes.
[`tests/rust/run_filesystem_failures.py`](../tests/rust/run_filesystem_failures.py) checks recovery after real EFBIG and ENOSPC failures.
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
It retains exit signals and independently confirmed saved snapshots.
A crash after a confirmed save does not itself demonstrate save corruption; an unconfirmed save cannot be treated as successful.
Short successful runs do not establish that this intermittent failure is fixed.
SDK acceptance covers correct error reporting and cleanup when a game process crashes.

## Build and Release Resources

The Rust toolchain and Cargo lockfile are checked in.
The Python package has no runtime Python business dependencies.
Lua resources, protocol schema and frozen configuration resources are compiled or packaged with the SDK.
Container builds install the native Agent and patch the game script ZIP using that same revision.
Certificate roots and timezone data are supplied by the runtime image and linked libraries.

[`tools/build_manifest.py`](../tools/build_manifest.py) records SDK/Python versions and source revisions.
It also records Lua, protocol and artifact digests, plus the game version.
The `distributions` container target builds native and Python artifacts; the `game` target creates the room runtime image.
CI checks Rust format/lints/tests, Lua 5.1 and LuaJIT contracts, and Python wrappers.
It also checks source distribution/wheel installation and CLI discovery.
Native game probes run separately from CI.
Use the deployment procedure above when replacing an existing installation.
