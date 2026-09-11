from typing import Literal

from dst_server.models.base import FrozenModel


class ConsoleValue(FrozenModel):
    type: str
    text: str


class ConsoleError(FrozenModel):
    kind: Literal["compile", "runtime"]
    message: str


class ConsoleResult(FrozenModel):
    output: str
    values: tuple[ConsoleValue, ...]
    error: ConsoleError | None = None
    truncated: bool = False
