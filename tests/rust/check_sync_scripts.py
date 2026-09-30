"""Exercise the workflow's synchronization step against disposable local Git repos."""

# All subprocess inputs are this checked-in workflow or disposable fixture paths.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import os
import subprocess
import sys
import tempfile
import textwrap
from itertools import takewhile
from pathlib import Path

ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "file",
    "GIT_TERMINAL_PROMPT": "0",
}
SUBMODULE = "dst-scripts/scripts"


def run(directory: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=directory,
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def git(directory: Path, *arguments: str) -> str:
    command = ["git", "-c", "user.name=Fixture", "-c", "user.email=ci@example.invalid"]
    result = run(directory, *command, *arguments)
    result.check_returncode()
    return result.stdout.strip()


def commit(directory: Path, name: str, content: str) -> str:
    (directory / name).write_text(content)
    git(directory, "add", name)
    git(directory, "commit", "-m", "chore: fixture")
    return git(directory, "rev-parse", "HEAD")


def main() -> None:
    job = (
        Path(__file__).resolve().parents[2] / ".github/workflows/image.yml"
    ).read_text()
    job = job.split("\n  sync-dst-scripts:\n", 1)[1]
    assert "      - name: Update DST scripts reference\n" in job
    block = job.split("      - name: Update DST scripts reference\n", 1)[1].split(
        "        run: |\n", 1
    )[1]
    block = "".join(
        takewhile(
            lambda line: not line.strip() or line.startswith("          "),
            block.splitlines(keepends=True),
        )
    )
    assert block.strip()
    script = textwrap.dedent(block)

    with tempfile.TemporaryDirectory(prefix="dst-sync-scripts-") as temporary:
        root = Path(temporary)
        output = root / "github-output"
        ENV["GITHUB_OUTPUT"] = str(output)
        source, parent, remote, runner, rival = (
            root / name
            for name in ["scripts", "parent", "remote.git", "runner", "rival"]
        )
        git(root, "init", "-b", "main", str(source))
        commit(source, "native.lua", "return 1\n")
        git(root, "init", "-b", "main", str(parent))
        git(parent, "submodule", "add", "-b", "main", str(source), SUBMODULE)
        git(parent, "commit", "-m", "chore: fixture")
        git(root, "clone", "--bare", str(parent), str(remote))
        git(root, "clone", str(remote), str(runner))
        git(runner, "submodule", "update", "--init", SUBMODULE)
        target = commit(source, "native.lua", "return 2\n")
        output.write_text("")
        result = run(runner, "bash", "-c", script)
        assert result.returncode == 0, result.stderr
        changed = git(runner, "rev-parse", "HEAD")
        assert output.read_text() == f"revision={changed}\n"
        assert git(remote, "rev-parse", "main") == changed
        assert git(runner, "show", "--format=", "--name-only", "HEAD") == SUBMODULE
        assert (
            git(remote, "ls-tree", "main", SUBMODULE)
            == f"160000 commit {target}\t{SUBMODULE}"
        )

        output.write_text("")
        result = run(runner, "bash", "-c", script)
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"revision={changed}\n"
        assert git(runner, "rev-parse", "HEAD") == changed
        assert git(remote, "rev-parse", "main") == changed

        commit(source, "native.lua", "return 3\n")
        git(root, "clone", str(remote), str(rival))
        concurrent = commit(rival, "human.txt", "keep this concurrent change\n")
        git(rival, "push", "origin", "HEAD:main")
        output.write_text("")
        result = run(runner, "bash", "-c", script)
        assert result.returncode != 0, result.stderr
        assert "rejected" in result.stderr, result.stderr
        assert not output.read_text()
        assert git(runner, "rev-parse", "HEAD^") == changed
        assert git(remote, "rev-parse", "main") == concurrent
    sys.stdout.write(
        "Submodule sync: changed, no change, and rejected concurrent push passed.\n"
    )


if __name__ == "__main__":
    main()
