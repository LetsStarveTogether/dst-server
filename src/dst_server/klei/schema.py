from pydantic import BaseModel, ConfigDict


class KleiModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        strict=True,
    )
