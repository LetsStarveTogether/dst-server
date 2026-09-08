import asyncio
import stat
import threading
import traceback
import urllib.parse
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx2
import pytest

from dst_server.mods import SteamCMD, WorkshopUpdater, workshop


def make_updater(tmp_path: Path) -> WorkshopUpdater:
    return WorkshopUpdater(SteamCMD("steamcmd"), tmp_path / "mods")


def cached_mod(updater: WorkshopUpdater, item: int) -> Path:
    source = (
        updater.mods_directory
        / "ugc/steamcmd/steamapps/workshop/content/322330"
        / str(item)
    )
    source.mkdir(parents=True)
    (source / "modinfo.lua").write_text('version = "new"\n')
    return source


def old_mod(updater: WorkshopUpdater, item: int) -> Path:
    target = updater.mods_directory / f"workshop-{item}"
    target.mkdir(parents=True)
    (target / "modinfo.lua").write_text('version = "old"\n')
    (target / "obsolete.lua").write_text("old file\n")
    return target


def success(item: int, source: Path) -> str:
    return f'Success. Downloaded item {item} to "{source}" (20 bytes)\n'


async def test_retries_only_unconfirmed_items_and_preserves_them_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = make_updater(tmp_path)
    source = cached_mod(updater, 1)
    cached_mod(updater, 2)
    previous = old_mod(updater, 2)
    calls = []

    def execute(commands: list[tuple[str, str, str]], **kwargs: object) -> str:
        calls.append((commands, kwargs))
        assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'
        return success(1, source)

    monkeypatch.setattr(updater.steamcmd, "run", AsyncMock(side_effect=execute))

    with pytest.raises(RuntimeError, match=r"did not confirm.*\[2\]"):
        await updater.update([2, 1, 1], attempts=2)

    assert [commands for commands, _ in calls] == [
        [
            ("workshop_download_item", "322330", "1"),
            ("workshop_download_item", "322330", "2"),
        ],
        [("workshop_download_item", "322330", "2")],
    ]
    assert all(
        kwargs == {"install_dir": updater.mods_directory / "ugc/steamcmd"}
        for _, kwargs in calls
    )
    assert (updater.mods_directory / "workshop-1/modinfo.lua").read_text() == (
        'version = "new"\n'
    )
    assert (previous / "obsolete.lua").read_text() == "old file\n"


async def test_retries_nonzero_exit_with_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    execute = AsyncMock(side_effect=ChildProcessError("SteamCMD failed"))
    monkeypatch.setattr(updater.steamcmd, "run", execute)

    with pytest.raises(ChildProcessError, match="SteamCMD failed"):
        await updater.update([1], attempts=3)

    assert execute.await_count == 3
    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'


@pytest.mark.parametrize(
    "fail_collections", [False, True], ids=["download", "collection"]
)
async def test_interrupted_publish_recovers_before_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_collections: bool
) -> None:
    updater = make_updater(tmp_path)
    target = old_mod(updater, 1)
    previous = updater.mods_directory / ".workshop-1.previous"
    target.rename(previous)
    monkeypatch.setattr(
        updater.steamcmd,
        "run",
        AsyncMock(side_effect=ChildProcessError("SteamCMD failed")),
    )
    monkeypatch.setattr(
        workshop,
        "_collection_items",
        AsyncMock(side_effect=ValueError("collection failed")),
    )

    failure = ValueError if fail_collections else ChildProcessError
    with pytest.raises(failure, match="failed"):
        await updater.update(
            [1], collections=[100] if fail_collections else [], attempts=1
        )

    assert (target / "modinfo.lua").read_text() == 'version = "old"\n'
    assert (target / "obsolete.lua").read_text() == "old file\n"
    assert not previous.exists()


async def test_recovers_from_nonzero_exit_and_installs_both_content_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = make_updater(tmp_path)
    ugc = cached_mod(updater, 1)
    legacy_directory = cached_mod(updater, 2)
    legacy = legacy_directory / "legacy.bin"
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr("modinfo.lua", 'version = "legacy"\n')
        archive.writestr("scripts/feature.lua", "return true\n")
    old_mod(updater, 1)
    old_mod(updater, 2)
    execute = AsyncMock(
        side_effect=[
            ChildProcessError("transient failure"),
            success(1, ugc) + success(2, legacy),
        ]
    )
    monkeypatch.setattr(updater.steamcmd, "run", execute)

    assert await updater.update([2, 1], attempts=2) == (1, 2)

    assert execute.await_count == 2
    assert (updater.mods_directory / "workshop-1/modinfo.lua").read_text() == (
        'version = "new"\n'
    )
    assert (updater.mods_directory / "workshop-2/modinfo.lua").read_text() == (
        'version = "legacy"\n'
    )
    assert (updater.mods_directory / "workshop-2/scripts/feature.lua").read_text() == (
        "return true\n"
    )
    assert not list(updater.mods_directory.glob("workshop-*/obsolete.lua"))
    assert not list(updater.mods_directory.glob(".workshop-*"))


async def test_rejects_success_path_outside_item_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    source = cached_mod(updater, 2)
    monkeypatch.setattr(
        updater.steamcmd,
        "run",
        AsyncMock(return_value=success(1, source)),
    )

    with pytest.raises(ValueError, match="unexpected Workshop download path"):
        await updater.update([1])

    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'


@pytest.mark.parametrize("member", ["../escaped.lua", "/escaped.lua", "x\\escaped.lua"])
def test_archive_rejects_unsafe_paths_and_preserves_installed_mod(
    tmp_path: Path, member: str
) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    archive_path = tmp_path / "legacy.bin"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("modinfo.lua", 'version = "new"\n')
        archive.writestr(member, "bad\n")

    with pytest.raises(ValueError, match="unsafe Workshop archive member"):
        workshop._install(archive_path, updater.mods_directory, 1)

    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'
    assert not (tmp_path / "escaped.lua").exists()
    assert not list(updater.mods_directory.glob(".workshop-*"))


def test_archive_rejects_symlinks_and_preserves_installed_mod(tmp_path: Path) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    archive_path = tmp_path / "legacy.bin"
    symlink = zipfile.ZipInfo("linked.lua")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("modinfo.lua", 'version = "new"\n')
        archive.writestr(symlink, "../escaped.lua")

    with pytest.raises(ValueError, match="unsafe Workshop archive member"):
        workshop._install(archive_path, updater.mods_directory, 1)

    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'


def test_ugc_rejects_symlinks_and_preserves_installed_mod(tmp_path: Path) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    source = cached_mod(updater, 1)
    (source / "linked.lua").symlink_to(previous / "modinfo.lua")

    with pytest.raises(ValueError, match="unsupported Workshop file"):
        workshop._install(source, updater.mods_directory, 1)

    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'


def test_missing_modinfo_preserves_installed_mod(tmp_path: Path) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    source = cached_mod(updater, 1)
    (source / "modinfo.lua").unlink()

    with pytest.raises(ValueError, match=r"has no modinfo\.lua"):
        workshop._install(source, updater.mods_directory, 1)

    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'


def test_failed_publish_restores_previous_mod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = make_updater(tmp_path)
    previous = old_mod(updater, 1)
    source = cached_mod(updater, 1)
    rename = Path.rename

    def fail_publish(path: Path, target: Path) -> Path:
        if path.name == "content":
            msg = "publish failed"
            raise OSError(msg)
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publish)

    with pytest.raises(OSError, match="publish failed"):
        workshop._install(source, updater.mods_directory, 1)

    assert (previous / "modinfo.lua").read_text() == 'version = "old"\n'
    assert (previous / "obsolete.lua").read_text() == "old file\n"
    assert not list(updater.mods_directory.glob(".workshop-*"))


async def test_update_unions_collection_items_with_explicit_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = make_updater(tmp_path)
    one = cached_mod(updater, 1)
    two = cached_mod(updater, 2)
    collection_items = AsyncMock(return_value={1, 2})
    execute = AsyncMock(return_value=success(1, one) + success(2, two))
    monkeypatch.setattr(workshop, "_collection_items", collection_items)
    monkeypatch.setattr(updater.steamcmd, "run", execute)

    assert await updater.update([2], collections=[100, 100]) == (1, 2)

    collection_items.assert_awaited_once_with({100}, None)
    execute.assert_awaited_once_with(
        [
            ("workshop_download_item", "322330", "1"),
            ("workshop_download_item", "322330", "2"),
        ],
        install_dir=updater.mods_directory / "ugc/steamcmd",
    )


@pytest.mark.parametrize("fail_install", [False, True], ids=["success", "failure"])
async def test_cancellation_keeps_directory_locked_until_install_worker_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_install: bool
) -> None:
    updater = make_updater(tmp_path)
    source = cached_mod(updater, 1)
    started = threading.Event()
    release = threading.Event()

    def install(_source: Path, _directory: Path, _item: int) -> None:
        started.set()
        assert release.wait(timeout=5)
        if fail_install:
            msg = "install failed"
            raise OSError(msg)

    monkeypatch.setattr(workshop, "_install", install)
    monkeypatch.setattr(
        updater.steamcmd,
        "run",
        AsyncMock(return_value=success(1, source)),
    )
    task = asyncio.create_task(updater.update([1]))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(BlockingIOError):
            await make_updater(tmp_path).update([])
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(BlockingIOError):
            await make_updater(tmp_path).update([])
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await task

    if fail_install:
        assert isinstance(cancellation.value.__cause__, OSError)
        assert str(cancellation.value.__cause__) == "install failed"
    else:
        assert cancellation.value.__cause__ is None
    assert await make_updater(tmp_path).update([]) == ()


@pytest.mark.parametrize("proxy", [None, "http://user:secret@proxy.invalid:1080"])
async def test_collections_expand_recursively_with_explicit_proxy_only(
    monkeypatch: pytest.MonkeyPatch, proxy: str | None
) -> None:
    monkeypatch.setenv("https_proxy", "http://inherited.invalid")
    monkeypatch.setenv("no_proxy", "*")
    collections = {100: [(200, 2), (1, 0)], 200: [(100, 2), (1, 0), (2, 0)]}
    requested = []
    clients = []
    original_client = httpx2.AsyncClient

    def respond(request: httpx2.Request) -> httpx2.Response:
        collection = int(
            urllib.parse.parse_qs(request.content.decode())["publishedfileids[0]"][0]
        )
        requested.append(collection)
        assert str(request.url) == workshop.COLLECTION_URL
        assert request.method == "POST"
        return httpx2.Response(
            200,
            json={
                "response": {
                    "collectiondetails": [
                        {
                            "result": 1,
                            "publishedfileid": str(collection),
                            "children": [
                                {"publishedfileid": str(item), "filetype": kind}
                                for item, kind in collections[collection]
                            ],
                        }
                    ]
                }
            },
        )

    def client(**options: object) -> httpx2.AsyncClient:
        assert options == {"proxy": proxy, "trust_env": False, "timeout": 30}
        value = original_client(
            transport=httpx2.MockTransport(respond), trust_env=False
        )
        clients.append(value)
        return value

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    assert await workshop._collection_items({100, 200}, proxy) == {1, 2}
    assert sorted(requested) == [100, 200]
    assert len(clients) == 1
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "detail",
    [
        {"result": 9, "publishedfileid": "100"},
        {"result": True, "publishedfileid": "100", "children": []},
        {"result": 1, "publishedfileid": "999", "children": []},
        {"result": 1, "publishedfileid": "100"},
        *(
            {
                "result": 1,
                "publishedfileid": "100",
                "children": [{"publishedfileid": item, "filetype": kind}],
            }
            for item, kind in [
                ("1", 1),
                ("1", False),
                ("01", 0),
                ("0", 0),
                ("18446744073709551616", 0),
                ("\uff11\uff12", 0),
            ]
        ),
    ],
)
async def test_collections_reject_unavailable_or_malformed_details(
    monkeypatch: pytest.MonkeyPatch, detail: dict[str, object]
) -> None:
    original_client = httpx2.AsyncClient
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200, json={"response": {"collectiondetails": [detail]}}
        )
    )
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **_options: original_client(transport=transport, trust_env=False),
    )
    with pytest.raises(
        ValueError, match="invalid or unavailable Workshop collection: 100"
    ):
        await workshop._collection_items({100})


@pytest.mark.parametrize("cancel_request", [False, True], ids=["close", "request"])
@pytest.mark.parametrize("fail_close", [False, True], ids=["closed", "close-failed"])
async def test_repeated_collection_cancellation_finishes_transport_cleanup(
    monkeypatch: pytest.MonkeyPatch, cancel_request: bool, fail_close: bool
) -> None:
    started = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    sensitive_detail = "synthetic-proxy-password"

    class SlowCloseTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx2.Request
        ) -> httpx2.Response:
            assert str(request.url) == workshop.COLLECTION_URL
            started.set()
            if cancel_request:
                await asyncio.Event().wait()
            return httpx2.Response(
                200,
                json={
                    "response": {
                        "collectiondetails": [
                            {"result": 1, "publishedfileid": "100", "children": []}
                        ]
                    }
                },
            )

        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            closed.set()
            if fail_close:
                raise httpx2.ProxyError(sensitive_detail)

    client = httpx2.AsyncClient(transport=SlowCloseTransport(), trust_env=False)
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **_options: client)
    task = asyncio.create_task(workshop._collection_items({100}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if not cancel_request:
            await asyncio.wait_for(closing.wait(), 1)
        task.cancel("first cancellation")
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        finished_before_cleanup = task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await task

    assert not finished_before_cleanup
    assert closed.is_set()
    assert client.is_closed
    assert cancellation.value.args == ("first cancellation",)
    assert sensitive_detail not in "".join(
        traceback.format_exception(cancellation.value)
    )


@pytest.mark.parametrize("failure", ["proxy", "redirect", "close", "url"])
async def test_collection_http_errors_do_not_expose_sensitive_details(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    sensitive_detail = "synthetic-proxy-password-or-location-token"
    requested = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requested.append(request)
        if failure == "proxy":
            raise httpx2.ProxyError(sensitive_detail)
        if failure == "redirect":
            return httpx2.Response(
                302,
                headers={
                    "Location": f"https://redirect.invalid/?token={sensitive_detail}"
                },
            )
        return httpx2.Response(
            200,
            json={
                "response": {
                    "collectiondetails": [
                        {"result": 1, "publishedfileid": "100", "children": []}
                    ]
                }
            },
        )

    transport = httpx2.MockTransport(respond)
    client = httpx2.AsyncClient(transport=transport, trust_env=False)
    if failure == "close":
        monkeypatch.setattr(
            transport,
            "aclose",
            AsyncMock(side_effect=httpx2.ProxyError(sensitive_detail)),
        )

    def create_client(**_options: object) -> httpx2.AsyncClient:
        if failure == "url":
            raise httpx2.InvalidURL(sensitive_detail)
        return client

    monkeypatch.setattr(httpx2, "AsyncClient", create_client)
    try:
        with pytest.raises(
            RuntimeError, match="Workshop collection request failed"
        ) as error:
            await workshop._collection_items({100})

        assert sensitive_detail not in "".join(traceback.format_exception(error.value))
        assert len(requested) == (0 if failure == "url" else 1)
        assert all("Authorization" not in request.headers for request in requested)
        assert all(
            "Proxy-Authorization" not in request.headers for request in requested
        )
        if failure != "url":
            assert client.is_closed
    finally:
        await client.aclose()


def test_workshop_rejects_symlinked_mod_directory(tmp_path: Path) -> None:
    directory = tmp_path / "real"
    directory.mkdir()
    linked = tmp_path / "mods"
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        WorkshopUpdater(SteamCMD("steamcmd"), linked)
