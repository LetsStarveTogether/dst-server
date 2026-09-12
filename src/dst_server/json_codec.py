import json

import orjson


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> None:
    keys: set[str] = set()
    for key, _ in pairs:
        if key in keys:
            msg = f"duplicate JSON object key: {key!r}"
            raise ValueError(msg)
        keys.add(key)


def validate_json_structure(payload: bytes) -> None:
    try:
        orjson.loads(payload)
    except orjson.JSONDecodeError as error:
        # CLI fields may fall back to plain text on syntax errors, but must
        # reject non-finite JSON constants instead of treating them as strings.
        if error.doc[error.pos :].startswith(("NaN", "Infinity", "-Infinity")):
            msg = "invalid JSON constant"
            raise ValueError(msg) from error
        raise
    # Keep orjson's strict syntax/UTF-8 validation; stdlib handles object keys.
    json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
