"""Check deferred SDK imports, public exports and runtime type annotations."""

# The child checks a process-wide import policy in an isolated interpreter.
# ruff: file-ignore[suspicious-subprocess-import]
import sys
lazy from importlib import import_module
lazy from subprocess import run
lazy from typing import get_type_hints

import dst_server


def main() -> None:
    assert {name for name in sys.modules if name.startswith("dst_server")} == {
        "dst_server"
    }
    assert dst_server.Client.__name__ == "Client"
    assert "dst_server._native" not in sys.modules

    host = dst_server.Host
    assert "dst_server.logs" not in sys.modules
    assert "dst_server.settings" not in sys.modules
    assert get_type_hints(host.connect)["return"].__args__[0] is dst_server.Client

    native = import_module("dst_server._native")
    assert dst_server.DstError is native.DstError
    assert dst_server.describe is native.describe
    assert dst_server.JournalCursorError is native.JournalCursorError
    for name in dst_server.__all__:
        assert getattr(dst_server, name) is not None
    for name in (
        "archive",
        "events",
        "external",
        "host",
        "logs",
        "management",
        "settings",
    ):
        import_module(f"dst_server.{name}")
    run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; sys.set_lazy_imports_filter(lambda *_: False); "
                "import dst_server; from typing import get_type_hints; "
                "assert get_type_hints(dst_server.Host.connect)['return'].__args__[0] "
                "is dst_server.Client"
            ),
        ],
        check=True,
        timeout=10,
    )


if __name__ == "__main__":
    main()
