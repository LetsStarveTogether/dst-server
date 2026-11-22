# Observability

Use the room journal for past events and the Agent socket for live subscriptions.
When OTLP export is enabled, the same gameplay record carries the same `log.record.uid` in each output.
The [log query design](log-queries.md) traces a record through each output and defines the scope of each filter.

## Capture Profiles

| Profile | What the shards observe |
| --- | --- |
| `off` | Disables gameplay capture while allowing room control, occupancy checks, saves and recovery. |
| `timeline` | Connections, loads, migration, player and epic deaths, revivals, public chat, votes and world changes. |
| `play` (default) | Timeline plus meals, crafting, work, sampled combat contacts and kills attributable to players. |

Set `DST_SERVER_TELEMETRY_PROFILE=timeline` in the deployment environment and restart the Agent service.
An explicit `agent --profile` takes precedence.
In `play`, Lua queues routine actions and sends deaths of players and epic creatures immediately.
A queued meal can arrive after a later death.

## Read the Journal and Live Events

```sh
journalctl -u dst-299.service --since today
dst-server logs journal --unit dst-299.service --query '{"since":"today","grep":"dst.telemetry.coverage"}'
dst-server logs journal --unit dst-299.service --follow --query '{"grep":"dst.player.public_chat"}'
dst-server subscribe events --socket /srv/dst/299/.dst-agent.sock
dst-server call status --socket /srv/dst/299/.dst-agent.sock
dst-server call list_players --socket /srv/dst/299/.dst-agent.sock
```

A journal query defaults to the most recent 100 entries in reverse order.
Following starts at the live tail and sends matching entries forward.
A named system service matches across every journal file the reader can access, including files owned by the game's UID.
Omitting all units queries the system journal.
For user services, add `--user` to the journal command or use `JournalLogs(user=True)` in Python.
RPC offers `events` for gameplay, `lifecycle` for coverage and operations, and `logs` for game output.
Read each batch's `records`, including the final batch with `closed=true`, and check `dropped` for subscriber loss.
Agent status reports rejected frames by shard under `agent.telemetry.receivers`.

## OTLP and Netdata

The Agent exports logs, metrics and traces over OTLP/gRPC.
For example, set `OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317` in the Agent environment.
Signal settings such as `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` override the shared setting.
Built-in rooms disable trace export by default.
Set `OTEL_TRACES_EXPORTER=otlp` to enable it.
A signal accepts `none` to disable its exporter and `OTEL_SDK_DISABLED=true` disables all three.
Check Agent stderr for exporter startup errors and the local `dst.telemetry.exporter_health` log for delivery failures.

In the backend receiving OTLP data, filter the resource by `dst.cluster.name` (for example, `dst-299`).

- **Logs** — search for `dst.server.operation` and use its `dst.operation.id` to follow a management request.
- **Traces** — select spans named `dst.server.start` and inspect `dst.operation.outcome` on completion.
- **Metrics** — plot `dst.server.client.connection.count` with `dst.server.presence.last_observed`.
  The latter gives the Unix time of the most recent reliable sample, so a dashboard can show its age.
  `dst.telemetry.sequence.gap.count` measures missing send sequences.
  `dst.telemetry.delivery.count` with stage `driver_relay` measures records lost on their way to the room.

This Netdata query searches OTLP logs in the local `otel-plugin` store for a Klei Client ID over one UTC day.

```sh
dst-server logs telemetry '{"since":1790899200,"until":1790985600,"userid":"KU_example","limit":100}'
```

`list_players` returns each connected player's Klei Client ID in `userid`.
The Netdata `userid` filter searches participant roles and SDK administration records.
Netdata returns flattened fields and queries whole seconds in `[since, until)`.
The Python SDK rounds a fractional start down and a fractional end up and returns the effective window.
Its `limit`, `matched` and `truncated` apply to that window.
If the match count is unavailable or smaller than the returned row count, `matched` and `truncated` are unknown.

For a known historical room instance, use `NetdataLogs.search` with a `RoomStream` derived from its journal resource.
The structured search reports gameplay and coverage separately, including limits, file diagnostics and failed reads.
Use `subscribe_history` to drain both live streams while reconciling overlapping journal receipt windows. [SDK examples](log-queries.md)

## Count Observed Events

`read_history` selects journal entries by receipt time.
A delayed event can cross midnight.
Use `record.observed_timestamp_ns` for the Agent's observation time.
For game order, pass records from the selected pages to `dst_server.history.replay_by_run`.
It orders events within each shard process and world generation by game tick, monotonic time, then send sequence.

This example counts observed meals by food for yesterday in UTC, using one set of UIDs for the full report.

```python
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

from dst_server import Host
from dst_server.history import count_by, read_history


async def main():
    host = Host("/srv/dst", "/etc/containers/systemd")
    until = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    totals = Counter()
    seen: set[str] = set()
    async for page in read_history(
        host, [299], since=until - timedelta(days=1), until=until
    ):
        counts = count_by(
            page.records, field="data.food", event="dst.player.ate", seen=seen
        )
        totals.update(dict(counts))
    print(totals)


asyncio.run(main())
```

To count meals per player, change the grouping field to `data.player.userid`.
For a trade, `data.giver.userid` identifies the giver.
`record.participants()` returns each observed role and Client ID.
An unknown account is null.
Presence rosters include lobby clients, and `players[].loaded` can be unknown.
Jobs that resume after a restart can commit unique UIDs, aggregates and the checkpoint together.
Persist `page.checkpoint` and pass it as `checkpoint=` to validate the original selection on resume.

`page.rejections` gives the source positions and reasons for malformed or unsupported entries.
Report activity as unknown in windows with missing coverage.
Use `read_history(..., coverage_only=True)` on a preceding window to carry the last coverage for each `change.run`.
Coverage records show the active profile, observer failures and loss counters.
Compare `play_queue_dropped` within one generation and `shard_loss` within one shard process.
The shard's `reply_dropped` and `telemetry_gaps` counters can count the same dropped frame.
Count changes in `room_loss.stdout_dropped` once per `change.agent_instance_id`.
A game exit can discard queued events before they receive a sequence.
Large journal lines can also split at the container or journal limit.
Compare UIDs in RPC or OTLP during a capture. [Native log checks](testing.md#checking-the-native-game-and-external-services)
