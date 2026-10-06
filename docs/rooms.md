# Configure a Room, Manage Players and Restore Saves

The examples use room 299 under `/srv/dst`.
Run Python snippets containing `await` inside an async function or in `python -m asyncio`.
For service installation, opening hours and recovery, see the [deployment guide](deployment.md).

## Choose a Template and Enable a Mod

`dst-server template list` lists the gameplay templates.
Build a pure endless room definition with one Workshop Mod enabled on both shards.

```python
from dst_server.settings import Room, build_template

cluster = build_template("pure_endless", number=299, settings={"max_players": 9})
room = Room(number=299, template="pure_endless", cluster=cluster)
mods = {"entries": {"workshop-2189004162": {"enabled": True}}}
room = room.edit_many([
    (f"/cluster/shards/{shard}/mods", mods) for shard in cluster.dump()["shards"]
])
print(room.dump(defaults=True)["cluster"]["settings"]["max_players"])
```

To combine individual presets, use `compose([preset("FOREST_CAVES"), preset("ENDLESS")]).build()` from `dst_server.settings`.
For a new online server, pass its token from a secret file to `build_template()` before calling `await host.create(room)`.
The game supplies world settings omitted from a preset.
`room.dump()` masks credentials, while native files returned by `room.cluster.files()` contain their actual values.

The shared Mod download list includes Workshop items enabled on any shard or through `ForceEnableMod`.
Use `items` and `collections` under `/cluster/downloads` for extra Workshop IDs.
Omitting `downloads` when editing preserves the existing list of extras, and `downloads={}` clears those extras.
An ID that is both enabled and listed as an extra reads back as enabled.
For dynamic Mod scripts, edit the native files or replace the setup with both `downloads` and `mod_settings`.
The Agent checks loaded versions against Workshop every five minutes when automatic maintenance is enabled.
`status.agent.mod_check` reports the versions loaded by each shard, update targets and errors.
To announce a shutdown, update Mods and reopen during opening hours, run
`dst-server call update_mods '{"restart":true}' --socket /srv/dst/299/.dst-agent.sock`.
Use `DST_SERVER_MOD_PROXY` to route version checks and native downloads through an HTTP(S) proxy.

## Edit an Existing Room

Use `Host.edit_fields()` or `host edit` to change named fields on a managed room.
Policy edits can run while players are connected.
Stop the games before changing cluster settings.
Stop the whole service to change deployment, published ports, shard names or the template.

```sh
dst-server host edit --room 299 --set '/cluster/settings/max_players=9'
```

For direct file access, stop the Agent and game processes first.
`RoomStore` reads and writes native settings and policy under the room lock.

```python
from dst_server.settings import RoomStore

store = RoomStore("/srv/dst", quadlet_dir="/etc/containers/systemd")
room = store.load(299)
store.save(room.edit("/cluster/settings/cluster_description", "Friday games"))
```

Each shard directory with a `server.ini` participates in the room, with exactly one master.
Use Host to update the managed Quadlet when changing port mappings or deployment settings.
Changing a shard's internal game or query port offline through Host preserves its host port unless you supply a new mapping.
Keep the room lock file and `.dst-server.json` across service replacements to preserve lock ownership and recovery state.

## Find a Player and Manage Access

Connect to the room socket to query players across shards and change account permissions.
Use the player's Klei `userid` from `list_players` for subsequent calls.

```python
from dst_server import Client

async with await Client.connect("/srv/dst/299/.dst-agent.sock") as client:
    print(await client.call("list_players"))
    print(await client.read_permissions())
    await client.whitelist("KU_example")
```

`get_player` returns the player's location and character details, including a loading state during character selection.
`client.ban(userid, seconds=600)` sets a temporary ban and `client.ban(userid)` sets a permanent one.
`read_permissions()` reads the permanent lists, and native temporary bans expire on their own.
`Host` takes the room number first and can edit permanent permission lists while the service is stopped.

Use `await client.list_vote_commands()` to discover the game's registered votes.
Pass the starter's `userid` and any required target to `client.start_vote()`.
Read `client.get_vote()` to follow the vote.
`dst-server describe` and `dst-server describe --shard` list the available calls and their arguments.
After a caller timeout on a mutation, check `status.operations.current` and `status.operations.last` before retrying.

## Export a Room

Export packs world and player saves, native settings, Mod configuration and supported Mod progress into a 7z archive.
Use a new output filename.

```sh
dst-server archive export 299 --output room-299.7z
```

Export can read a running room directly.
For a consistent backup, stop the games cleanly first so every shard finishes its final save.
The exporter checks the latest world snapshot of each indexed session and reports an incomplete save.
For full recovery, also back up credentials, permission lists, local Mods and downloads, Agent state and the Quadlet.
Each shared archive gets a fresh cluster key.
When overriding the export configuration of a saved world, keep its shard IDs and its Forge or Gorge game mode.

Use `--client-compatible` to package exactly one master and one secondary as a game client save slot.
In Python, `async with export_cluster(store, 299) as archive:` from `dst_server.archive` gives access to the seekable `archive.stream`.
Call `await archive.save(path)` to save locally or `await archive.upload()` with your S3 or R2 environment.

## Inspect or Restore an Archive

`dst-server archive import room-299.7z` inspects the selected files and configuration in memory.
Use `--output` to write a new room directory at a path that does not exist.
Supply the new server's own token before hosting online.

```sh
dst-server archive import room-299.7z --output restored-299
```

To restore an existing room, back up the destination, stop its Agent and all game processes, and run from outside its directory.
The apply operation replaces the selected saves in full, including player data.

```sh
dst-server archive import room-299.7z --into /srv/dst/299 --level data
```

| Level | Content replaced |
| --- | --- |
| `data` | World and player saves, snapshots, indexes and saved game progress. |
| `runtime` (default) | All of `data`, plus Mod settings, download lists and world overrides. Selected override files absent from the archive are removed. |
| `all` | All of `runtime`, plus `cluster.ini` and each `server.ini`, including ports. Check the Quadlet's published ports before restarting. |

Applying requires matching shard counts and a complete indexed world snapshot in each source shard.
At `data` and `runtime`, the destination keeps its shard settings and must match explicit source shard IDs.
When a source omits a secondary shard ID, the importer uses its directory name or its unique role.
Before applying that archive to a room with custom IDs, confirm the original assignment or restore to a new directory.
To transfer a world between Forge, Gorge and ordinary rooms, use `all` so the game mode travels with the save.
Ambiguous matches fail before publication.
The destination keeps its token and permission lists.
The importer stages the room next to the destination and exchanges both directories.
The parent must be writable and on the same filesystem.
In Python, `await import_cluster(path)` from `dst_server.archive` exposes the parsed `configuration` and `files`.
Use `await imported.save(new_path)` or `await imported.apply(existing_path, level="data")` on the returned object.

## Query Klei or Steam

```python
from dst_server import KleiClient

klei = KleiClient()
print(await klei.get_latest_build("release"))
print(await klei.lobby("us-east-1"))
```

`get_latest_build()` returns the largest numeric build in the branch and reports an error if the branch has none.
Use `await KleiClient(access_token=token).room(row_id, region)` for lobby details and player data.
In Rust, pass up to 100 `PublishedFileId` values to `dst_server::steam::Client::published_file_details`
and inspect each result. [Workshop client](../crates/dst-server/src/steam.rs)
