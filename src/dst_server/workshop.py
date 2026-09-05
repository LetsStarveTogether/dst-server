"""Download and install DST Workshop content without running the game binary."""

import asyncio
import fcntl
import json
import os
import re
import shutil
import stat
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from dst_server.cluster.mods import _validate_directory
from dst_server.cluster.overrides import MAX_WORKSHOP_ID
from dst_server.steamcmd import SteamCMD, absolute_path, positive_integer

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
        self.mods_directory = absolute_path("Mod directory", mods_directory).resolve()

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
        _validate_directory(directory)
        directory.mkdir(parents=True, exist_ok=True)
        owner = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _restore_missing(directory)
            async with asyncio.timeout(UPDATE_TIMEOUT):
                if collection_ids:
                    pending.update(await _collection_items(collection_ids))
                selected = tuple(sorted(pending))
                if not selected:
                    return ()
                cache = directory / "ugc" / "steamcmd"
                _validate_directory(cache.parent)
                _validate_directory(cache)
                cache.mkdir(parents=True, exist_ok=True)
                await self._download(pending, cache, attempts)
                return selected
        finally:
            os.close(owner)

    async def _download(self, pending: set[int], cache: Path, attempts: int) -> None:
        for attempt in range(1, attempts + 1):
            try:
                output = await self.steamcmd.execute_authenticated(
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
                    await _install_async(source, self.mods_directory, item)
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


async def _collection_items(collections: set[int]) -> set[int]:
    pending = set(collections)
    visited: set[int] = set()
    items: set[int] = set()
    while pending:
        collection = pending.pop()
        visited.add(collection)
        payload = await asyncio.to_thread(_get_collection, collection)
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            (detail,) = payload["response"]["collectiondetails"]
            if detail["result"] != 1 or detail["publishedfileid"] != str(collection):
                raise ValueError  # ruff: ignore[raise-within-try]
            for child in detail["children"]:
                raw = child["publishedfileid"]
                if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
                    raise ValueError  # ruff: ignore[raise-within-try]
                item = _workshop_id(int(child["publishedfileid"]))
                if child["filetype"] == COLLECTION_FILE_TYPE:
                    if item not in visited:
                        pending.add(item)
                elif child["filetype"] == 0:
                    items.add(item)
                else:
                    raise ValueError  # ruff: ignore[raise-within-try]
        except (KeyError, TypeError, ValueError) as error:
            msg = f"invalid or unavailable Workshop collection: {collection}"
            raise ValueError(msg) from error
    return items


def _get_collection(collection: int) -> Any:
    data = urllib.parse.urlencode({
        "collectioncount": "1",
        "publishedfileids[0]": str(collection),
    }).encode("ascii")
    request = urllib.request.Request(COLLECTION_URL, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:  # ruff: ignore[suspicious-url-open-usage]
        return json.load(response)


async def _install_async(source: Path, directory: Path, item: int) -> None:
    task = asyncio.create_task(asyncio.to_thread(_install, source, directory, item))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as primary:
        # A copying thread cannot be cancelled; retain the directory lock until done.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception as error:
                raise primary from error
        try:
            task.result()
        except BaseException as error:
            raise primary from error
        raise


def _restore_missing(directory: Path) -> None:
    for previous in directory.glob(".workshop-*.previous"):
        item = previous.name.removeprefix(".workshop-").removesuffix(".previous")
        if not item.isascii() or not item.isdecimal():
            continue
        target = directory / f"workshop-{_workshop_id(int(item))}"
        _validate_directory(previous)
        _validate_directory(target)
        if not target.exists():
            previous.rename(target)


def _install(source: Path, directory: Path, item: int) -> None:
    if stat.S_IFMT(source.lstat().st_mode) not in {stat.S_IFREG, stat.S_IFDIR}:
        msg = f"unsupported Workshop content: {source}"
        raise ValueError(msg)
    target = directory / f"workshop-{item}"
    previous = directory / f".workshop-{item}.previous"
    _validate_directory(target)
    _validate_directory(previous)
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
