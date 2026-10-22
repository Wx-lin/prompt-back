"""Validated private image-to-prompt inputs and versioned model output."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BilingualPrompt(_StrictResult):
    zh: Annotated[str, Field(min_length=1, max_length=6000, pattern=r"\S")]
    en: Annotated[str, Field(min_length=1, max_length=12000, pattern=r"\S")]


class BilingualBreakdown(_StrictResult):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"properties": {"zh": {"const": ""}, "en": {"const": ""}}},
                {"properties": {"zh": {"pattern": r"\S"}, "en": {"pattern": r"\S"}}},
            ],
        }
    )

    zh: Annotated[str, Field(max_length=600)]
    en: Annotated[str, Field(max_length=1200)]

    @model_validator(mode="after")
    def require_both_observed_or_both_empty(self) -> "BilingualBreakdown":
        if self.zh == self.en == "":
            return self
        if not self.zh.strip() or not self.en.strip():
            raise ValueError("Breakdown must contain both languages or two empty strings")
        return self


class PromptBreakdown(_StrictResult):
    subject: BilingualBreakdown
    composition: BilingualBreakdown
    lighting: BilingualBreakdown
    style: BilingualBreakdown


class ModelResult(_StrictResult):
    schema_version: Literal["1"]
    title: Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")]
    prompt: BilingualPrompt
    breakdown: PromptBreakdown
    uncertain_details: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=240, pattern=r"\S")]],
        Field(max_length=8),
    ]


class CreateAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: UUID


class EditAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: Annotated[int, Field(strict=True, ge=0)]
    prompt: BilingualPrompt
