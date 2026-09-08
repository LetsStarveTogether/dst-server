import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Annotated

from pydantic import Field, TypeAdapter

DEFAULT_COMMAND_TIMEOUT = 120.0
DEFAULT_SAVE_TIMEOUT = 300.0
DEFAULT_RELOAD_TIMEOUT = 900.0
DEFAULT_STARTUP_TIMEOUT = 900.0
DEFAULT_LIFECYCLE_TIMEOUT = 10800.0
DEFAULT_STOP_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 60.0
RPC_TIMEOUT_MARGIN = 30.0
OUTPUT_DRAIN_TIMEOUT = 30.0

type Timeout = Annotated[float, Field(gt=0, allow_inf_nan=False)]
_TIMEOUT = TypeAdapter(Timeout)


def positive_timeout(value: float) -> float:
    return _TIMEOUT.validate_python(value, strict=True)


operation_deadline = ContextVar[float | None](
    "dst_server_operation_deadline", default=None
)


@asynccontextmanager
async def timeout_scope(duration: float) -> AsyncIterator[float]:
    deadline = operation_deadline.get()
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + duration
    token = operation_deadline.set(deadline)
    try:
        async with asyncio.timeout_at(deadline):
            yield deadline
    finally:
        operation_deadline.reset(token)
