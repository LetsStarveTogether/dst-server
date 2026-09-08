import json
from functools import cache
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from dst_server.commands import schema_contract
from dst_server.errors import ErrorInfo
from dst_server.models.cluster import GameEventRecord, LifecycleRecord, LogRecord

capnp: Any = import_module("capnp")
RPC_SCHEMA_PATH = Path(__file__).with_name("schema") / "rpc.capnp"


def _payload_contract() -> bytes:
    return json.dumps(
        {
            "wireFormat": 2,
            "commands": schema_contract(),
            "observations": {
                model.__name__: model.model_json_schema()
                for model in (LogRecord, LifecycleRecord, GameEventRecord)
            },
            "error": TypeAdapter(ErrorInfo).json_schema(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@cache
def load_schema() -> Any:
    return capnp.load(str(RPC_SCHEMA_PATH))


def _schema_fingerprint(payload: bytes) -> str:
    return sha256(RPC_SCHEMA_PATH.read_bytes() + b"\0" + payload).hexdigest()


SCHEMA_FINGERPRINT = _schema_fingerprint(_payload_contract())
