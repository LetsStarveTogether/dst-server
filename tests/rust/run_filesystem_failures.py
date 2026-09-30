"""Run disposable EFBIG/ENOSPC checks and save their bounded JSON evidence.

Build the executable with cargo test -p dst-server --test filesystem_failures
--no-run, then pass the reported executable path as the positional argument.
The container loads the matching host glibc through a read-only library mount.
"""

# The caller selects the test binary and local test image; no shell is involved.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(command: list[str], env: dict[str, str] | None = None) -> list[dict]:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return [
        json.loads(line.removeprefix("FILESYSTEM_FAILURE_REPORT "))
        for line in result.stdout.splitlines()
        if line.startswith("FILESYSTEM_FAILURE_REPORT ")
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--libraries", type=Path, default=Path("/usr/lib"))
    parser.add_argument("--loader", default="ld-linux-x86-64.so.2")
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    binary = arguments.binary.resolve(strict=True)
    libraries = arguments.libraries.resolve(strict=True)
    (libraries / arguments.loader).resolve(strict=True)
    cases = run(
        [str(binary), "--exact", "file_size_worker", "--ignored", "--nocapture"],
        os.environ | {"DST_FILE_SIZE_WORKER": "1"},
    )
    cases += run([
        "podman",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/failure:rw,size=65536,mode=0700",
        "--env",
        "DST_TEST_TMPFS=/failure",
        "--volume",
        f"{binary}:/filesystem_failures:ro",
        "--volume",
        f"{libraries}:/native-libs:ro",
        "--entrypoint",
        f"/native-libs/{arguments.loader}",
        arguments.image,
        "--library-path",
        "/native-libs",
        "/filesystem_failures",
        "--exact",
        "tmpfs_enospc_worker",
        "--ignored",
        "--nocapture",
    ])
    assert {case["errno"] for case in cases} == {27, 28}
    report = json.dumps({"cases": cases, "isolated_container": True}, indent=2) + "\n"
    arguments.report.write_text(report)
    sys.stdout.write(report)


if __name__ == "__main__":
    main()
