import asyncio
from pathlib import Path

import pytest

from dst_server.cluster import mods

from .helpers import process_stopped
from .test_mods_cleanup import write_updater

COMPLETE = (
    "[00:00:31]: FinishDownloadingServerMods Complete! Process trying to quit nicely.."
)
TIMEOUT = "[00:00:31]: DownloadServerMods timed out with no response from Workshop..."
UPDATER = r"""#!/usr/bin/env python3
import os
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


def native_updater(
    tmp_path: Path,
    outputs: list[str],
    *,
    delay: float = 0,
    returncode: int = 0,
) -> tuple[Path, Path]:
    source = (
        UPDATER
        + f"\noutputs = {outputs!r}\n"
        + "print(outputs[min(attempt, len(outputs)) - 1], end='', flush=True)\n"
        + f"time.sleep({delay!r})\nsys.exit({returncode!r})\n"
    )
    return write_updater(tmp_path, source)


@pytest.mark.parametrize("with_handler", [False, True])
async def test_timeout_retries_preserve_cache_until_fifth_attempt(
    tmp_path: Path,
    with_handler: bool,
) -> None:
    executable, ugc = native_updater(
        tmp_path,
        [TIMEOUT + "\n" + COMPLETE] * 4 + [COMPLETE],
    )
    lines: list[str] = []

    await mods.update(
        executable,
        ugc,
        log_handler=lines.append if with_handler else None,
    )

    assert (ugc / "attempts").read_text() == "5"
    assert (ugc / "partial-download").read_bytes() == b"retained download"
    if with_handler:
        assert sum("retrying" in line for line in lines) == 4


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
async def test_explicit_failure_overrides_completion_and_exhausts_attempts(
    tmp_path: Path,
    failure: str,
) -> None:
    executable, ugc = native_updater(tmp_path, [COMPLETE + "\n" + failure])

    with pytest.raises(RuntimeError, match=r"failed after 2 attempt\(s\)"):
        await mods.update(executable, ugc, attempts=2)

    assert (ugc / "attempts").read_text() == "2"
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

    with pytest.raises(ChildProcessError, match="status 7"):
        await mods.update(executable, ugc)

    assert (ugc / "attempts").read_text() == "1"


async def test_attempts_share_the_outer_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, ugc = native_updater(
        tmp_path,
        [TIMEOUT + "\n" + COMPLETE, COMPLETE],
        delay=1.2,
    )
    monkeypatch.setattr(mods, "UPDATE_PROCESS_TIMEOUT", 2)
    existing_tasks = asyncio.all_tasks()

    with pytest.raises(TimeoutError):
        await mods.update(executable, ugc, attempts=2)

    assert (ugc / "attempts").read_text() == "2"
    assert await asyncio.to_thread(process_stopped, int((ugc / "pid").read_text()))
    assert asyncio.all_tasks() == existing_tasks


@pytest.mark.parametrize("attempts", [0, -1, True, 1.5])
async def test_invalid_attempt_count_is_rejected(
    tmp_path: Path,
    attempts: int,
) -> None:
    executable, ugc = native_updater(tmp_path, [COMPLETE])

    with pytest.raises(ValueError, match="attempts must be a positive integer"):
        await mods.update(executable, ugc, attempts=attempts)

    assert not (ugc / "attempts").exists()
