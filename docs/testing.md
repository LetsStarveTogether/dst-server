# Testing

Start with the public operation a player, room operator or SDK caller uses.
A useful regression test fails when the visible result, saved data or process lifetime is wrong.

## Running Tests

Build the SDK with the stable Rust toolchain, Cap'n Proto and libarchive.
The Lua suite also needs the checked out `dst-scripts/scripts` submodule, Lua 5.1 and LuaJIT.
Install the Python development tools with `uv sync --locked --only-group dev` and install [cargo-nextest][nextest-install].

During an edit, select the relevant Rust target or behavior.

```sh
cargo nextest run --locked -p dst-server -E 'binary(lua_data)'
cargo nextest run --locked -p dst-server -E 'test(recovery)'
```

Run the regular Rust and Lua suites with the same commands as CI.

```sh
cargo nextest run --locked -p dst-server --all-targets
cargo build --locked -p dst-server --bin dst-server --example rpc_fixture
uv run --locked --only-group dev pytest tests/lua
```

nextest runs four tests concurrently and gives the real daily schedule and shutdown announcement tests priority.
The daily schedule test crosses two minute boundaries and can take up to 210 seconds.
The Lua harness starts a fresh interpreter for each scenario so registered game hooks and globals cannot leak between tests.
Use `pytest tests/lua -k 'bootstrap and luajit'` in the development environment to check the startup path on one interpreter.

`just test` also checks the Python bindings in the development environment.
`just build` builds a source distribution, installs its wheel in isolation and runs the SDK checks against that package.
Local builds, CI and releases use the same [SDK check script](../tests/rust/run_sdk_checks.sh).
It exercises public Python calls, archive and log commands, and checks the installed console script against the native CLI.
`just verify` also runs the repository hooks.
Run `cargo test --locked -p dst-server --doc` when changing executable documentation examples.

## Choosing the Test

| Behavior | Suitable check |
| --- | --- |
| A stored room setting can be repaired | Save through `RoomStore`, then read the file or reload the room. |
| A save, stop or cancelled request completes safely | Use the real Agent or driver with a disposable child and check the result, process and socket. |
| A Lua hook produces an event | Call the game component through its normal entry point on Lua 5.1 and LuaJIT. |
| An input rule has a few known outcomes | Use a small table of named cases with one assertion per outcome. |
| Encoded values retain their types | Generate values from the supported domain and assert the round trip. |
| Packaging or Python async behavior changes | Check the installed wheel through the facade, including cancellation when ownership matters. |

[Integration tests][rust-tests] can exercise several modules through a public API.
Keep a module test for a rule that is clearer to check directly, such as constructing a recovery target from fixed evidence.
The existing [proptest round trip][lua-data] generates bounded Lua data and shrinks failures to a small input.
Keep a concrete regression alongside a property when it documents a case the generator may miss.

Wait for an acknowledgement, a process exit or a state transition before asserting asynchronous effects.
Use Tokio's [paused clock][tokio-tests] for timers owned by a single runtime.
An external game process or the system wall clock needs a real deadline.
Share fixtures for repeated setup and keep the inputs that distinguish a scenario in its test.

## Checking the Native Game and External Services

The local tests use the pinned native game scripts, subprocess fixtures, and local HTTP and gRPC peers.
The [native acceptance checks](#native-verification) exercise the installed game and real saves in disposable rooms.
Point pytest at the scripts and executable extracted from the image.

```sh
uv run --locked --only-group dev pytest tests/lua --scripts /tmp/native/source/scripts --binary /tmp/native/dst-server
```

Check the active Lua listeners for each gameplay profile and validate their event names and payloads through Rust.
Exercise an accepted and a rejected action through the native component.
For example, a rejected player gift produces zero trades, and a successful gift produces one record naming both players.
With gameplay disabled, check that a short connection still updates activity and the Agent can still save the room.
Use the existing callback arguments, player entities and local ownership to attribute successful outcomes.

`tests/rust/probe_telemetry.py` accepts `--profile off`, `timeline` or `play` (default).
Pass a locally built Agent, its matching script bundle, an immutable local image ID and a private `dst-r2-` container name.
In `off`, check for zero gameplay frames while control and occupancy queries still work.
In `timeline` and `play`, check that compact rosters distinguish connected clients from loaded characters.
Binding the same resurrection structure twice produces one new bond in these two profiles.
For public chat, check that an accepted public message emits `dst.player.public_chat` and a whisper emits no event.
In `play`, check a meal, a log accepted by a firepit and a wooden wall placement for the same player.
The native probe damages a wooden wall and checks that two spiders share a sample for each direction.
It also damages one player with another and checks that both Client IDs appear on a single sample.
The Lua contract checks two epic creatures of the same kind and repeats a hit at 9.5 and 10 simulation seconds.
In the Lua burst test, 64 players each hit the same boss six times in one tick and then each eat once.
Check that all 64 combat samples and 64 meals arrive.
Check the FD4 frame limit and delivery to a slow subscriber.
Trigger a burst of meals in one simulation tick, followed by a death while meals are still queued.
Check the send limit against `GetStaticTick()`, which can advance while the simulation tick stays the same.
Check that player condition changes and deaths of players or epic creatures arrive immediately.
Trigger 64 ordinary kills attributable to a player in one tick.
Check that they share the four sends per static tick with other queued events.
Check the original times and players on deferred events.
Verify the `play_queue_dropped` delta after overflow and scheduling failures.

For a frame cost comparison, pin the game, save, player count, Mods, profile and release build options.
Measure two runs of the baseline and then alternate baseline and candidate in both orders for at least four pairs.
Use the monotonic clock for time between frames and the main thread CPU clock for work on that thread.
Record p95, p99 and the proportion of frames over budget alongside FD4 bytes per simulation tick.
The event's tick describes when the action happened. Check its send budget at the static tick that performed the write.
Measure RPC latency and drops per output with the Agent draining FD4 and one slow RPC subscriber.
In a separate disposable run, pause the FD4 reader, observe whether the game thread blocks and resume the reader.
A synthetic roster exercises serialization and occupancy edges. A loaded server needs representative gameplay and Mods.

On game build `762172`, individual `LuaPrintRemote` calls delivered 65,536, 65,537, 262,144 and 1,048,576 bytes via FD4.
Pausing the reader for 250 ms filled the 65,536-byte pipe and put the game thread to sleep in `anon_pipe_write`.
Resuming the reader delivered every byte and the following marker.
With 64 synthetic loaded players, the test roster produced an 18,165-byte `list_players` reply in both scenarios.
The ordinary sample gave each character one follower and three skills.
The rich sample used 16 followers, 15 skills, a mount and a combat target.
In the game's Lua 5.1 runtime, the median time to encode the lists was 2.08 ms and 2.17 ms respectively.
Encoding the same characters with all `get_player` fields took 7.24 ms and 16.23 ms, producing 63,221 and 121,141 bytes.
These timings cover reply encoding from an assembled roster, with five runs to warm up and 51 measured runs per scenario.
The reader drained FD4 throughout.

To replay saved Lua events in the RPC capacity tests, point `DST_TEST_EVENT_CAPTURE` at a JSON Lines file.
Each line contains one Lua envelope without its FD4 framing prefix.
The test output includes `schema_capacity` with the measured resource and record sizes.

To check delivery to all three outputs, start the isolated logs collector and subscribe to events before acting.
Have one native player eat one berry and then issue a distinct marker on the same event stream.
Read the subscription through the marker and check `dropped`.
After output drains, count the matching rows in stdout, RPC and OTLP before deduplicating.
Each output should contain one row with the same UID, instance, shard, nonce, generation, sequence and clocks.
Check large journal records with the actual service log driver and the matching system or user Host.
Record the UTF-8 byte length of the complete `DST_RECORD|` line, excluding its terminating newline.
With conmon 2.2.1 and its journald driver, a roughly 66 KB line produced multiple `MESSAGE` entries.
The first fragment had `CONTAINER_PARTIAL_MESSAGE=true`.
With Podman passthrough, the tested service journal returned 49,152 bytes in one entry and split 49,153 at `line-max`.
Both the container log driver and the journal's stream limit affect the result.
See the [Podman log drivers] and [journald stream settings].

The ordinary Steam tests use local responses, including retries and proxy errors.
Use the anonymous Workshop check to test network access.

```sh
cargo test --locked -p dst-server --test steam -- --ignored --nocapture
```

Set `DST_SERVER_MOD_PROXY` to include the configured proxy in that check.

## Native Verification

[`tests/rust/native_acceptance.py`](../tests/rust/native_acceptance.py) checks native 1/2/5-world save, stop and restart.
Its Agent check saves two shards through the CLI, verifies both native catalogs and waits for clean shutdown.
The probes also cover absolute and repeated restores, invalid sessions and synthetic player recovery.
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
Ten two-shard cycles completed with all 20 exits zero, which is insufficient to establish a safe shard-count threshold.
The game build, shared runtime, network environment and shard interactions remain possible contributors.
No validated correction or specific upstream cause has been established.

On build `762172`, we requested three shutdowns in an offline two-shard room.
The surface process wrote its snapshot and exited with SIGSEGV during the second shutdown.
The SDK returned `partial_failure` and reported that output had drained without a forced kill.
Both shards reopened their saved sessions and exited normally in the third cycle.
The cause of the surface crash is unknown.

The Agent reports abnormal Stop as failure and requires a fresh container.
It retains exit signals and process cleanup results.
A crash alone does not establish save corruption; inspect the native snapshot catalog when persistence matters.
Short successful runs do not establish that this intermittent failure is fixed.
SDK acceptance covers correct error reporting and cleanup when a game process crashes.

[rust-tests]: https://doc.rust-lang.org/book/ch11-03-test-organization.html
[nextest-install]: https://nexte.st/docs/installation/pre-built-binaries/
[Podman log drivers]: https://docs.podman.io/en/stable/markdown/podman-run.1.html#log-driver-driver
[journald stream settings]: https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html#LineMax=
[tokio-tests]: https://tokio.rs/tokio/topics/testing
[lua-data]: ../crates/dst-server/tests/lua_data.rs
