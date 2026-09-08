"""Download and install DST Workshop content without running the game binary."""

import asyncio
import fcntl
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

import httpx2

from dst_server.concurrency import complete
from dst_server.configuration.overrides import MAX_WORKSHOP_ID

from .files import validate_directory
from .process import positive_integer
from .steamcmd import SteamCMD, absolute_path

APP_ID = 322330
COLLECTION_FILE_TYPE = 2
UPDATE_TIMEOUT = 30 * 60
COLLECTION_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
)
DOWNLOAD_SUCCESS = re.compile(
    r'Success\. Downloaded item (\d+) to "([^"\r\n]+)" \(\d+ bytes\)'
)


class WorkshopUpdater:
    """Keep SteamCMD's cache and install complete Mods into ``workshop-ID``.

    Stop game processes before updating their shared Mod directory.
    SteamCMD owns download metadata; installed Mods need no additional database.
    """

    def __init__(self, steamcmd: SteamCMD, mods_directory: Path) -> None:
        self.steamcmd = steamcmd
        directory = absolute_path("Mod directory", mods_directory)
        validate_directory(directory)
        self.mods_directory = directory.resolve()

    async def update(
        self,
        items: Iterable[int],
        *,
        collections: Iterable[int] = (),
        attempts: int = 5,
    ) -> tuple[int, ...]:
        attempts = positive_integer("Mod update attempts", attempts)
        pending = {_workshop_id(item) for item in items}
        collection_ids = {_workshop_id(item) for item in collections}
        directory = self.mods_directory
        validate_directory(directory)
        directory.mkdir(parents=True, exist_ok=True)
        owner = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _restore_missing(directory)
            async with asyncio.timeout(UPDATE_TIMEOUT):
                if collection_ids:
                    pending.update(
                        await _collection_items(collection_ids, self.steamcmd.proxy)
                    )
                selected = tuple(sorted(pending))
                if not selected:
                    return ()
                cache = directory / "ugc" / "steamcmd"
                validate_directory(cache.parent)
                validate_directory(cache)
                cache.mkdir(parents=True, exist_ok=True)
                await self._download(pending, cache, attempts)
                return selected
        finally:
            os.close(owner)

    async def _download(self, pending: set[int], cache: Path, attempts: int) -> None:
        for attempt in range(1, attempts + 1):
            try:
                output = await self.steamcmd.run(
                    [
                        ("workshop_download_item", str(APP_ID), str(item))
                        for item in sorted(pending)
                    ],
                    install_dir=cache,
                )
            except ChildProcessError:
                if attempt == attempts:
                    raise
            else:
                downloaded = {
                    int(item): Path(path)
                    for item, path in DOWNLOAD_SUCCESS.findall(output)
                }
                for item in sorted(pending.intersection(downloaded)):
                    source = downloaded[item]
                    expected = (
                        cache
                        / "steamapps"
                        / "workshop"
                        / "content"
                        / str(APP_ID)
                        / str(item)
                    )
                    if (
                        expected not in {source, source.parent}
                        or source.resolve() != source
                    ):
                        msg = f"unexpected Workshop download path: {source}"
                        raise ValueError(msg)
                    await complete(
                        asyncio.to_thread(_install, source, self.mods_directory, item)
                    )
                    pending.remove(item)
                if not pending:
                    return
            if self.steamcmd.log_handler is not None and attempt < attempts:
                self.steamcmd.log_handler(
                    f"Workshop update attempt {attempt}/{attempts} incomplete; "
                    f"retrying {sorted(pending)} with the existing cache."
                )
        msg = f"SteamCMD did not confirm Workshop downloads: {sorted(pending)}"
        raise RuntimeError(msg)


def _workshop_id(value: int) -> int:
    value = positive_integer("Workshop ID", value)
    if value > MAX_WORKSHOP_ID:
        msg = f"Workshop ID exceeds uint64: {value}"
        raise ValueError(msg)
    return value


async def _collection_items(
    collections: set[int], proxy: str | None = None
) -> set[int]:
    pending = set(collections)
    visited: set[int] = set()
    items: set[int] = set()
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        client = httpx2.AsyncClient(proxy=proxy, trust_env=False, timeout=30)
        primary: BaseException | None = None
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while pending:
                collection = pending.pop()
                visited.add(collection)
                response = await client.post(
                    COLLECTION_URL,
                    data={
                        "collectioncount": "1",
                        "publishedfileids[0]": str(collection),
                    },
                )
                response.raise_for_status()
                for item, kind in _collection_children(response.json(), collection):
                    if kind != COLLECTION_FILE_TYPE:
                        items.add(item)
                    elif item not in visited:
                        pending.add(item)
        except BaseException as error:
            primary = error
        try:
            await complete(client.aclose())
        except BaseException as error:
            primary = primary or error
        if primary is not None:
            # Cleanup cancellation must not replace the first failure or expose
            # an HTTP exception chained by complete().
            raise primary from None
    except httpx2.HTTPError, httpx2.InvalidURL:
        msg = "Workshop collection request failed"
        raise RuntimeError(msg) from None
    return items


def _collection_children(payload: Any, collection: int) -> list[tuple[int, int]]:
    children = []
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        (detail,) = payload["response"]["collectiondetails"]
        if (
            type(detail["result"]) is not int
            or detail["result"] != 1
            or detail["publishedfileid"] != str(collection)
        ):
            raise ValueError  # ruff: ignore[raise-within-try]
        for child in detail["children"]:
            raw, kind = child["publishedfileid"], child["filetype"]
            if (
                not isinstance(raw, str)
                or re.fullmatch(r"[1-9][0-9]*", raw) is None
                or type(kind) is not int
                or kind not in {0, COLLECTION_FILE_TYPE}
            ):
                raise ValueError  # ruff: ignore[raise-within-try]
            children.append((_workshop_id(int(raw)), kind))
    except (KeyError, TypeError, ValueError) as error:
        msg = f"invalid or unavailable Workshop collection: {collection}"
        raise ValueError(msg) from error
    return children


def _restore_missing(directory: Path) -> None:
    for previous in directory.glob(".workshop-*.previous"):
        item = previous.name.removeprefix(".workshop-").removesuffix(".previous")
        if not item.isascii() or not item.isdecimal():
            continue
        target = directory / f"workshop-{_workshop_id(int(item))}"
        validate_directory(previous)
        validate_directory(target)
        if not target.exists():
            previous.rename(target)


def _install(source: Path, directory: Path, item: int) -> None:
    if stat.S_IFMT(source.lstat().st_mode) not in {stat.S_IFREG, stat.S_IFDIR}:
        msg = f"unsupported Workshop content: {source}"
        raise ValueError(msg)
    target = directory / f"workshop-{item}"
    previous = directory / f".workshop-{item}.previous"
    validate_directory(target)
    validate_directory(previous)
    if previous.exists():
        if target.exists():
            shutil.rmtree(previous)
        else:
            previous.rename(target)
    with TemporaryDirectory(prefix=f".workshop-{item}-", dir=directory) as temporary:
        staged = Path(temporary) / "content"
        _stage_contents(source, staged)
        if not (staged / "modinfo.lua").is_file():
            msg = f"Workshop item {item} has no modinfo.lua"
            raise ValueError(msg)
        if target.exists():
            target.rename(previous)
        try:
            staged.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
        if previous.exists():
            shutil.rmtree(previous)


def _stage_contents(source: Path, staged: Path) -> None:
    if source.is_dir():
        for root, directories, files in os.walk(source):
            for name in directories + files:
                path = Path(root) / name
                if stat.S_IFMT(path.lstat().st_mode) not in {
                    stat.S_IFREG,
                    stat.S_IFDIR,
                }:
                    msg = f"unsupported Workshop file: {path}"
                    raise ValueError(msg)
        # ponytail: copy full Mods each update; compare revisions if this is costly.
        shutil.copytree(source, staged)
    else:
        _extract(source, staged)


def _extract(source: Path, target: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        names: set[PurePosixPath] = set()
        for member in members:
            path = PurePosixPath(member.filename)
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (
                path.is_absolute()
                or ".." in path.parts
                or any(char in member.orig_filename for char in ("\\", "\0"))
                or path in names
                or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
            ):
                msg = f"unsafe Workshop archive member: {member.orig_filename!r}"
                raise ValueError(msg)
            names.add(path)
        available = shutil.disk_usage(target.parent).free
        if sum(member.file_size for member in members) > available:
            msg = f"insufficient disk space for Workshop archive: {source}"
            raise OSError(msg)
        # Reading every member verifies its ZIP CRC before the directory is published.
        archive.extractall(target, members=members)
