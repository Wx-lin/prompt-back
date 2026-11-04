"""Saved character configurations and private generated PNG previews."""

import base64
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from app.database import get_db
from app.models.character_asset import CharacterAsset
from app.models.user import User
from app.schemas.character_asset import CharacterAssetInput
from app.services.prompt_analysis.errors import AnalysisError
from app.utils.deps import get_current_user
from app.utils.prompt_analysis_http import PromptAnalysisRoute, success

router = APIRouter(tags=["character-assets"], route_class=PromptAnalysisRoute)
MAX_ASSETS_PER_USER = 100
MAX_STORAGE_BYTES_PER_USER = 100 * 1024 * 1024


def summary(asset):
    return {
        "id": str(asset.id),
        "name": asset.name,
        "template": asset.template,
        "model": asset.model,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
        "has_preview": bool(asset.has_preview),
    }


def detail(asset):
    return {
        "id": str(asset.id),
        "name": asset.name,
        "values": asset.values,
        "template": asset.template,
        "model": asset.model,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
        "has_preview": asset.preview_image is not None,
        "preview_image_base64": base64.b64encode(asset.preview_image).decode()
        if asset.preview_image
        else None,
        "preview_signature": asset.preview_signature,
    }


async def owned(db, user_id, asset_id):
    asset = await db.scalar(
        select(CharacterAsset).where(CharacterAsset.id == asset_id, CharacterAsset.user_id == user_id)
    )
    if asset is None:
        raise HTTPException(status_code=404)
    return asset


async def lock_user(db, user_id):
    # All writes acquire this lock before reading asset state or quota. PostgreSQL
    # READ COMMITTED then sees the preceding writer's committed quota totals.
    exists = await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
    if exists is None:
        raise HTTPException(status_code=401)


async def check_quota(db, user_id, image_size, *, creating, old_image_size=0):
    count, storage = (
        await db.execute(
            select(
                func.count(CharacterAsset.id),
                func.coalesce(func.sum(func.octet_length(CharacterAsset.preview_image)), 0),
            ).where(CharacterAsset.user_id == user_id)
        )
    ).one()
    if creating and count >= MAX_ASSETS_PER_USER:
        raise AnalysisError(429, "ASSET_LIMIT_REACHED", "角色数量已达上限，请先删除不需要的角色")
    if storage - old_image_size + image_size > MAX_STORAGE_BYTES_PER_USER:
        raise AnalysisError(413, "ASSET_STORAGE_LIMIT", "角色图片空间已满，请先删除不需要的角色")


def apply(asset, body):
    asset.name = body.name
    asset.values = dict(body.values)
    asset.template = body.template
    asset.model = body.model
    asset.preview_image = (
        base64.b64decode(body.preview_image_base64) if body.preview_image_base64 is not None else None
    )
    asset.preview_signature = body.preview_signature


@router.get("/api/character-assets")
async def list_assets(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
):
    columns = (
        CharacterAsset.id,
        CharacterAsset.name,
        CharacterAsset.template,
        CharacterAsset.model,
        CharacterAsset.created_at,
        CharacterAsset.updated_at,
        CharacterAsset.preview_image.is_not(None).label("has_preview"),
    )
    result = await db.execute(
        select(*columns)
        .where(CharacterAsset.user_id == user.id)
        .order_by(CharacterAsset.updated_at.desc(), CharacterAsset.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return success(request, [summary(row) for row in result])


@router.post("/api/character-assets")
async def create_asset(
    body: CharacterAssetInput, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    await lock_user(db, user.id)
    asset = CharacterAsset(user_id=user.id)
    apply(asset, body)
    await check_quota(db, user.id, len(asset.preview_image or b""), creating=True)
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return success(request, detail(asset), status_code=201)


@router.get("/api/character-assets/{asset_id}")
async def get_asset(
    asset_id: uuid.UUID, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    return success(request, detail(await owned(db, user.id, asset_id)))


@router.get("/api/character-assets/{asset_id}/image")
async def get_asset_image(asset_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db)):
    asset = await owned(db, user.id, asset_id)
    if asset.preview_image is None:
        raise HTTPException(status_code=404)
    return Response(
        asset.preview_image, media_type="image/png", headers={"Cache-Control": "private, no-store"}
    )


@router.put("/api/character-assets/{asset_id}")
async def update_asset(
    asset_id: uuid.UUID,
    body: CharacterAssetInput,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await lock_user(db, user.id)
    asset = await owned(db, user.id, asset_id)
    old_size = len(asset.preview_image or b"")
    new_size = len(base64.b64decode(body.preview_image_base64)) if body.preview_image_base64 else 0
    await check_quota(db, user.id, new_size, creating=False, old_image_size=old_size)
    apply(asset, body)
    await db.commit()
    await db.refresh(asset)
    return success(request, detail(asset))


@router.delete("/api/character-assets/{asset_id}")
async def delete_asset(asset_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db)):
    await lock_user(db, user.id)
    asset = await owned(db, user.id, asset_id)
    await db.delete(asset)
    await db.commit()
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})
