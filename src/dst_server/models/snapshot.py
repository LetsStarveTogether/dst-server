from pathlib import Path
from typing import Literal, Self

from pydantic import Field

from .base import (
    FrozenModel,
    Identifier,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
)


class PhaseSegments(FrozenModel):
    day: NonNegativeInt | None = None
    dusk: NonNegativeInt | None = None
    night: NonNegativeInt | None = None


class SeasonSegments(FrozenModel):
    autumn: PhaseSegments | None = None
    winter: PhaseSegments | None = None
    spring: PhaseSegments | None = None
    summer: PhaseSegments | None = None


class SeasonLengths(FrozenModel):
    autumn: NonNegativeInt | None = None
    winter: NonNegativeInt | None = None
    spring: NonNegativeInt | None = None
    summer: NonNegativeInt | None = None


class SeasonRandomness(FrozenModel):
    autumn: bool | None = None
    winter: bool | None = None
    spring: bool | None = None
    summer: bool | None = None


class SnapshotClock(FrozenModel):
    segs: PhaseSegments | None = None
    cycles: NonNegativeInt | None = None
    phase: Literal["day", "dusk", "night"] | None = None
    mooomphasecycle: PositiveInt | None = None
    totaltimeinphase: NonNegativeFloat | None = None
    remainingtimeinphase: NonNegativeFloat | None = None


class SnapshotSeasons(FrozenModel):
    mode: Literal["cycle", "endless", "always"] | None = None
    premode: bool | None = None
    israndom: SeasonRandomness | None = None
    segs: SeasonSegments | None = None
    season: Literal["autumn", "winter", "spring", "summer"] | None = None
    totaldaysinseason: NonNegativeInt | None = None
    elapseddaysinseason: NonNegativeInt | None = None
    remainingdaysinseason: NonNegativeInt | None = None
    lengths: SeasonLengths | None = None


class _Metadata(FrozenModel):
    @classmethod
    def load(cls, path: Path) -> Self:
        from dst_server.lua_codec import KLEI_FILE_HEADER, parse_literal

        if path.is_symlink() or not path.is_file():
            msg = f"snapshot metadata must be a regular file, not a symlink: {path}"
            raise ValueError(msg)
        try:
            source = path.read_text(encoding="utf-8").rstrip().removesuffix("\0")
        except UnicodeError as error:
            msg = f"snapshot metadata must contain UTF-8 Lua: {path}"
            raise ValueError(msg) from error
        source = KLEI_FILE_HEADER.sub("", source.lstrip(), count=1)
        if "\0" in source or not source.startswith("return"):
            msg = f"unsupported snapshot metadata format: {path}"
            raise ValueError(msg)
        value = parse_literal(source.removeprefix("return"), "snapshot metadata")
        return cls.model_validate(value)


class WorldSnapshotMetadata(_Metadata):
    clock: SnapshotClock = Field(default_factory=SnapshotClock)
    seasons: SnapshotSeasons = Field(default_factory=SnapshotSeasons)

    @property
    def day(self) -> int | None:
        return self.clock.cycles + 1 if self.clock.cycles is not None else None


class PlayerSnapshotMetadata(_Metadata):
    character: Identifier


class Snapshot(FrozenModel):
    snapshot_id: NonNegativeInt
    world_file: str | None = None
    metadata: WorldSnapshotMetadata | None = None


class SnapshotCatalog(FrozenModel):
    session_id: Identifier
    snapshots: tuple[Snapshot, ...]
    has_more: bool
