"""Check CLI log filters, records and signal-driven child cleanup."""

# Each command and executable belongs to the disposable test directory.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[0])\n"
        "path.with_suffix('.pid').write_text(str(os.getpid()))\n"
        "path.with_suffix('.args').write_text(json.dumps(sys.argv[1:]))\n" + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def command(*arguments: str) -> list[str]:
    program = (
        sys.argv[1:]
        if len(sys.argv) > 1
        else [sys.executable, "-I", "-m", "dst_server"]
    )
    return [*program, "--json", "logs", *arguments]


def reaped(reader: Path) -> None:
    pid = int(reader.with_suffix(".pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    message = f"log CLI returned before reaping native reader {pid}"
    raise AssertionError(message)


JOURNAL = """
record = {'__CURSOR': 'first', '__REALTIME_TIMESTAMP': '1000001',
          'MESSAGE': '玩家\\nsecond line', '_SYSTEMD_UNIT': 'dst-007.service'}
print(json.dumps(record), flush=True)
if '--follow' in sys.argv:
    print('reader diagnostic', file=sys.stderr, flush=True)
    time.sleep(60)
else:
    record['__CURSOR'] = 'second'
    print(json.dumps(record))
"""


def check_journal(directory: Path) -> None:
    reader = executable(directory, "journal", JOURNAL)
    query = {
        "limit": 1,
        "direction": "forward",
        "since": "2026-10-01",
        "until": "now",
        "namespace": "games",
        "grep": "--literal",
    }
    result = subprocess.run(
        command(
            "journal",
            "--executable",
            str(reader),
            "--unit",
            "dst-007.service",
            "--query",
            "-",
        ),
        input=json.dumps(query),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    assert page["has_more"]
    assert page["next_cursor"] == "first"
    assert page["records"][0]["fields"]["MESSAGE"] == "玩家\nsecond line"
    arguments = json.loads(reader.with_suffix(".args").read_text())
    assert "--unit=dst-007.service" in arguments
    assert "--lines=+2" in arguments
    for key in ("since", "until", "namespace", "grep"):
        assert f"--{key}={query[key]}" in arguments
    reaped(reader)

    for stop in (signal.SIGINT, signal.SIGTERM):
        process = subprocess.Popen(
            command(
                "journal",
                "--executable",
                str(reader),
                "--follow",
                "--query",
                '{"grep":"players"}',
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            assert select.select([process.stdout], [], [], 10)[0]
            record = json.loads(process.stdout.readline())
            assert record["fields"]["__CURSOR"] == "first"
            arguments = json.loads(reader.with_suffix(".args").read_text())
            assert "--follow" in arguments
            assert "--lines=0" in arguments
            assert "--reverse" not in arguments
            process.send_signal(stop)
            _, errors = process.communicate(timeout=10)
            assert process.returncode == 0, errors
            reaped(reader)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def check_netdata(directory: Path) -> None:
    reader = executable(
        directory,
        "otel-plugin",
        """
assert sys.argv[1] == 'logs'
print(json.dumps({'timestamp_ns': 18446744073709551615,
                  'fields': [['tag', 'a'], ['tag', 'b']]}))
print('matched=5 returned=1 window=1..2', file=sys.stderr)
""",
    )
    query = directory / "query.json"
    query.write_text(
        json.dumps({
            "since": 1,
            "until": 2,
            "service_name": "dst",
            "service_namespace": "007",
            "filters": [["tag", "player"], ["body.userid", "KU_example"]],
            "fields": ["tag", "body.userid"],
            "limit": 1,
        }),
        encoding="utf-8",
    )
    result = subprocess.run(
        command(
            "telemetry",
            f"@{query}",
            "--executable",
            str(reader),
            "--stock-config",
            str(directory / "stock.yaml"),
            "--config",
            str(directory / "otel.yaml"),
            "--max-concurrency",
            "2",
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    assert page["matched"] == 5
    assert page["truncated"] is True
    assert page["records"][0]["timestamp_ns"] == 18_446_744_073_709_551_615
    assert page["records"][0]["fields"] == [["tag", "a"], ["tag", "b"]]
    arguments = json.loads(reader.with_suffix(".args").read_text())
    assert "--filter=tag=player,body.userid=KU_example" in arguments
    assert "--fields=tag,body.userid" in arguments
    assert "--name=dst" in arguments
    assert "--namespace=007" in arguments
    reaped(reader)


def check_cancelled_queries(directory: Path) -> None:
    reader = executable(directory, "hanging", "time.sleep(60)\n")
    marker = reader.with_suffix(".pid")
    for options in (("journal",), ("telemetry", '{"since":1}')):
        for stop in (signal.SIGINT, signal.SIGTERM):
            marker.unlink(missing_ok=True)
            process = subprocess.Popen(
                command(*options, "--executable", str(reader)),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                while not marker.exists():
                    assert time.monotonic() < deadline
                    assert process.poll() is None
                    time.sleep(0.01)
                process.send_signal(stop)
                _, errors = process.communicate(timeout=10)
                assert process.returncode == 1, errors
                assert "cancel" in errors.lower(), errors
                reaped(reader)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        check_journal(Path(temporary))
        check_netdata(Path(temporary))
        check_cancelled_queries(Path(temporary))
    sys.stdout.write("Native log CLI checks passed\n")
