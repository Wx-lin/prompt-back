"""Read-only trusted source adapter for the opt-in loopback preview application.

No signing key is copied and no bearer token is cached or stored. Every private
request is authenticated by the already-running real backend before local SQL.
"""

import json
import re
import uuid

import httpx
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.models import Prompt, PromptImage, User
from app.schemas.auth import UserOut
from app.schemas.prompt import PromptDetailOut
from app.services.prompt_analysis import assets, public_sources
from app.services.prompt_analysis.errors import AnalysisError

SOURCE_URL = "http://127.0.0.1:8000"
USER_LIMIT = 32 * 1024
PROMPT_LIMIT = 1024 * 1024


def unavailable():
    return AnalysisError(503, "SERVICE_UNAVAILABLE", "真实后端暂不可用，请稍后重试")


def bearer_token(header):
    if not isinstance(header, str) or not header.startswith("Bearer "):
        raise AnalysisError(401, "UNAUTHENTICATED", "请先登录")
    token = header[7:]
    if not re.fullmatch(r"[A-Za-z0-9._~-]{10,8192}", token):
        raise AnalysisError(401, "UNAUTHENTICATED", "请先登录")
    return token


class PreviewSource:
    """Only two fixed GET paths; neither caller nor response can redirect us."""

    def __init__(self, *, transport=None):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(5), follow_redirects=False, trust_env=False, transport=transport
        )

    async def close(self):
        await self.client.aclose()

    async def _read(self, path, *, token=None, maximum):
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if token:
            headers = {**headers, "Authorization": "Bearer " + token}
        try:
            async with self.client.stream("GET", SOURCE_URL + path, headers=headers) as response:
                if token and response.status_code in {401, 403}:
                    raise AnalysisError(401, "UNAUTHENTICATED", "请重新登录")
                if not token and response.status_code == 404:
                    raise assets.not_found()
                if response.status_code != 200:
                    raise unavailable()
                if not re.fullmatch(
                    r"application/json(?:\s*;.*)?", response.headers.get("content-type", ""), re.I
                ):
                    raise unavailable()
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > maximum):
                    raise unavailable()
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > maximum:
                        raise unavailable()
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict):
                    raise unavailable()
                return value
        except AnalysisError:
            raise
        except Exception:
            raise unavailable() from None

    async def current_user(self, authorization):
        token = bearer_token(authorization)
        value = await self._read("/api/auth/me", token=token, maximum=USER_LIMIT)
        try:
            user = UserOut.model_validate(value)
            if (
                user.created_at.tzinfo is None
                or (user.nickname is not None and len(user.nickname) > 100)
                or (user.avatar_url is not None and len(user.avatar_url) > 500)
            ):
                raise ValueError
            return user
        except (ValueError, TypeError, ValidationError):
            raise unavailable() from None

    async def public_prompt(self, prompt_id):
        if not isinstance(prompt_id, uuid.UUID):
            raise AnalysisError(422, "INVALID_REQUEST", "请求参数不合法")
        value = await self._read(f"/api/prompts/{prompt_id}", maximum=PROMPT_LIMIT)
        try:
            prompt = PromptDetailOut.model_validate(value)
            if (
                prompt.id != prompt_id
                or not 1 <= len(prompt.title) <= 200
                or not prompt.prompt_text.strip()
                or len(prompt.category) > 50
                or not 1 <= len(prompt.images) <= 64
                or len(prompt.models) > 100
                or any(len(model) > 50 for model in prompt.models)
                or prompt.created_at.tzinfo is None
                or (prompt.author_name is not None and len(prompt.author_name) > 100)
                or (prompt.author_avatar_url is not None and len(prompt.author_avatar_url) > 1000)
                or (prompt.source_url is not None and len(prompt.source_url) > 500)
            ):
                raise ValueError
            image = min(prompt.images, key=lambda image: (image.sort_order, image.id))
            if len(image.image_url) > 500 or (image.thumbnail_url and len(image.thumbnail_url) > 500):
                raise ValueError
        except (ValueError, TypeError, ValidationError):
            raise unavailable() from None
        # Preserve the production COS bucket/prefix checks before any local row is written.
        public_sources.object_key(image.image_url)
        return prompt, image


async def mirror_user(db, user):
    # Email/Google subject and the bearer token are not needed in the local tool DB.
    values = {
        "id": user.id,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at,
        "is_active": True,
    }
    statement = (
        insert(User)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[User.id],
            set_={"nickname": user.nickname, "avatar_url": user.avatar_url, "is_active": True},
        )
        .returning(User)
    )
    result = (await db.execute(statement)).scalar_one()
    await db.commit()
    return result


async def mirror_public_prompt(db, prompt, image):
    values = {
        key: getattr(prompt, key)
        for key in (
            "id",
            "title",
            "prompt_text",
            "description",
            "category",
            "models",
            "author_name",
            "author_avatar_url",
            "source_url",
            "like_count",
            "view_count",
            "collect_count",
            "created_at",
        )
    }
    # A successful public detail read establishes publication. No invented prompt is seeded.
    values = {**values, "is_published": True, "review_status": None}
    await db.execute(
        insert(Prompt)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Prompt.id],
            set_=values,
        )
    )
    await db.execute(delete(PromptImage).where(PromptImage.prompt_id == prompt.id))
    await db.execute(
        insert(PromptImage).values(
            id=image.id,
            prompt_id=prompt.id,
            image_url=image.image_url,
            thumbnail_url=image.thumbnail_url,
            sort_order=image.sort_order,
        )
    )
    await db.commit()
