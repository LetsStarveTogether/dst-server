import json
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
from pathlib import Path

import pytest

MISSING = "ERROR! Failed to install app '343050' (Missing configuration)"
OTHER = "ERROR! Failed to install app '343050' (No subscription)"
SUCCESS = "Success! App '343050' fully installed."


@pytest.mark.parametrize(
    ("outcomes", "returncode", "delays"),
    [
        pytest.param(((0, SUCCESS),), 0, (), id="first-success"),
        pytest.param(((8, MISSING), (0, SUCCESS)), 0, (10,), id="retry-success"),
        pytest.param(((8, MISSING),) * 10, 8, (10, *([30] * 8)), id="retry-limit"),
        pytest.param(((8, OTHER),), 8, (), id="other-error"),
        pytest.param(((143, MISSING),), 143, (), id="signal-status"),
        pytest.param(((8, MISSING), (23, OTHER)), 23, (10,), id="fresh-attempt-log"),
    ],
)
def test_steamcmd_install_retry(
    tmp_path: Path,
    outcomes: tuple[tuple[int, str], ...],
    returncode: int,
    delays: tuple[int, ...],
) -> None:
    containerfile = Path(__file__).parents[1] / "Containerfile"
    block = containerfile.read_text().split("# Install the DST server.\n", 1)[1]
    block = block.split("\n\n", 1)[0]
    script = block.split("RUN ", 1)[1].replace("\\\n", "")
    script = script.replace(
        "/tmp/steamcmd-install.log",  # ruff:ignore[hardcoded-temp-file]
        str(tmp_path / "install.log"),
    )
    executable = (
        f"#!{sys.executable}\n"
        r"""
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
calls = Path(os.environ["CALLS"])
previous = [json.loads(line) for line in calls.read_text().splitlines()]
with calls.open("a") as stream:
    stream.write(json.dumps([name, *sys.argv[1:]]) + "\n")
if name != "steamcmd.sh":
    sys.exit(0)
attempt = sum(call[0] == name for call in previous)
outcomes = json.loads(os.environ["OUTCOMES"])
status, output = outcomes[min(attempt, len(outcomes) - 1)]
print(output, flush=True)
sys.exit(status)
"""
    )
    for name in ("chmod", "chown", "sleep", "steamcmd.sh"):
        path = tmp_path / name
        path.write_text(executable)
        path.chmod(0o755)
    calls_path = tmp_path / "calls.jsonl"
    calls_path.touch()
    diagnostics = tmp_path / "Steam" / "logs"
    diagnostics.mkdir(parents=True)
    for name in ("content_log.txt", "connection_log.txt"):
        (diagnostics / name).write_text(f"DIAGNOSTIC|{name}\n")

    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "STEAMCMDDIR": str(tmp_path),
            "HOMEDIR": str(tmp_path),
            "BETA": "1",
            "CALLS": str(calls_path),
            "OUTCOMES": json.dumps(outcomes),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == returncode, output
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    steam_calls = [call for call in calls if call[0] == "steamcmd.sh"]
    assert len(steam_calls) == len(outcomes), output
    assert tuple(int(call[1]) for call in calls if call[0] == "sleep") == delays
    for call in steam_calls:
        assert call[1:] == [
            "+@ShutdownOnFailedCommand",
            "1",
            "+@NoPromptForPassword",
            "1",
            "+force_install_dir",
            "/install",
            "+login",
            "anonymous",
            "+app_update",
            "343050",
            "-beta",
            "updatebeta",
            "validate",
            "+quit",
        ]
    for name in ("content_log.txt", "connection_log.txt"):
        assert output.count(f"DIAGNOSTIC|{name}") == sum(
            status != 0 for status, _ in outcomes
        )
    if returncode == 0:
        assert not (tmp_path / "install.log").exists()
