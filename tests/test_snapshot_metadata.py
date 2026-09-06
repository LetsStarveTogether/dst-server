from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

from dst_server.models.snapshot import (
    PhaseSegments,
    PlayerSnapshotMetadata,
    Snapshot,
    SnapshotCatalog,
    WorldSnapshotMetadata,
)

WORLD_LUA = b"""return {
    clock = {
        segs = {day=7,dusk=5,night=4}, cycles = 20, phase = "night",
        mooomphasecycle = 1, totaltimeinphase = 120,
        remainingtimeinphase = 20.5
    },
    seasons = {
        mode = "cycle", premode = false,
        israndom = {autumn=false,winter=false,spring=true,summer=false},
        segs = {
            autumn={day=8,dusk=6,night=2}, winter={day=5,dusk=5,night=6},
            spring={day=5,dusk=8,night=3}, summer={day=11,dusk=1,night=4}
        },
        season = "winter", totaldaysinseason = 15, elapseddaysinseason = 0,
        remainingdaysinseason = 15,
        lengths = {autumn=20,winter=15,spring=20,summer=15}
    }
}"""


@pytest.fixture
def metadata_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="dst-snapshot-metadata-") as directory:
        yield Path(directory) / "0000000021.meta"


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [(b"", b""), (b"", b"\0"), (b"KLEI     1 ", b"\n"), (b"KLEI     1 ", b"\0")],
)
def test_load_complete_world_metadata(
    metadata_path: Path, prefix: bytes, suffix: bytes
) -> None:
    content = prefix + WORLD_LUA + suffix
    metadata_path.write_bytes(content)

    metadata = WorldSnapshotMetadata.load(metadata_path)

    assert metadata.model_dump() == {
        "clock": {
            "segs": {"day": 7, "dusk": 5, "night": 4},
            "cycles": 20,
            "phase": "night",
            "mooomphasecycle": 1,
            "totaltimeinphase": 120.0,
            "remainingtimeinphase": 20.5,
        },
        "seasons": {
            "mode": "cycle",
            "premode": False,
            "israndom": {
                "autumn": False,
                "winter": False,
                "spring": True,
                "summer": False,
            },
            "segs": {
                "autumn": {"day": 8, "dusk": 6, "night": 2},
                "winter": {"day": 5, "dusk": 5, "night": 6},
                "spring": {"day": 5, "dusk": 8, "night": 3},
                "summer": {"day": 11, "dusk": 1, "night": 4},
            },
            "season": "winter",
            "totaldaysinseason": 15,
            "elapseddaysinseason": 0,
            "remainingdaysinseason": 15,
            "lengths": {"autumn": 20, "winter": 15, "spring": 20, "summer": 15},
        },
    }
    assert isinstance(metadata.clock.segs, PhaseSegments)
    assert metadata.day == 21
    assert metadata_path.read_bytes() == content


@pytest.mark.parametrize(
    "content",
    [b"return {}\0", b"return {clock={},seasons={}}\0"],
)
def test_initial_world_metadata_has_no_invented_values(
    metadata_path: Path, content: bytes
) -> None:
    metadata_path.write_bytes(content)

    metadata = WorldSnapshotMetadata.load(metadata_path)

    assert metadata.model_dump(exclude_none=True) == {"clock": {}, "seasons": {}}
    assert metadata.day is None


def test_initial_world_metadata_can_have_only_season_fields(
    metadata_path: Path,
) -> None:
    metadata_path.write_bytes(
        b'return {clock={},seasons={season="spring",totaldaysinseason=20,'
        b"elapseddaysinseason=0,remainingdaysinseason=20}}\0"
    )

    metadata = WorldSnapshotMetadata.load(metadata_path)

    assert metadata.seasons.season == "spring"
    assert metadata.seasons.remainingdaysinseason == 20
    assert metadata.seasons.mode is None
    assert metadata.seasons.lengths is None
    assert metadata.day is None


@pytest.mark.parametrize("character", ["wilson", "mod_character"])
def test_player_metadata_uses_character_identifier(
    metadata_path: Path, character: str
) -> None:
    metadata_path.write_bytes(f'return {{character="{character}"}}\0'.encode())

    metadata = PlayerSnapshotMetadata.load(metadata_path)

    assert metadata.model_dump() == {"character": character}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorldSnapshotMetadata.load(metadata_path)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"KLEI     2 compressed",
        b"return {}\xff",
        b"return {clock={}\0}",
        b"return {}\0\0",
        b"return {",
        b"return {}; print(1)",
        b'return require("clock")',
        b"return {clock={cycles=1+2}}",
        b"return {clock={},clock={}}",
        b'return {character="wilson"}',
        b"return {clock={moonphasecycle=1}}",
        b"return {clock={cycles=true}}",
        b'return {clock={cycles="1"}}',
        b"return {clock={cycles=-1}}",
        b"return {clock={remainingtimeinphase=1e999}}",
        b'return {seasons={season="monsoon"}}',
        b"return {seasons={israndom={autumn=1}}}",
    ],
)
def test_reject_invalid_world_metadata(metadata_path: Path, content: bytes) -> None:
    metadata_path.write_bytes(content)

    with pytest.raises(ValueError, match=r"snapshot|Snapshot"):
        WorldSnapshotMetadata.load(metadata_path)


def test_player_metadata_requires_character(metadata_path: Path) -> None:
    metadata_path.write_bytes(b"return {}\0")

    with pytest.raises(ValidationError, match="character"):
        PlayerSnapshotMetadata.load(metadata_path)


def test_metadata_loader_requires_regular_file(metadata_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        WorldSnapshotMetadata.load(metadata_path)
    assert not metadata_path.exists()

    with pytest.raises(ValueError, match="regular file"):
        WorldSnapshotMetadata.load(metadata_path.parent)

    target = metadata_path.with_suffix(".target")
    target.write_bytes(WORLD_LUA)
    metadata_path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        WorldSnapshotMetadata.load(metadata_path)


def test_snapshot_catalog_supports_metadata_hydration(metadata_path: Path) -> None:
    catalog = SnapshotCatalog.model_validate_json(
        '{"session_id":"EXAMPLE","snapshots":[{"snapshot_id":21,'
        '"world_file":"session/EXAMPLE/0000000021"},{"snapshot_id":0}],'
        '"has_more":false}'
    )
    metadata_path.write_bytes(WORLD_LUA)

    snapshot = catalog.snapshots[0].replace(
        metadata=WorldSnapshotMetadata.load(metadata_path)
    )
    hydrated = catalog.replace(snapshots=(snapshot, catalog.snapshots[1]))

    assert catalog.snapshots[0].metadata is None
    assert catalog.snapshots[1] == Snapshot(snapshot_id=0)
    assert snapshot.metadata is not None
    assert snapshot.metadata.day == 21
    assert SnapshotCatalog.model_validate_json(hydrated.model_dump_json()) == hydrated
    with pytest.raises(ValidationError, match="greater_than_equal"):
        snapshot.replace(snapshot_id=-1)
