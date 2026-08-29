from functools import cache
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any

from .codec import _payload_contract

capnp: Any = import_module("capnp")

SCHEMA_DIRECTORY = Path(__file__).with_name("schema")
RPC_SCHEMA_PATH = SCHEMA_DIRECTORY / "rpc.capnp"


@cache
def load_schema() -> Any:
    return capnp.load(str(RPC_SCHEMA_PATH))


def _schema_fingerprint(payload: bytes) -> str:
    return sha256(RPC_SCHEMA_PATH.read_bytes() + b"\0" + payload).hexdigest()


SCHEMA_FINGERPRINT = _schema_fingerprint(_payload_contract())
