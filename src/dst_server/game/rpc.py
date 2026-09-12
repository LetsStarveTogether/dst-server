from functools import cache
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

RPC_PREFIX = b"DST_RPC|"
MAX_RESULT_LINE_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Success[DataT](Envelope):
    ok: Literal[True]
    data: DataT


class Failure(Envelope):
    ok: Literal[False]
    error: Literal[
        "invalid_request",
        "not_ready",
        "stale_generation",
        "lua_error",
        "invalid_json_value",
        "invalid_utf8",
        "response_too_large",
        "indeterminate",
    ]


type ResponseAdapter[DataT] = TypeAdapter[Success[DataT] | Failure]


@cache
def response_adapter(result_type: Any) -> ResponseAdapter[Any]:
    return TypeAdapter(Success[result_type] | Failure)


class SavedSnapshot(Envelope):
    snapshot: Annotated[str, Field(min_length=1, max_length=4096)]


SAVE_RESPONSE = response_adapter(SavedSnapshot)


class LuaRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"DST Lua request failed: {code}")


class ResponseHeader(Envelope):
    v: Literal[1]
    nonce: Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]
    id: Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")] | None
    generation: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]


class Accepted(ResponseHeader):
    accepted: Literal[True]


class Result(ResponseHeader):
    result: Success[JsonValue] | Failure


RPC_RESPONSE = TypeAdapter(Accepted | Result)
