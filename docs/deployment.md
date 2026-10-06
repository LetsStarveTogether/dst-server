# Room Deployment

Use one matched set of the Agent binary, Lua bundle and Python wheel for each room.
The Agent holds the room lock and owns every shard.
Its Unix socket stays available while the games are closed.
See the [room setup](../README.md#open-a-room) and [native acceptance checks](testing.md#native-verification).

## Build from Source

Install stable Rust, a C/C++ toolchain, Cap'n Proto, pkg-config, libarchive development files (3.7+) and uv.
Use the same checkout to build the CLI, Python bindings and image.

```sh
cargo build --locked --release -p dst-server --bin dst-server
export PATH="$PWD/target/release:$PATH"
uv sync --locked
game_version=$(uv run --no-sync python -c '
import asyncio
from dst_server import KleiClient
print(asyncio.run(KleiClient().get_latest_build()))
')
podman build --target game --build-arg GAME_VERSION="${game_version:?}" -t localhost/dst-server:rust .
```

Set `deployment.image` to `localhost/dst-server:rust` when creating a room with this image.
The published release and beta images use `quay.io/wh2099/dst-server:latest` and `quay.io/wh2099/dst-server:beta`.
Generated units use `Pull=never`, so pull a published tag before starting the service.
Enable `podman-auto-update.timer` if you want systemd to update these tagged images and restart their rooms.

## Replacing a Running Deployment

1. Record the running image digest, game build, wheel hash and Python ABI separately for release and beta.
   A push to `main` can publish both shared image tags.
   Pin rooms awaiting replacement to their existing image or suspend their automatic updates before publishing.
2. Inspect the effective Quadlet, its drop-ins, port mappings and policy.
   Record the maintenance origin, recovery budget and each shard's fixed restore target.
   Keep the current intent for closed rooms.
   Prepare the new state and unit offline on a private copy with the candidate tools.
   Render the managed unit with the candidate Host so the Agent inherits `PODMAN_SYSTEMD_UNIT`.
   The candidate unit uses `LogDriver=passthrough` in `[Container]` to send Agent stdout to the service journal.
   A custom telemetry room name belongs in `OTEL_RESOURCE_ATTRIBUTES` as `dst.cluster.name`.
   Wait for an active maintenance job to finish if its original operation identity cannot be established.
   Remove obsolete `last_wall` and `last_monotonic` fields from `policy_state` on the offline copy.
   An idle maintenance record with no pending targets uses an explicit empty `policy_state.mods.targets` object.
   Inspect `DST_SERVER_TELEMETRY_PROFILE` in the unit and its drop-ins.
   A fleet unit with `history` needs `play` for the current player action coverage.
   Render the current stop settings and remove `SuccessExitStatus=8` and `RestartForceExitStatus=8` on the copy.
3. Run the candidate and its wheel on a disposable copy with its own mounts, socket, network, Mod data and test credentials.
   Exercise opening, saving and stopping a normal room, and reopening the Agent of a room held closed by recovery.
   Rehearse restoring the prior image, wheel, Mod files and state on the copy.
4. During the maintenance window, announce the stop.
   Record each saveable shard's current snapshot and request a save.
   Verify a newer readable snapshot on each shard and inspect the native exit results.
   Forge and Gorge disable saves, so verify their expected exits separately.
5. Stop the service and exclude other writers.
   Take a restorable copy of world data, management state, permissions, Mods and UGC.
   Re-read `agent_run` after the previous Agent exits cleanly.
   An active run or a failure missing its attempt ID needs inspection before conversion.
   Keep the room lock inode, update the state and install the complete unit with its original port mappings.
   Reload systemd and start the service according to its prior intent.
   Check the Agent and confirm shard readiness or the expected recovery closure.
   An explicit SDK Host start can authorize a retry in a room closed by recovery.
6. Verify one room before continuing.
   On failure, keep the candidate saves and diagnostics.
   Rehearse loading any new player progress with the previous artifacts on a copy before returning the room to players.

## Resident Agent

Generated Quadlets use `Timezone=local`, `RunInit=true` and `Restart=on-failure`.
Reload systemd after replacing a unit and recreate the container to apply its new image and stop settings.

The Agent stays available when the schedule is closed, Mod maintenance has failed, or recovery is exhausted.
It checks the policy on startup, every 30 seconds, and after configuration or maintenance changes.
At an opening it completes pending maintenance and recovery before starting all configured shards.
At closure it saves and stops all games, keeping the container and control socket available.
Changing policy times or timezone takes effect through the same scheduler.

`host start` and `host restart` wait for all shards to be ready during opening hours.
Outside opening hours they wait for an available Agent with stopped games.
Use `host stop` to stop the container and suspend automatic operation.
`call stop` also shuts down the Agent after stopping games, including when a scheduled closure shortens the countdown.
These explicit stops exit with code `0`.
Abnormal game exits, forced termination and persistent control faults exit with code `1` for recovery in a fresh container.

`call restart` normally waits for replacement games to become ready within the same Agent.
If the schedule closes during its countdown, the Agent stops the shards and returns `restart: false`.
`call update_mods` persists the maintenance request and replies with `queued: true`.
The current Agent runs the updater and then starts games if requested and permitted by the schedule.
With `restart: false`, it exits after completing maintenance.
A manual update can take over automatic maintenance queued during a scheduled closure.
An operator can also stop the Agent while that automatic update is queued.
With automatic updates enabled, a setup error at Agent startup appears in `status.agent.maintenance.error`
while the socket stays available.
A full `configure` request with explicit `downloads` can repair the Workshop setup while games are stopped.
For a dynamic `modsettings.lua` with unknown dependencies, edit the native setup directly
or supply `mod_settings` and `downloads` together.
A queued maintenance reply does not confirm game readiness; use Host operations to wait for the result.

### Service Stop Notices and Budgets

The first TERM or INT closes admission and gives accepted work a shared 180-second handoff deadline.
Controller and Room use the same deadline.
Cached Room and Shard Status remain available during shutdown; an unknown shard still returns `not_found`.
Other new calls return `not_ready`.
A healthy running master receives the default shutdown notice even when the room is empty or its player count is unknown.
Each announcement request has at most five seconds inside the Driver.
After the first true result, the Agent waits 60 seconds and sends a reminder after 30 seconds.
The final notice has at most another five seconds.
True confirms the game call, without confirming that each player saw it.
False, error or unknown first results continue directly to saving; an unknown command is not resent.
Existing Stop, Restart, MOD or schedule deadlines are reused if earlier, including an explicit no-notice choice.
Stop, Restart and UpdateMods reserve that choice before the Controller records internal acceptance.
A signal before Room execution preserves the original notice choice.
The interrupted request still reports its actual result.
Internal acceptance establishes operation ownership and does not confirm final RPC success.
Schedule closure never adds another minute when it reaches its deadline.
Later TERM/INT signals end the remaining notice or cancel handoff waits and continue normal saving.
Kill retains its explicit forced-stop behavior.

The common native exit deadline is 370 seconds after the first signal, followed by KILL at 380 seconds if still needed.
An earlier stop deadline is retained; another caller cannot reset it.
Independent deadline enforcement still runs while Room ownership is held elsewhere.
After game output and request workers, the default stage allocation reaches 412 seconds.
Telemetry then allows 5 seconds for relays, 30 for logs, 30 for the SDK and 5 for local output, reaching 482 seconds.
RPC drain adds four seconds, for a total default allocation of 486 seconds and 54 seconds below Podman's 540 seconds.
OS reaping, synchronous IO, scheduling and SDK background work can exceed these application targets.
Completion still requires real exit reports.
Notice failure or handoff timeout alone does not turn a clean game exit into a failed save.
Abnormal or forced game exit still returns failure even if later cleanup succeeds.

Room waits for log exporter settlement before closing the metrics and tracing SDK, then closes local output.
This gives the final metrics collection a chance to observe log timeouts, shutdown drops and cleared pending counts.
It does not include that export's own result or later local output totals, and does not guarantee remote delivery.
Cancelled callers and repeated waits observe the same retained close operation.

The managed unit uses `StopSignal=SIGTERM`, `StopTimeout=540` and `TimeoutStopSec=600`.
Check effective Quadlet and systemd drop-ins before replacing it.
Podman fixes its stop timeout when it creates the container.
Systemd applies `TimeoutStopSec` separately to its stopping phases.

Host start/stop/restart default to a 900-second caller wait.
A slow restart can exceed this wait; pass a larger `timeout` when needed and query status before resending timed-out work.

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
Default completion budgets include the notice delay and up to ten seconds for its first and final requests.

| Request | Default 60-second notice | Explicit `notice: null` |
| --- | --- | --- |
| Stop | 230 seconds | 160 seconds |
| Restart | 1210 seconds | 1140 seconds |
| UpdateMods | 7210 seconds | 7140 seconds |

Kill keeps its 150-second budget.
A custom notice replaces the default delay; zero delay sends at most one immediate notice.
Omitting notice in a JSON call request keeps the default; explicit null and Rust `None` disable it.
Host update operations default to no notice, and Python `Host.update_mods()` continues to send null explicitly.
Stop, Restart and UpdateMods perform their requested stop when the notice fails or returns false or unknown.
Invalid notice parameters are rejected before admission, and a failed native stop still prevents restart or MOD download.
Independent Announce calls preserve their result and error semantics.
Shutdown sends one native `c_shutdown()` per shard, allowing the game to perform its final save.
It then waits for exit, process reaping and output drainage.
Mods that disable saving can accept save requests and stop cleanly without creating a snapshot.
An abnormal exit or forced termination fails normal Stop.
Its error details retain each shard's exit status, actual termination flag and output drainage.
They also retain protocol errors and output counters.
The shared final `exit` summary is absent until process wait and reader cleanup complete.
Driver `failure` keeps its control error independently of the Process `protocol_error`.
`force_requested` records operation intent; it does not override actual `forced` evidence.
Signal termination uses the negative signal number, such as `-6` for SIGABRT.
Explicit Kill succeeds when every child is reaped and its output drained.
It reports actual exit results with independent `force_requested` intent.
Save and stop results do not confirm disk persistence.
There is one concurrent mutation per room; ordinary conflicts return `busy`.
During service shutdown only cached Room and Shard Status remain available to new callers.
A timed-out or disconnected mutation is never automatically resent.
The Agent keeps the current operation and last result in memory.
A required shard failure stops the entire room.
Scheduled closure keeps the Agent running; explicit stop exits the container with code `0`.
Abnormal exits, forced termination or an IPC fault require a fresh container, with the recovery decision persisted first.
Known configuration, disk, permission or Mod problems remain closed and report the cause.
Retryable faults receive two current-save restarts, 30 seconds apart.
The login case requires the exact adjacent native login-host CURL 6 and empty Account Failed 6 lines within five seconds.
It applies before any generation, readiness or world identity.
Other messages do not grant retry permission, and observed login or Lua diagnostics alone do not terminate a running game.
Each failed shard must supply its own evidence; a healthy sibling cannot authorize an unknown shard.
Control errors, missing final exit results or undrained output block recovery.
The original cause and trigger survive cleanup signals and abnormal exit codes.
Later independent failures and unforced abnormal exits also constrain classification using their original shard evidence.
Persistent health-check failure has room scope; every required shard still needs its own valid identity and final exit report.
Login faults never authorize rollback and still require a complete catalog with a session and latest native world file.
New worlds without those saved identities remain closed.
Current saves may differ by at most `MAX_SNAPSHOT_GAP = 3` across all required shards.
Larger gaps remain closed with `snapshot_gap_exceeded`.
Snapshot changes during a started attempt do not block the next retry.
Startup also waits through the world reload caused by native shard synchronization.
An unstarted retry still requires its recorded saves.
After those fail, only a proven load failure may try one previous complete room snapshot.
Every target must exist in the same session and precede the failed snapshot.
It must be no more than three snapshot numbers behind any shard's latest save.
Snapshot numbers also advance for manual saves, so this limit does not guarantee a particular number of game days.
Recovery records absolute targets before applying them and resumes those same targets after interruption.
The compact JSON stores current save identities inside `retry` and omits absent optional fields.
`last_failure` stores one bounded cause and disposition, including per-shard evidence and final exit facts.
It also records classification, catalog errors, decision, budget and correlated recovery result.
The first report is saved before cleanup; final facts and classification follow reaping.
An interrupted Agent with a known failure needs a complete report before recovery can proceed.
Failure consumption and budget reservation share one atomic state update.
Starting the retry and setting its new active identity share another.
Pending retries survive Agent replacement without another charge; a started interruption preserves the spent attempt.
Only readiness of the associated recovery attempt marks success.
Normal maintenance, stopping and budget resets preserve the report.
Before its own attempt starts, a new Room also preserves the previous active run or completed failure during empty stops.
Failure-evidence persistence errors keep subsequent cleanup conservative, including when an earlier wait is cancelled.
After recovery reaches readiness, a later Agent interruption starts a new incident without restoring the spent budget.
Fatal or already closed failures skip catalog work, while either catalog phase failing closes with its original cause retained.
Preparation rechecks all shards and records completion once, after every native receipt is verified.
It never repeatedly reinterprets "previous snapshot".

Thirty minutes of continuous readiness across the same shard processes and world generations restore the recovery budget.
At the next opening, an unstarted retry checks its reserved saves again before launching any shard.
An exhausted room keeps its Agent available; a scheduled room gets a new opportunity at the next opening.
Repeated checks within the same opening do not reset it.
An always-open room requires an explicit Host start after exhaustion.
Container startup reads the persisted recovery state and preserves its remaining budget.
After container exit, use Host status to inspect that state and the current schedule.

## Build and Release Resources

The Rust toolchain and Cargo lockfile are checked in.
The Python package has no runtime Python business dependencies.
Lua resources, the Cap'n Proto protocol schema and gameplay presets are compiled or packaged with the SDK.
Container builds install the native Agent and patch the game script ZIP using that same revision.
Certificate roots and timezone data are supplied by the runtime image and linked libraries.

[`tools/build_manifest.py`](../tools/build_manifest.py) records SDK/Python versions and source revisions.
It also records source, Lua and artifact digests, the toolchain identity and the installed game version when supplied.
Native and Python artifacts also compile `SOURCE_REVISION` and `SOURCE_DIRTY` into their telemetry resource identity.
`SOURCE_REVISION` accepts a full 40- or 64-digit hexadecimal Git object ID; `SOURCE_DIRTY` accepts `true`, `false` or `unknown`.
Absent revision yields no `dst.build.revision` and `dst.build.dirty="unknown"` regardless of the supplied clean flag.
A dirty build names its source baseline, without claiming the artifact equals that commit.
Container builds pass these inputs before compilation; CI only claims `false` after checking the selected checkout is clean.
For a modified local build, pass the chosen baseline and `SOURCE_DIRTY=true` to both Cargo and maturin.
No runtime Git lookup or previously installed image manifest supplies these facts.
`dst.game.version` accepts at most 128 bytes from the canonical executable's `bin64/../version.txt`.
It accepts ASCII digits with trailing CR/LF, which also counts toward the byte limit.
The opened source must be a regular file; symbolic links to regular version files remain supported.
Missing, unreadable, malformed, overlong or nonregular sources yield `unknown`, as do nonstandard installation layouts.
Writerless FIFOs are rejected without waiting for a writer, including when OTEL export is disabled.
Build and game facts are applied after environment resource attributes, so those attributes cannot replace them.
The `distributions` container target builds native and Python artifacts; the `game` target creates the room runtime image.
CI checks Rust format/lints/tests, Lua 5.1 and LuaJIT contracts, and Python wrappers.
It also checks source distribution/wheel installation and CLI discovery.
Optional CI jobs run native game probes in disposable containers.
