from functools import cache
from importlib import import_module
from pathlib import Path
from typing import Any

capnp: Any = import_module("capnp")
RPC_SCHEMA_PATH = Path(__file__).with_name("schema") / "rpc.capnp"


@cache
def load_schema() -> Any:
    return capnp.load(str(RPC_SCHEMA_PATH))
