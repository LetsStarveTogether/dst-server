import asyncio
import os
import sys
from pathlib import Path

import orjson
import pytest

from dst_server.mods import native as mods
from tests.helpers import process_stopped
from tests.mods.test_files import write_updater

COMPLETE = (
    "[00:00:31]: FinishDownloadingServerMods Complete! Process trying to quit nicely.."
)
TIMEOUT = "[00:00:31]: DownloadServerMods timed out with no response from Workshop..."
UPDATER = (
    f"#!{sys.executable}\n"
    r"""import os
import sys
import time
from pathlib import Path

ugc = Path(sys.argv[sys.argv.index("-ugc_directory") + 1])
counter = ugc / "attempts"
attempt = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(attempt))
(ugc / "pid").write_text(str(os.getpid()))
partial = ugc / "partial-download"
if attempt > 1:
    assert partial.read_bytes() == b"retained download"
partial.write_bytes(b"retained download")
"""
)


def native_updater(
    tmp_path: Path,
    outputs: list[str],
    *,
    returncode: int = 0,
) -> tuple[Path, Path]:
    source = (
        UPDATER
        + f"\noutputs = {outputs!r}\n"
        + "print(outputs[min(attempt, len(outputs)) - 1], end='', flush=True)\n"
        + f"sys.exit({returncode!r})\n"
    )
    return write_updater(tmp_path, source)


@pytest.mark.parametrize("proxy", [None, "http://user:secret@127.0.0.1:1080"])
async def test_native_download_uses_only_explicit_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxy: str | None
) -> None:
    variables = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy")
    for name in (*variables, *(name.upper() for name in variables)):
        monkeypatch.setenv(name, "http://inherited.invalid")
    monkeypatch.setenv("DST_KEEP_ENV", "retained")
    executable, ugc = write_updater(
        tmp_path,
        UPDATER + "\nimport orjson\n"
        '(ugc / "environment").write_bytes(orjson.dumps(dict(os.environ)))\n'
        + 'print(os.environ.get("http_proxy", "direct"))\n'
        + f"print({COMPLETE!r})\n",
    )
    lines: list[str] = []

    await mods.update(executable, ugc, proxy=proxy, log_handler=lines.append)

    environment = orjson.loads((ugc / "environment").read_text())
    assert environment["DST_KEEP_ENV"] == "retained"
    assert {
        name: value for name, value in environment.items() if name.lower() in variables
    } == ({"http_proxy": proxy, "https_proxy": proxy} if proxy else {})
    assert "secret" not in "\n".join(lines)
    assert os.environ["http_proxy"] == "http://inherited.invalid"


@pytest.mark.parametrize(
    "failure",
    [
        TIMEOUT,
        "[00:00:31]: [Workshop] ItemQuery failed entirely, unrecoverable.",
        "[00:00:31]: [Workshop] CollectionQuery failed entirely, unrecoverable.",
        "[00:00:31]: [Workshop] ODPF failed entirely: 16",
        "[00:00:31]: [Workshop] FAILED: DownloadPublishedFile [16] 42",
    ],
)
async def test_explicit_failure_overrides_completion_without_retry(
    tmp_path: Path,
    failure: str,
) -> None:
    executable, ugc = native_updater(tmp_path, [COMPLETE + "\n" + failure])

    with pytest.raises(mods.ModUpdateError, match="failed"):
        await mods.update(executable, ugc)

    assert (ugc / "attempts").read_text() == "1"
    assert (ugc / "partial-download").read_bytes() == b"retained download"


async def test_empty_or_dynamic_setup_needs_no_item_query(tmp_path: Path) -> None:
    executable, ugc = native_updater(
        tmp_path,
        [
            "[S_API FAIL] Tried to access Steam interface "
            "STEAMUGC_INTERFACE_VERSION016 before SteamAPI_Init succeeded.\n"
            "[00:00:01]: [Workshop] ODPF failed to get a lock, retrying\n"
            "[00:00:01]: No mods registered\n" + COMPLETE,
        ],
    )

    await mods.update(executable, ugc)

    assert (ugc / "attempts").read_text() == "1"


@pytest.mark.parametrize(
    ("output", "error"),
    [
        ("[00:00:01]: ModIndex: Load sequence finished successfully.\n", "completion"),
        (
            TIMEOUT + "\n"
            "[00:00:31]: #ERROR: Failure to load dedicated_server_mods_setup.lua:\n"
            + COMPLETE,
            "Failure to load dedicated_server_mods_setup",
        ),
    ],
)
async def test_missing_completion_and_invalid_setup_do_not_retry(
    tmp_path: Path,
    output: str,
    error: str,
) -> None:
    executable, ugc = native_updater(tmp_path, [output])

    with pytest.raises(RuntimeError, match=error):
        await mods.update(executable, ugc)

    assert (ugc / "attempts").read_text() == "1"


async def test_nonzero_exit_is_not_retried(tmp_path: Path) -> None:
    executable, ugc = native_updater(
        tmp_path,
        [TIMEOUT + "\n" + COMPLETE],
        returncode=7,
    )

    with pytest.raises(mods.ModUpdateError, match="status 7"):
        await mods.update(executable, ugc)

    assert (ugc / "attempts").read_text() == "1"


async def test_timeout_terminates_downloader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, ugc = write_updater(
        tmp_path,
        UPDATER + "\nprint('READY', flush=True)\ntime.sleep(60)\n",
    )
    real_timeout = asyncio.timeout
    deadline = real_timeout(None)
    monkeypatch.setattr(
        asyncio,
        "timeout",
        lambda delay: (
            deadline if delay == mods.UPDATE_PROCESS_TIMEOUT else real_timeout(delay)
        ),
    )
    existing_tasks = asyncio.all_tasks()

    def expire_after_retry_is_ready(line: str) -> None:
        if line == "READY":
            deadline.reschedule(asyncio.get_running_loop().time())

    async with real_timeout(5):
        with pytest.raises(mods.ModUpdateError):
            await mods.update(executable, ugc, log_handler=expire_after_retry_is_ready)

    assert deadline.expired()
    assert (ugc / "attempts").read_text() == "1"
    assert await asyncio.to_thread(process_stopped, int((ugc / "pid").read_text()))
    assert asyncio.all_tasks() == existing_tasks
