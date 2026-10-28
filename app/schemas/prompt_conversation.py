"""Bounded text/image inputs; context and ownership always come from the server."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.prompt_analysis import BilingualPrompt, PromptBreakdown


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: (
        Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
        | None
    ) = None
    asset_id: UUID | None = None
    mode: Literal["auto", "generate"] = "auto"

    @model_validator(mode="after")
    def require_input(self):
        if self.message is None and self.asset_id is None:
            raise ValueError("An idea or reference image is required")
        return self


class ConversationMessage(ConversationInput):
    parent_analysis_id: UUID
    parent_revision: Annotated[int, Field(strict=True, ge=0)]


class PromptAssetImport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: UUID


class RetryConversation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_id: UUID


class ConversationReplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["reply-1"]
    title: Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")]
    reply: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]


class ConversationDialogue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    user: Annotated[str, Field(max_length=500)]
    assistant: Annotated[str, Field(max_length=4000)]


class ConversationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    title: Annotated[str, Field(min_length=1, max_length=80)]
    prompt: BilingualPrompt | None = None
    breakdown: PromptBreakdown | None = None
    source_type: Literal["image", "text"] | None = None
    recent_messages: Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=6)]
    recent_dialogue: Annotated[list[ConversationDialogue], Field(max_length=6)] = Field(default_factory=list)
