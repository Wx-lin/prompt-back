"""Authenticated, bounded natural-language character interpretation."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.redis import get_redis_client
from app.services import character_interpret
from app.utils.deps import get_current_user
from app.utils.prompt_analysis_http import PromptAnalysisRoute, success

router = APIRouter(tags=["character-interpret"], route_class=PromptAnalysisRoute)


class InterpretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=2000)
    current_details: str = Field(default="", max_length=2000)

    @field_validator("description")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("描述不能为空")
        return value


@router.post("/api/character-interpret")
async def interpret_character(
    body: InterpretRequest, request: Request, user=Depends(get_current_user), redis=Depends(get_redis_client)
):
    result = await character_interpret.interpret(body.description, str(user.id), redis, body.current_details)
    return success(request, result)
