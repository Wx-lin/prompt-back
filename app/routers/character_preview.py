"""Rate-limited endpoint for on-demand character image previews."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.redis import get_redis_client
from app.services import character_preview
from app.utils.deps import get_current_user
from app.utils.prompt_analysis_http import PromptAnalysisRoute, success

router = APIRouter(tags=["character-preview"], route_class=PromptAnalysisRoute)


class CharacterPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=12000)
    aspect_ratio: Literal["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"] = "1:1"

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("提示词不能为空")
        return value


@router.post("/api/character-preview")
async def preview_character(
    body: CharacterPreviewRequest,
    request: Request,
    user=Depends(get_current_user),
    redis=Depends(get_redis_client),
):
    image = await character_preview.create_preview(body.prompt, str(user.id), redis, body.aspect_ratio)
    return success(request, {"image_base64": image, "mime_type": "image/png"})
