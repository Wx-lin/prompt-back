"""Character preview route for explicitly isolated loopback preview apps only."""

from fastapi import APIRouter, Depends, Request

from app.config import Settings
from app.routers.character_preview import CharacterPreviewRequest
from app.services import character_preview
from app.utils.deps import get_current_user
from app.utils.prompt_analysis_http import PromptAnalysisRoute, success


def create_local_character_router(settings: Settings) -> APIRouter:
    """Use the regular auth, schema and provider without local rate limiting."""
    router = APIRouter(tags=["character-preview"], route_class=PromptAnalysisRoute)

    @router.post("/api/character-preview")
    async def preview_character(
        body: CharacterPreviewRequest, request: Request, user=Depends(get_current_user)
    ):
        key = settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise character_preview.PreviewError(503, "角色预览暂未配置，请稍后再试")
        image = await character_preview.generate_image(
            body.prompt, key, character_preview.IMAGE_SIZES[body.aspect_ratio]
        )
        return success(request, {"image_base64": image, "mime_type": "image/png"})

    return router
