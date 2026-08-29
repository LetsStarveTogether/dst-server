from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ulid import ULID


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalidArgument"
    INVALID_STATE = "invalidState"
    NOT_FOUND = "notFound"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    OVERFLOW = "overflow"
    TOPOLOGY_CHANGE_REQUIRED = "topologyChangeRequired"
    INCOMPATIBLE_SCHEMA = "incompatibleSchema"
    INTERNAL = "internal"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: ErrorCode
    error_id: ULID
    message: str
    fields: tuple[str, ...] = ()

    @classmethod
    def from_wire(cls, value: Any) -> ErrorInfo:
        return cls(
            code=ErrorCode(str(value.code)),
            error_id=ULID.from_str(str(value.errorId)),
            message=str(value.message),
            fields=tuple(value.fields),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "errorId": str(self.error_id),
            "message": self.message,
            "fields": list(self.fields),
        }


class RemoteError(RuntimeError):
    def __init__(self, error: ErrorInfo) -> None:
        self.error = error
        super().__init__(f"{error.code}: {error.message} [{error.error_id}]")


class DisconnectedError(ConnectionError):
    pass


class IndeterminateError(RemoteError, ConnectionError):
    def __init__(self, error: ErrorInfo | None = None) -> None:
        super().__init__(
            error
            or ErrorInfo(
                ErrorCode.INDETERMINATE,
                ULID(),
                "operation is indeterminate",
            )
        )


def unwrap_outcome(value: Any) -> Any:
    selected = value.which()
    if selected == "error":
        error = ErrorInfo.from_wire(value.error)
        if error.code is ErrorCode.INDETERMINATE:
            raise IndeterminateError(error)
        raise RemoteError(error)
    if selected != "value":
        msg = f"invalid RPC outcome member: {selected}"
        raise ValueError(msg)
    return value.value


def success(value: object | None = None) -> dict[str, object]:
    return {"value": {} if value is None else value}


def failure(error: ErrorInfo) -> dict[str, object]:
    return {"error": error.to_wire()}
