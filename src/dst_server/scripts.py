"""Build and verify the SDK's native, non-mod DST script bundle."""

import hashlib
import os
import stat
from dataclasses import dataclass, replace
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import orjson
from luaparser.astnodes import Call, Name, String
from pydantic import Field

from dst_server.lua_codec import parse_statements
from dst_server.models.base import FrozenModel

ENTRYPOINT = "scripts/globalvariableoverrides.lua"
MANIFEST = "scripts/dst_server_bundle.json"
ORIGINAL_ENTRYPOINT = b"-- Intentionally blank\n"
BOOTSTRAP = b'require("dst_server.bootstrap").start()\n'
LUA_DIRECTORY = Path(__file__).with_name("lua")
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 20_000
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Manifest(FrozenModel):
    format: Literal[1] = 1
    sdk_version: str
    source_digest: Digest
    files: dict[str, Digest]
    sdk_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScriptBundle:
    path: Path
    sdk_version: str
    source_digest: str
    native_files: int
    sdk_files: int
    entrypoint: str = ENTRYPOINT


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _source_digest(files: dict[str, str]) -> str:
    return _digest(orjson.dumps(files, option=orjson.OPT_SORT_KEYS))


def _owned(name: str) -> bool:
    return name.startswith("scripts/dst_server/") or name in {
        "scripts/dst_server.lua",
        "scripts/components/dst_server_runtime.lua",
    }


def _payload() -> dict[str, bytes]:
    files = {
        "scripts/" + path.relative_to(LUA_DIRECTORY).as_posix(): path.read_bytes()
        for path in sorted(LUA_DIRECTORY.rglob("*.lua"))
    }
    required = {
        "scripts/dst_server.lua",
        "scripts/dst_server/bootstrap.lua",
        "scripts/components/dst_server_runtime.lua",
    }
    if not required <= files.keys() or not all(map(_owned, files)):
        message = "SDK Lua resources are incomplete or outside the bundle namespace"
        raise ValueError(message)
    return files


def _entries(archive: ZipFile) -> dict[str, ZipInfo]:
    entries: dict[str, ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if any((
            info.orig_filename != name,
            "\\" in name,
            path.is_absolute(),
            ".." in path.parts,
            path.as_posix() != name.rstrip("/"),
            not name.startswith("scripts/"),
            name in entries,
            stat.S_ISLNK(info.external_attr >> 16),
            info.flag_bits & 1,
            info.compress_type not in {ZIP_STORED, ZIP_DEFLATED},
        )):
            message = f"unsupported or duplicate script archive entry: {name!r}"
            raise ValueError(message)
        total += info.file_size
        if info.file_size > MAX_ENTRY_BYTES or total > MAX_ARCHIVE_BYTES:
            message = "script archive exceeds the supported uncompressed size"
            raise ValueError(message)
        entries[name] = info
    if len(entries) > MAX_ENTRIES:
        message = "script archive contains too many entries"
        raise ValueError(message)
    if not {ENTRYPOINT, "scripts/main.lua"} <= entries.keys():
        message = "archive does not contain the native DST startup scripts"
        raise ValueError(message)
    return entries


def _inspect(
    archive: ZipFile, *, current_sdk: bool
) -> tuple[dict[str, ZipInfo], dict[str, str], _Manifest | None]:
    entries = _entries(archive)
    # A comment, function body or disabled branch cannot serve as the boot entrypoint.
    for statement in parse_statements(
        archive.read("scripts/main.lua").decode(), "native main.lua"
    ):
        match statement:
            case Call(
                func=Name(id="require"), args=[String(s=b"globalvariableoverrides")]
            ):
                break
    else:
        message = "native main.lua no longer loads globalvariableoverrides at top level"
        raise ValueError(message)
    manifest = (
        _Manifest.model_validate_json(archive.read(MANIFEST))
        if MANIFEST in entries
        else None
    )
    files = {
        name: _digest(archive.read(info))
        for name, info in entries.items()
        if name != MANIFEST
    }
    if manifest is None:
        if archive.read(ENTRYPOINT) != ORIGINAL_ENTRYPOINT:
            message = (
                "native globalvariableoverrides.lua has changed; "
                "review the startup entrypoint"
            )
            raise ValueError(message)
        if any(_owned(name) for name in files):
            message = "unmanaged archive already contains files in the SDK namespace"
            raise ValueError(message)
        return entries, files, None
    if (
        manifest.files != files
        or len(set(manifest.sdk_files)) != len(manifest.sdk_files)
        or set(manifest.sdk_files) != {name for name in files if _owned(name)}
        or files.get(ENTRYPOINT) != _digest(BOOTSTRAP)
    ):
        message = "managed script bundle contents do not match its manifest"
        raise ValueError(message)
    native = {name: digest for name, digest in files.items() if not _owned(name)}
    native[ENTRYPOINT] = _digest(ORIGINAL_ENTRYPOINT)
    if _source_digest(native) != manifest.source_digest:
        message = "managed script bundle native contents do not match its source digest"
        raise ValueError(message)
    if current_sdk:
        expected = {name: _digest(data) for name, data in _payload().items()}
        actual = {name: files[name] for name in manifest.sdk_files}
        if actual != expected or manifest.sdk_version != version("dst-server"):
            message = "managed script bundle does not match the installed SDK"
            raise ValueError(message)
    return entries, native, manifest


def verify_bundle(path: Path, *, source: Path | None = None) -> ScriptBundle:
    """Verify every file and the installed SDK; optionally compare a native source.

    The manifest detects corruption, not authenticity. Supply a trusted source
    archive when checking provenance independently of the embedded manifest.

    Returns:
        The verified bundle's provenance and file counts.

    Raises:
        ValueError: The archive is unmanaged, incompatible, or has invalid contents.
    """
    path = Path(path)
    with ZipFile(path) as archive:
        _, native, manifest = _inspect(archive, current_sdk=True)
    if manifest is None:
        message = "script archive has not been built with the SDK"
        raise ValueError(message)
    if source is not None:
        with ZipFile(source) as archive:
            _, original, _ = _inspect(archive, current_sdk=False)
        if native != original:
            message = "script bundle does not preserve the supplied native source"
            raise ValueError(message)
    return ScriptBundle(
        path.absolute(),
        manifest.sdk_version,
        manifest.source_digest,
        len(native),
        len(manifest.sdk_files),
    )


def build_bundle(source: Path, output: Path) -> ScriptBundle:
    """Atomically build, rebuild or upgrade a native script bundle.

    Source and output may be the same path. Run before the game starts, as
    replacing a running game's resource bundle cannot update its loaded Lua VM.

    Returns:
        The published, verified bundle's provenance and file counts.
    """
    source, output = Path(source), Path(output)
    payload = _payload()
    temporary: Path | None = None
    try:  # ruff: ignore[too-many-nested-blocks]
        with ZipFile(source) as original:
            entries, native, _ = _inspect(original, current_sdk=False)
            files = (
                native
                | {ENTRYPOINT: _digest(BOOTSTRAP)}
                | {name: _digest(data) for name, data in payload.items()}
            )
            manifest = _Manifest(
                sdk_version=version("dst-server"),
                source_digest=_source_digest(native),
                files=files,
                sdk_files=tuple(sorted(payload)),
            )
            with NamedTemporaryFile(
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                with ZipFile(stream, "w", compression=ZIP_DEFLATED) as bundled:
                    bundled.comment = original.comment
                    for name, info in entries.items():
                        if name == MANIFEST or _owned(name):
                            continue
                        bundled.writestr(
                            info,
                            BOOTSTRAP if name == ENTRYPOINT else original.read(info),
                        )
                    for name, data in payload.items():
                        info = ZipInfo(name)
                        info.compress_type = ZIP_DEFLATED
                        info.external_attr = (stat.S_IFREG | 0o644) << 16
                        bundled.writestr(info, data)
                    info = ZipInfo(MANIFEST)
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    bundled.writestr(info, manifest.model_dump_json().encode() + b"\n")
                stream.flush()
                os.fchmod(stream.fileno(), stat.S_IMODE(source.stat().st_mode))
                os.fsync(stream.fileno())
        result = verify_bundle(temporary)
        temporary.replace(output)
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return replace(result, path=output.absolute())
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
