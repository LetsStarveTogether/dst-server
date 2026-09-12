from pathlib import Path

FAKE_UPDATER = r"""#!/usr/bin/env python3
import os
import signal
import subprocess
import sys

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()",
])
print(f"READY|{os.getpid()}|{os.getpgrp()}|{child.pid}", flush=True)
signal.pause()
"""


def write_updater(
    tmp_path: Path,
    source: str = FAKE_UPDATER,
) -> tuple[Path, Path]:
    executable = tmp_path / "fake-updater"
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o755)
    ugc = tmp_path / "ugc"
    ugc.mkdir()
    return executable, ugc
