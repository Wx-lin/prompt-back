"""Real PostgreSQL transaction tests, isolated schema; COS transport is mocked."""

import asyncio
import io
import os
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models import SubmissionAsset, User
from app.redis import get_redis_client
from app.routers import admin, admin_submissions, prompts, submissions, users
from app.services import cos_service, submission_cleanup, submission_service
from app.utils.admin_auth import verify_admin_key
from app.utils.deps import get_current_user
from app.utils.submission_body_limit import SubmissionBodyLimit

pytestmark = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Requires isolated test DB")


def test_submission_authorization_review_and_publication(monkeypatch):
    monkeypatch.setattr(
        cos_service, "upload_submission_image", AsyncMock(return_value="https://cos.example/test.jpg")
    )
    monkeypatch.setattr(cos_service, "submission_preview_url", lambda key: "https://private.example/" + key)
    visibility = AsyncMock()
    monkeypatch.setattr(cos_service, "set_submission_visibility", visibility)
    monkeypatch.setattr(cos_service, "delete_submission_image", AsyncMock())

    async def run():
        schema = "test_submissions_" + uuid.uuid4().hex
        admin_engine = create_async_engine(os.environ["AIGC_TEST_DATABASE_URL"])
        engine = create_async_engine(
            os.environ["AIGC_TEST_DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}}
        )
        try:
            async with admin_engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                author = User(nickname="投稿作者", is_active=True)
                other = User(nickname="其他用户", is_active=True)
                db.add_all([author, other])
                await db.commit()
            app = FastAPI()
            app.add_middleware(SubmissionBodyLimit)
            for router in (
                submissions.router,
                admin_submissions.router,
                prompts.router,
                users.router,
                admin.router,
            ):
                app.include_router(router)

            async def session():
                async with sessions() as db:
                    yield db

            async def current(request: Request):
                value = request.headers.get("authorization", "")
                if value == "Bearer author":
                    return author
                if value == "Bearer other":
                    return other
                raise HTTPException(401)

            async def admin_key(request: Request):
                if request.headers.get("X-Admin-Key") != "test-admin":
                    raise HTTPException(401)

            class Redis:
                async def eval(self, *_):
                    return 1

            app.dependency_overrides.update(
                {
                    get_db: session,
                    get_current_user: current,
                    get_redis_client: lambda: Redis(),
                    verify_admin_key: admin_key,
                }
            )
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                image = io.BytesIO()
                Image.new("RGB", (10, 10), "red").save(image, format="PNG")
                data = image.getvalue()
                assert (
                    await client.post("/api/submissions/upload", files={"file": ("x.png", data, "image/png")})
                ).status_code == 401
                headers = {"Authorization": "Bearer author"}
                upload = await client.post(
                    "/api/submissions/upload", files={"file": ("x.png", data, "image/png")}, headers=headers
                )
                assert upload.status_code == 201, upload.text
                body = {
                    "title": "用户测试作品",
                    "prompt_text": "red cat",
                    "category": "portrait",
                    "models": ["MJ V6"],
                    "upload_ids": [upload.json()["id"]],
                }
                assert (await client.post("/api/submissions", json=body)).status_code == 401
                assert (
                    await client.post(
                        "/api/submissions", json=body, headers={"Authorization": "Bearer other"}
                    )
                ).status_code == 409
                created = await client.post("/api/submissions", json=body, headers=headers)
                assert created.status_code == 201, created.text
                result = created.json()
                pid = result["id"]
                assert result["review_status"] == "pending" and not result["published"]
                assert result["models"] == ["Midjourney V6"]
                assert result["image_urls"][0].startswith("https://private.example/")
                assert (await client.post("/api/submissions", json=body, headers=headers)).status_code == 409
                assert (await client.get(f"/api/prompts/{pid}")).status_code == 404
                assert (
                    await client.get("/api/submissions", headers={"Authorization": "Bearer other"})
                ).json()["total"] == 0
                assert (await client.get("/api/submissions", headers=headers)).json()["total"] == 1
                assert (await client.get("/api/admin/submissions")).status_code == 401
                aheaders = {"X-Admin-Key": "test-admin"}
                # Generic admin publication must not bypass the dedicated review transaction.
                assert (
                    await client.post(f"/api/admin/prompts/{pid}/publish", headers=aheaders)
                ).status_code == 409
                approved = await client.post(f"/api/admin/submissions/{pid}/approve", headers=aheaders)
                assert approved.status_code == 200, approved.text
                assert approved.json()["published"] and approved.json()["review_status"] == "approved"
                assert visibility.await_args.args[1] is True
                assert (await client.get(f"/api/prompts/{pid}")).status_code == 200
                assert (
                    await client.post(f"/api/admin/submissions/{pid}/approve", headers=aheaders)
                ).status_code == 409
                assert (
                    await client.post(f"/api/admin/prompts/{pid}/unpublish", headers=aheaders)
                ).status_code == 200
                assert (await client.get(f"/api/prompts/{pid}")).status_code == 404
                assert visibility.await_args.args[1] is False
                upload2 = await client.post(
                    "/api/submissions/upload", files={"file": ("x.png", data, "image/png")}, headers=headers
                )
                body["upload_ids"] = [upload2.json()["id"]]
                pid2 = (await client.post("/api/submissions", json=body, headers=headers)).json()["id"]
                assert (
                    await client.post(
                        f"/api/admin/submissions/{pid2}/reject", json={"note": " "}, headers=aheaders
                    )
                ).status_code == 422
                reject = await client.post(
                    f"/api/admin/submissions/{pid2}/reject", json={"note": "缺少可复现参数"}, headers=aheaders
                )
                assert reject.status_code == 200 and reject.json()["review_note"] == "缺少可复现参数"
                assert (await client.get(f"/api/prompts/{pid2}")).status_code == 404
                assert (
                    await client.post(f"/api/admin/submissions/{pid2}/approve", headers=aheaders)
                ).status_code == 409
                # Timeout after a remote ACL change must reconcile every attempted image.
                upload3 = await client.post(
                    "/api/submissions/upload", files={"file": ("x.png", data, "image/png")}, headers=headers
                )
                body["upload_ids"] = [upload3.json()["id"]]
                pid3 = (await client.post("/api/submissions", json=body, headers=headers)).json()["id"]
                visibility.reset_mock()
                visibility.side_effect = [TimeoutError("remote timeout"), None]
                failed = await client.post(f"/api/admin/submissions/{pid3}/approve", headers=aheaders)
                assert failed.status_code == 503
                assert visibility.await_args_list[-1].args[1] is False
                visibility.side_effect = None
                rows = (await client.get("/api/submissions", headers=headers)).json()["items"]
                assert next(row for row in rows if row["id"] == pid3)["review_status"] == "pending"
                # Deletion revokes visibility, and periodic cleanup deletes consumed orphan assets.
                assert (await client.delete(f"/api/admin/prompts/{pid}/", headers=aheaders)).status_code in (
                    307,
                    404,
                )
                assert (await client.delete(f"/api/admin/prompts/{pid}", headers=aheaders)).status_code == 204
                async with sessions() as db:
                    await submission_cleanup.cleanup_assets(db)
                    asset = await db.get(SubmissionAsset, uuid.UUID(upload.json()["id"]))
                    assert asset is None
                # Rollback expires ORM fields: restoration must use keys cached before rollback.
                async with sessions() as db:
                    visibility.reset_mock()
                    db.commit = AsyncMock(side_effect=RuntimeError("commit failed"))
                    with pytest.raises(RuntimeError, match="commit failed"):
                        await submission_service.review_submission(db, uuid.UUID(pid3), True)
                    assert visibility.await_args.args[1] is False
                # Every object beyond the first batch receives ACL reconciliation.
                async with sessions() as db:
                    for index in range(101):
                        db.add(
                            SubmissionAsset(
                                user_id=author.id,
                                object_key=f"bulk/{index}.jpg",
                                image_url=f"https://cos.example/bulk/{index}.jpg",
                                prompt_id=uuid.UUID(pid3),
                            )
                        )
                    await db.commit()
                    visibility.reset_mock()
                    await submission_cleanup.cleanup_assets(db)
                    seen = {call.args[0] for call in visibility.await_args_list}
                    assert {f"bulk/{index}.jpg" for index in range(101)} <= seen
        finally:
            await engine.dispose()
            async with admin_engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(run())
