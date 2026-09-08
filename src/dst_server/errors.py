from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError
from ulid import ULID

from dst_server.models.base import ULIDValue


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
    error_id: ULIDValue
    message: str
    fields: tuple[str, ...] = ()


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


class IndeterminateCommandError(RuntimeError):
    pass


class ControllerOperationError(RuntimeError):
    def __init__(self, error_id: ULID | None = None) -> None:
        self.error_id = error_id or ULID()
        super().__init__("cluster operation failed")


class IncompleteRosterError(RuntimeError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(f"missing shard agents: {', '.join(missing)}")


class PlayerLocationConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("player is active on more than one shard")


class SubscriptionOverflowError(OverflowError):
    pass


class ConfigurationStoreError(RuntimeError):
    pass


class GamesRunningError(ConfigurationStoreError):
    def __init__(self) -> None:
        super().__init__("all game processes must be stopped")


class RevisionConflictError(ConfigurationStoreError):
    def __init__(self, revision: ULID) -> None:
        self.revision = revision
        super().__init__("configuration revision conflict")


class InvalidConfigurationError(ConfigurationStoreError):
    def __init__(self, revision: ULID, paths: tuple[tuple[str, ...], ...]) -> None:
        self.revision = revision
        self.paths = paths
        super().__init__("configuration is invalid")


class TopologyChangeError(ConfigurationStoreError):
    def __init__(self, paths: tuple[tuple[str, ...], ...]) -> None:
        self.paths = paths
        super().__init__("configuration changes deployment topology")


class ConfigurationWriteError(ConfigurationStoreError):
    def __init__(self, revision: ULID, paths: tuple[tuple[str, ...], ...]) -> None:
        self.revision = revision
        self.paths = paths
        super().__init__("configuration write failed")


def indeterminate_cause(error: BaseException) -> bool:
    if isinstance(error, TimeoutError | IndeterminateCommandError):
        return True
    if isinstance(error, RemoteError):
        return error.error.code in {ErrorCode.TIMEOUT, ErrorCode.INDETERMINATE}
    return isinstance(error, BaseExceptionGroup) and any(
        indeterminate_cause(nested) for nested in error.exceptions
    )


def indeterminate_info(error: ErrorInfo) -> ErrorInfo:
    return ErrorInfo(
        ErrorCode.INDETERMINATE,
        error.error_id,
        "operation is indeterminate",
        error.fields,
    )


def error_info(error: BaseException) -> ErrorInfo:  # ruff: ignore[complex-structure, too-many-branches]
    if isinstance(error, RemoteError):
        return error.error
    error_id = getattr(error, "error_id", None) or ULID()
    if isinstance(error, BaseExceptionGroup) and indeterminate_cause(error):
        return ErrorInfo(
            ErrorCode.INDETERMINATE, error_id, "operation is indeterminate"
        )
    fields = (
        tuple(
            ".".join(map(str, item["loc"]))
            for item in error.errors(include_input=False, include_url=False)
        )
        if isinstance(error, ValidationError)
        else tuple(".".join(path) for path in getattr(error, "paths", ()))
    )
    match error:
        case ValidationError() | ValueError() | TypeError():
            code, message = ErrorCode.INVALID_ARGUMENT, "invalid argument"
        case KeyError():
            code, message = ErrorCode.NOT_FOUND, "resource not found"
        case RevisionConflictError():
            code, message = ErrorCode.CONFLICT, "configuration changed"
        case GamesRunningError():
            code, message = ErrorCode.INVALID_STATE, "game processes are running"
        case InvalidConfigurationError():
            code, message = ErrorCode.INVALID_ARGUMENT, "configuration is invalid"
        case TopologyChangeError():
            code, message = (
                ErrorCode.TOPOLOGY_CHANGE_REQUIRED,
                "configuration changes deployment topology",
            )
        case TimeoutError():
            code, message = ErrorCode.TIMEOUT, "operation timed out"
        case SubscriptionOverflowError():
            code, message = ErrorCode.OVERFLOW, "subscription overflowed"
        case PlayerLocationConflictError():
            code, message = ErrorCode.CONFLICT, "player location conflicts"
        case IncompleteRosterError():
            code, message = (
                ErrorCode.INVALID_STATE,
                "cluster shard roster is incomplete",
            )
        case IndeterminateCommandError():
            code, message = ErrorCode.INDETERMINATE, "operation is indeterminate"
        case DisconnectedError():
            code, message = ErrorCode.UNAVAILABLE, "shard agent is unavailable"
        case RuntimeError() if not isinstance(
            error, ControllerOperationError | ConfigurationWriteError
        ):
            code, message = ErrorCode.INVALID_STATE, "operation is invalid now"
        case _:
            code, message = ErrorCode.INTERNAL, "internal operation error"
    return ErrorInfo(code, error_id, message, fields)
