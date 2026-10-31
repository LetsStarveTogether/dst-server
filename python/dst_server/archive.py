# ruff: file-ignore[private-member-access]
"""Native room imports, exports in memory and cancellable S3 uploads."""

from asyncio import CancelledError, shield
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import PathLike
from typing import Any, BinaryIO, Protocol, Self, cast

from ._native import ClusterArchive as _Archive
from ._native import export_archive as _export_archive
from ._native import import_archive as _import_archive
from .host import Host
from .settings import Configuration


class SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


def _secret(value: str | SecretValue | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.get_secret_value()


@dataclass(frozen=True, slots=True)
class ArchiveUploadResult:
    key: str
    url: str | None


class ClusterArchive:
    """A native archive with an independent, seekable binary stream."""

    __slots__ = ("_archive",)

    def __init__(self, filename: str, stream: BinaryIO) -> None:
        self._archive = _Archive(filename, stream)

    @classmethod
    def _from_native(cls, archive: _Archive) -> Self:
        result = cls.__new__(cls)
        result._archive = archive
        return result

    @property
    def filename(self) -> str:
        return self._archive.filename

    @property
    def stream(self) -> BinaryIO:
        return cast("BinaryIO", self._archive.stream)

    async def save(self, path: str | PathLike[str]) -> None:
        """Write a new file; existing files are preserved."""
        await self._archive.save(path)

    async def upload(
        self,
        *,
        bucket: str | None = None,
        endpoint: str | None = None,
        region: str | None = None,
        access_key_id: str | SecretValue | None = None,
        secret_access_key: str | SecretValue | None = None,
        session_token: str | SecretValue | None = None,
        object_prefix: str = "",
        url_prefix: str | None = None,
    ) -> ArchiveUploadResult:
        """Upload with explicit S3 settings, waiting for abort on cancellation."""
        upload = self._archive.start_upload(
            bucket=bucket,
            endpoint=endpoint,
            region=region,
            access_key_id=_secret(access_key_id),
            secret_access_key=_secret(secret_access_key),
            session_token=_secret(session_token),
            object_prefix=object_prefix,
            url_prefix=url_prefix,
        )
        try:
            return ArchiveUploadResult(**await upload.wait())
        except CancelledError:
            await shield(upload.cancel())
            raise

    def close(self) -> None:
        self._archive.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@asynccontextmanager
async def export_cluster(
    host: Host,
    number: int,
    *,
    configuration: Configuration | dict[str, Any] | None = None,
    room_id: str | None = None,
    client_compatible: bool = False,
    compression_level: int = 9,
) -> AsyncGenerator[ClusterArchive]:
    """Return an archive in RAM; optionally adapt two shards for a client save slot."""
    with ClusterArchive._from_native(
        await _export_archive(
            host._host,
            number,
            options={
                "configuration": configuration,
                "room_id": room_id,
                "client_compatible": client_compatible,
            },
            compression_level=compression_level,
        )
    ) as archive:
        yield archive


async def import_cluster(
    source: str | PathLike[str] | ClusterArchive,
    destination: str | PathLike[str],
    *,
    from_client: bool = False,
) -> Configuration:
    """Import a directory or archive into a new native save directory."""
    return await _import_archive(
        source._archive if isinstance(source, ClusterArchive) else source,
        destination,
        options={"from_client": from_client},
    )


__all__ = [
    "ArchiveUploadResult",
    "ClusterArchive",
    "SecretValue",
    "export_cluster",
    "import_cluster",
]
