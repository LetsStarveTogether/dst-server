import json

from pydantic import SecretStr

from dst_server.models.base import FrozenModel


class SecretRecord(FrozenModel):
    count: int
    token: SecretStr


def test_secret_serialization_preserves_field_schemas() -> None:
    schema = SecretRecord.model_json_schema(mode="serialization")

    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["token"]["type"] == "string"
    assert schema["properties"]["token"]["writeOnly"] is True
    record = SecretRecord(count=7, token=SecretStr("test-token"))
    assert json.loads(record.model_dump_json()) == {
        "count": 7,
        "token": "**********",
    }
    assert json.loads(record.model_dump_json(context={"secrets": True})) == {
        "count": 7,
        "token": "test-token",
    }
