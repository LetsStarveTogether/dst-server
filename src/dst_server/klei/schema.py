from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class KleiModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        strict=True,
    )


__all__ = ["KleiModel"]
