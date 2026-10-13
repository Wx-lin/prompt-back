"""Administrator-owned homepage selection contract."""

import uuid

from pydantic import BaseModel, Field, field_validator

from app.schemas.prompt import PromptListItem


class FeaturedUpdate(BaseModel):
    prompt_ids: list[uuid.UUID] = Field(max_length=50)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("prompt_ids")
    @classmethod
    def unique_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("作品不能重复")
        return value


class FeaturedResponse(BaseModel):
    items: list[PromptListItem]
    revision: str
