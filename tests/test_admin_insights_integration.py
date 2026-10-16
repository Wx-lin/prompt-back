"""Real isolated PostgreSQL aggregation, literal search, and privacy contract."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import CrawlDraft, Prompt, PromptImage, User
from app.schemas.admin_insights import AdminUserList
from app.services.admin_insights_service import get_dashboard, list_users

pytestmark = pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit isolated test DB required"
)


def test_aggregates_search_and_pagination():
    async def run():
        schema = "test_insights_" + uuid.uuid4().hex
        url = os.environ["AIGC_TEST_DATABASE_URL"]
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                now = datetime(2026, 10, 16, 16, 30, tzinfo=UTC)
                start = datetime(2026, 10, 16, 16, tzinfo=UTC)
                with patch("app.services.admin_insights_service.utc_now", return_value=now):
                    empty = await get_dashboard(db)
                    assert empty["users"]["total"] == empty["content"]["total"] == 0
                    assert len(empty["categories"]) == 12
                    assert (await list_users(db, "", None, 1, 20))["items"] == []
                    users = [
                        User(
                            id=uuid.UUID(int=i + 1),
                            nickname=name,
                            email=email,
                            google_id=f"secret-{i}",
                            avatar_url="private-avatar",
                            is_active=i != 1,
                            created_at=created,
                        )
                        for i, (name, email, created) in enumerate(
                            [
                                ("Literal%_\\", "one@example.com", start),
                                (None, "two@example.com", start + timedelta(hours=1)),
                                ("Normal", None, start - timedelta(microseconds=1)),
                                ("Future", "future@example.com", start + timedelta(days=1)),
                                ("Same", None, start),
                            ]
                        )
                    ]
                    db.add_all(users)
                    for i, (category, published, status) in enumerate(
                        [
                            ("portrait", True, None),
                            ("portrait", True, "approved"),
                            ("portrait", True, "pending"),
                            ("portrait", True, "rejected"),
                            ("avatar", False, "pending"),
                            ("avatar", False, "rejected"),
                            ("legacy", False, None),
                            ("legacy", True, "approved"),
                            ("legacy", True, "unknown"),
                            ("avatar", False, "approved"),
                        ]
                    ):
                        prompt = Prompt(
                            title=str(i),
                            prompt_text="body",
                            category=category,
                            is_published=published,
                            review_status=status,
                        )
                        db.add(prompt)
                        await db.flush()
                        if i == 0:
                            db.add(PromptImage(prompt_id=prompt.id, image_url="https://example.com/i"))
                    db.add_all(
                        [
                            CrawlDraft(
                                source_platform="x",
                                source_note_id=str(i),
                                source_url="x",
                                raw_content="body",
                                status=status,
                            )
                            for i, status in enumerate(["pending", "approved", "rejected"])
                        ]
                    )
                    await db.commit()
                    result = await get_dashboard(db)
                    assert result["generated_at"] == now
                    assert result["users"] == {"total": 5, "enabled": 4, "disabled": 1, "today_new": 3}
                    assert result["content"] == {
                        "total": 10,
                        "published": 3,
                        "pending": 2,
                        "rejected": 2,
                        "other_unpublished": 3,
                        "images": 1,
                    }
                    assert result["crawl"] == {"total": 3, "pending": 1}
                    assert len(result["categories"]) == 13
                    assert result["categories"][-1] == {
                        "key": "__other__",
                        "label": "其他/历史分类",
                        "total": 3,
                        "published": 1,
                    }
                    assert sum(row["total"] for row in result["categories"]) == 10
                    assert sum(row["published"] for row in result["categories"]) == 3
                    assert next(row for row in result["categories"] if row["key"] == "sticker")["total"] == 0
                for term in ["%", "_", "\\", " Literal%_\\ "]:
                    found = await list_users(db, term, None, 1, 20)
                    assert found["total"] == 1 and found["all_users_total"] == 5
                assert (await list_users(db, "EXAMPLE.COM", False, 1, 20))["total"] == 1
                first = await list_users(db, "", None, 1, 2)
                second = await list_users(db, "", None, 2, 2)
                assert [row["id"] for row in first["items"] + second["items"]] == [
                    users[i].id for i in [3, 1, 4, 0]
                ]
                serialized = AdminUserList.model_validate(first).model_dump(mode="json")
                assert set(serialized["items"][0]) == {
                    "id",
                    "nickname",
                    "email",
                    "is_active",
                    "created_at",
                    "last_login_at",
                }
                assert (await list_users(db, "", None, 99, 20))["total"] == 5
                beyond = await list_users(db, "no-match", None, 99, 20)
                assert beyond["items"] == [] and beyond["total"] == 0 and beyond["all_users_total"] == 5
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())
