# ruff: file-ignore[private-member-access]
"""Consistent native room exports and cancellable S3 uploads.

Export requires the game to be stopped.
An online Agent retains room ownership while preparing the archive.
Transfer artifacts expire after thirty minutes if a caller disappears.
"""

from asyncio import CancelledError, shield
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import PathLike
from typing import Any, BinaryIO, Protocol, Self, cast

from ._native import ClusterArchive as _Archive
from ._native import export_archive as _export_archive
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
    encode_user_path: bool = True,
    compression_level: int = 3,
) -> AsyncGenerator[ClusterArchive]:
    """Snapshot a stopped room through its Agent or an exclusive offline lock."""
    archive = ClusterArchive._from_native(
        await _export_archive(
            host._host,
            number,
            options={
                "configuration": configuration,
                "room_id": room_id,
                "encode_user_path": encode_user_path,
            },
            compression_level=compression_level,
        )
    )
    try:
        yield archive
    finally:
        archive.close()


__all__ = ["ArchiveUploadResult", "ClusterArchive", "SecretValue", "export_cluster"]
