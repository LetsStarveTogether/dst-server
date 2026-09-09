import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from ntpath import isreserved
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, BinaryIO

from pydantic import ConfigDict, Field, InstanceOf, SecretStr, validate_call

from dst_server.klei_id import encode_klei_id

from .configuration.files import PERMISSION_FILES, discover
from .configuration.models import ClusterConfig, ClusterSettings, ShardSettings
from .lua_codec import parse_return_table, render_literal

PUBLIC_PRIVACY = 0
CLAN_PRIVACY = 3


@dataclass(frozen=True, slots=True)
class ArchiveUploadResult:
    key: str
    url: str | None


@dataclass(frozen=True, slots=True)
class ClusterArchive:
    filename: str
    stream: BinaryIO

    @validate_call(config=ConfigDict(strict=True, hide_input_in_errors=True))
    def upload(
        self,
        *,
        bucket: str | None = None,
        endpoint: str | None = None,
        region: str = "auto",
        access_key_id: InstanceOf[SecretStr] | None = None,
        secret_access_key: InstanceOf[SecretStr] | None = None,
        session_token: InstanceOf[SecretStr] | None = None,
        object_prefix: Annotated[str, Field(pattern=r"^(?:[^/]|$)")] = "",
        url_prefix: str | None = None,
    ) -> ArchiveUploadResult:
        """Upload with explicit S3 settings overriding AWS environment variables."""
        from obstore.store import S3Store

        settings = {
            "aws_endpoint_url_s3": endpoint,
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
            "session_token": session_token,
        }
        store = S3Store(
            bucket,
            region=region,
            **{
                name: value.get_secret_value()
                if isinstance(value, SecretStr)
                else value
                for name, value in settings.items()
                if value is not None
            },
        )
        key = f"{object_prefix}{self.filename}"
        self.stream.seek(0)
        store.put(
            key,
            self.stream,
            attributes={"Content-Type": "application/x-7z-compressed"},
        )
        return ArchiveUploadResult(
            key=key,
            url=None if url_prefix is None else url_prefix + key.rsplit("/", 1)[-1],
        )


@contextmanager
def export_cluster(  # ruff: ignore[complex-structure, too-many-branches]
    directory: Path,
    *,
    configuration: ClusterConfig | None = None,
    room_id: str | None = None,
    encode_user_path: bool = True,
) -> Iterator[ClusterArchive]:
    """Yield an auto-closing archive stream for a saved, quiescent cluster."""
    from py7zr import FILTER_ZSTD, SevenZipFile

    directory = Path(directory).absolute()
    room_id = directory.name if room_id is None else room_id
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", room_id) is None:
        msg = "room_id must contain 1-80 ASCII letters, digits, underscores or hyphens"
        raise ValueError(msg)
    if directory.is_symlink() or not directory.is_dir():
        msg = f"export source must be a real directory: {directory}"
        raise ValueError(msg)
    if configuration is None:
        configuration = ClusterConfig.load(directory)
    else:
        configuration = ClusterConfig.model_validate(configuration)
        actual = {shard.name: shard.master for shard in discover(directory)}
        expected = {
            name: shard.settings.is_master
            for name, shard in configuration.shards.items()
        }
        if actual != expected:
            msg = "export configuration must match the source shard topology"
            raise ValueError(msg)
        if not encode_user_path and any(
            shard.settings.encode_user_path
            != ShardSettings.load(directory / name / "server.ini").encode_user_path
            for name, shard in configuration.shards.items()
        ):
            msg = "export configuration must preserve the source player path encoding"
            raise ValueError(msg)
    exported = configuration.replace(
        settings=ClusterSettings.model_validate(
            configuration.settings.model_dump(
                exclude_unset=True,
                exclude={
                    "cluster_password",
                    "cluster_key",
                    "steam_group_id",
                    "steam_group_only",
                    "steam_group_admins",
                },
            )
        ),
        shards={
            name: shard.replace(
                settings=shard.settings.replace(
                    cluster_key=None,
                    encode_user_path=True
                    if encode_user_path
                    else shard.settings.encode_user_path,
                )
            )
            for name, shard in configuration.shards.items()
        },
    )
    files = exported.files()
    for name in ("cluster_token.txt", *PERMISSION_FILES):
        del files[Path(name)]
    for path in files:
        _validate_archive_path(path)
    saves = _save_files(directory, configuration, encode_user_path)
    filename = f"DST-{room_id}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.7z"
    with TemporaryFile(mode="w+b") as stream:
        with SevenZipFile(
            stream, "w", filters=[{"id": FILTER_ZSTD, "level": 22}]
        ) as archive:
            for path, content in files.items():
                archive.writestr(content, (Path(room_id) / path).as_posix())
            for source, (target, expected) in saves.items():
                descriptor = os.open(
                    source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                with os.fdopen(descriptor, "rb") as saved:
                    if _file_state(os.fstat(saved.fileno())) != expected:
                        msg = f"save changed during export: {source}"
                        raise RuntimeError(msg)
                    arcname = (Path(room_id) / target).as_posix()
                    if target.parts[1:] == ("save", "shardindex"):
                        archive.writestr(
                            _export_shard_index(
                                saved.read(), encode_user_path=encode_user_path
                            ),
                            arcname,
                        )
                    else:
                        archive.writef(saved, arcname)
        if saves != _save_files(directory, configuration, encode_user_path):
            msg = "saves changed during export; use a quiescent copy or stop the games"
            raise RuntimeError(msg)
        del saves, files, exported, configuration
        stream.seek(0)
        yield ClusterArchive(filename, stream)


def _file_state(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _save_files(  # ruff: ignore[complex-structure, too-many-branches]
    directory: Path,
    configuration: ClusterConfig,
    encode_user_path: bool,
) -> dict[Path, tuple[Path, tuple[int, ...]]]:
    files = {}
    player_directories: dict[Path, Path] = {}
    for name in configuration.shards:
        root = directory / name / "save" / "session"
        for parent in (
            directory / name,
            root.parent,
            root,
            root.parent / "mod_config_data",
        ):
            if parent.is_symlink():
                msg = f"save directory cannot be a symlink: {parent}"
                raise ValueError(msg)
            if parent.exists() and not parent.is_dir():
                raise NotADirectoryError(parent)
        if not (directory / name).is_dir():
            raise FileNotFoundError(directory / name)
        convert_user_path = (
            encode_user_path
            and not ShardSettings.load(directory / name / "server.ini").encode_user_path
        )
        if (root.parent / "saveindex").exists() and not (
            root.parent / "shardindex"
        ).exists():
            msg = "legacy saveindex must be migrated by the game before export"
            raise ValueError(msg)
        paths = list(root.rglob("*"))
        paths.extend(root.parent.glob("mod_config_data/mod_worldjump_data_*"))
        for filename in ("shardindex", "recipebook", "reforged_achievements_server"):
            path = root.parent / filename
            if path.exists() or path.is_symlink():
                paths.append(path)
        for source in sorted(paths):
            if source.name == ".last_login" or source.name.startswith("..last_login."):
                continue
            state = source.lstat()
            if stat.S_ISDIR(state.st_mode) and source.is_relative_to(root):
                continue
            if not stat.S_ISREG(state.st_mode):
                msg = f"save entry must be a regular file or directory: {source}"
                raise ValueError(msg)
            target = source.relative_to(directory)
            _validate_archive_path(target)
            if not convert_user_path or not source.is_relative_to(root):
                files[source] = target, _file_state(state)
                continue
            parts = list(source.relative_to(root).parts)
            if parts[2:] and parts[1].startswith("KU_"):
                # DST appends one underscore to an unencoded online player ID.
                userid = (
                    parts[1][:-1]
                    if re.fullmatch(r"KU_[\w-]{8}_", parts[1], re.ASCII)
                    else parts[1]
                )
                parts[1] = encode_klei_id(userid)
            target = Path(name, "save", "session", *parts)
            if parts[2:]:
                original = root.joinpath(*source.relative_to(root).parts[:2])
                player = Path(name, *parts[:2])
                if player_directories.setdefault(player, original) != original:
                    msg = f"player save directories collide after encoding: {player}"
                    raise ValueError(msg)
            files[source] = target, _file_state(state)
    return files


def _validate_archive_path(path: Path) -> None:
    if any("\\" in part or isreserved(part) for part in path.parts):
        msg = f"save path is unsafe on Windows: {path}"
        raise ValueError(msg)


def _export_shard_index(source: bytes, *, encode_user_path: bool = True) -> bytes:
    """Keep the world index while removing credentials and Steam group settings."""
    try:
        text = source.decode("utf-8")
    except UnicodeError as error:
        msg = "shard index must contain valid UTF-8"
        raise ValueError(msg) from error
    index = parse_return_table(text, "shard index")
    server = index.get("server")
    if not isinstance(server, dict):
        msg = "shard index must contain a server table"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    for key in (
        "password",
        "cluster_password",
        "cluster_key",
        "cluster_token",
        "token",
        "clan",
    ):
        server.pop(key, None)
    if server.get("privacy_type") == CLAN_PRIVACY:
        server["privacy_type"] = PUBLIC_PRIVACY
    if encode_user_path:
        server["encode_user_path"] = True
    return f"KLEI     1 return {render_literal(index)}\n".encode()
