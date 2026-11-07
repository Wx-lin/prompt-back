"""Bounded text/image inputs; context and ownership always come from the server."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.prompt_analysis import BilingualPrompt, PromptBreakdown


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: (
        Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
        | None
    ) = None
    asset_id: UUID | None = None
    generated_image_id: UUID | None = None
    mode: Literal["auto", "generate", "image", "smart"] = "auto"

    @model_validator(mode="after")
    def require_input(self):
        if self.message is None and self.asset_id is None:
            raise ValueError("An idea or reference image is required")
        if self.generated_image_id and (self.mode not in {"image", "smart"} or self.asset_id is not None):
            raise ValueError("Generated references require image mode and cannot be combined with an upload")
        return self


class ConversationImageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["image-1"]
    title: Annotated[str, Field(min_length=1, max_length=80)]
    asset_id: UUID


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


class ConversationImageIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["image-intent-1"]
    use_reference: bool
    use_context: bool


class ConversationImageQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["image-question-1"]


class ConversationDialogue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    user: str
    assistant: Annotated[str, Field(max_length=4000)]


class ConversationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    title: Annotated[str, Field(min_length=1, max_length=80)]
    prompt: BilingualPrompt | None = None
    breakdown: PromptBreakdown | None = None
    source_type: Literal["image", "text"] | None = None
    recent_messages: Annotated[list[str], Field(max_length=6)]
    recent_dialogue: Annotated[list[ConversationDialogue], Field(max_length=6)] = Field(default_factory=list)
    has_generated_image: bool = False


class AgentConversationDialogue(ConversationDialogue):
    """Agent history bounds replies independently of stored conversation data."""

    assistant: Annotated[str, Field(max_length=2000)]


class AgentConversationContext(ConversationContext):
    """Recent agent history: at most 12 turns and 12,000 dialogue characters."""

    recent_messages: Annotated[list[str], Field(max_length=12)]
    recent_dialogue: Annotated[list[AgentConversationDialogue], Field(max_length=12)] = Field(
        default_factory=list
    )
    has_reference_image: bool = False

    @model_validator(mode="after")
    def bound_dialogue(self):
        if sum(len(turn.user) + len(turn.assistant) for turn in self.recent_dialogue) > 12000:
            raise ValueError("Agent dialogue exceeds character budget")
        return self
