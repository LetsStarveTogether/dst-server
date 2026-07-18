from __future__ import annotations

import os
import sys

_PROTOCOL_FDS = (3, 4, 5)


def main() -> None:
    sources = tuple(int(value) for value in sys.argv[1:4])
    command = sys.argv[4:]
    if len(sources) != len(_PROTOCOL_FDS) or not command:
        msg = "usage: _fd_wrapper.py FD3 FD4 FD5 COMMAND [ARG ...]"
        raise SystemExit(msg)
    for source, target in zip(sources, _PROTOCOL_FDS, strict=True):
        os.dup2(source, target, inheritable=True)
    for source in sources:
        if source not in _PROTOCOL_FDS:
            os.close(source)
    os.execv(command[0], command)  # ruff:ignore[start-process-with-no-shell]


if __name__ == "__main__":
    main()
