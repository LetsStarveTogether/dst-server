import json

import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from dst_server.json_codec import validate_json_structure

JSON_TEXT = st.text(alphabet=st.characters(codec="utf-8"), max_size=30)
JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**63), max_value=2**64 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | JSON_TEXT,
    lambda values: (
        st.lists(values, max_size=5) | st.dictionaries(JSON_TEXT, values, max_size=5)
    ),
    max_leaves=20,
)


@given(JSON_VALUES)
def test_nested_json_values_keep_distinct_object_keys(value: JsonValue) -> None:
    payload = json.dumps(value, ensure_ascii=False).encode()
    validate_json_structure(payload)


@given(key=JSON_TEXT, depth=st.integers(min_value=0, max_value=4))
def test_repeated_keys_cannot_hide_behind_escaping_or_arrays(
    key: str, depth: int
) -> None:
    escaped = json.dumps(key, ensure_ascii=True)
    literal = json.dumps(key, ensure_ascii=False)
    obj = "{" + escaped + ":0," + literal + ":1}"
    payload = ("[" * depth + obj + "]" * depth).encode()
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        validate_json_structure(payload)


@pytest.mark.parametrize(
    "payload", [b"plain text", b'{"key":"\xff"}', rb'{"key":"\ud800"}']
)
def test_syntax_and_utf8_failures_keep_orjson_error_type(payload: bytes) -> None:
    with pytest.raises(orjson.JSONDecodeError):
        validate_json_structure(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"key":1,"key":2}',
        rb'{"key":1,"\u006bey":2}',
        rb'{"nested":[{"a\\\"}":1,"a\\\"}":2}]}',
        b'{"":0,"":1}',
    ],
)
def test_json_structure_rejects_duplicate_keys(payload: bytes) -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        validate_json_structure(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'[{"key":1},{"key":2}]',
        b'{"key":{"key":1},"other":{"key":2}}',
        rb'{"text":"{\"key\":1,\"key\":2}","literal":"NaN"}',
        rb'{"\u006bey":1,"a\\\"}":2}',
        b"[0,true,false,null,18446744073709551616]",
    ],
)
def test_json_structure_accepts_distinct_keys_and_string_contents(
    payload: bytes,
) -> None:
    validate_json_structure(payload)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
@pytest.mark.parametrize("nested", [False, True])
def test_json_structure_rejects_nonfinite_constants(
    constant: bytes, nested: bool
) -> None:
    payload = b'{"value":' + constant + b"}" if nested else constant
    with pytest.raises(ValueError, match="invalid JSON constant"):
        validate_json_structure(payload)
