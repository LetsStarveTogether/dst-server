"""Run Fieldkit's Lua contracts and syntax checks under Lua 5.1 and LuaJIT."""

# Build/test commands pass trusted repository paths as separate arguments.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true, print]
import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    specs = sorted((root / "tests/client_mod").glob("*_spec.lua"))
    files = sorted(
        path
        for folder in (root / "mods/fieldkit", root / "tests/client_mod")
        for path in folder.rglob("*.lua")
    )
    assert specs
    assert files
    for runtime in ("lua5.1", "luajit"):
        executable = shutil.which(runtime)
        if executable is None:
            message = f"{runtime} is required"
            raise RuntimeError(message)
        for script in specs:
            result = subprocess.run(
                [executable, str(script)],
                cwd=root,
                env=os.environ
                | {"FIELDKIT_GAME_SCRIPTS": str(root / "dst-scripts/scripts")},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, (
                f"{runtime} {script.name}:\n{result.stdout}{result.stderr}"
            )
        subprocess.run(
            [executable, "-", *map(str, files)],
            cwd=root,
            input="for i=1,#arg do assert(loadfile(arg[i])) end\n",
            text=True,
            timeout=30,
            check=True,
        )
        print(
            f"{runtime}: {len(specs)} Fieldkit contracts and "
            f"{len(files)} syntax checks passed"
        )


if __name__ == "__main__":
    main()
