"""Run the same CLI shipped by the native binary."""

import signal
import sys

from ._native import run_cli


def main() -> int:
    # Rust handles interruption and waits for native resource cleanup.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
