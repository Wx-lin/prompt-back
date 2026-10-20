"""Collection list contracts, including covers with no generated thumbnail."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.database import get_db
from app.routers import users
from app.utils.deps import get_current_user

ORIGINAL = "https://images.example.com/lip-mask.jpg"
THUMBNAIL = "https://images.example.com/lip-mask-small.jpg"


def collected_prompt(images):
    return SimpleNamespace(
        id=uuid.uuid4(),
        title="唇膜产品平拍摄影",
        category="product",
        models=[],
        images=[SimpleNamespace(**image) for image in images],
        like_count=1,
        view_count=0,
        author_name="Oogie",
        created_at=datetime(2026, 10, 15, tzinfo=UTC),
    )


def list_database(rows, total):
    return SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Mock(scalar_one=Mock(return_value=total)),
                Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=rows)))),
            ]
        )
    )


def collection_app(db, user=None):
    app = FastAPI()
    app.include_router(users.router)
    app.dependency_overrides[get_db] = lambda: db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.mark.parametrize(
    "images,expected",
    [
        ([{"image_url": ORIGINAL, "thumbnail_url": THUMBNAIL}], THUMBNAIL),
        ([{"image_url": ORIGINAL, "thumbnail_url": None}], ORIGINAL),
        ([{"image_url": ORIGINAL, "thumbnail_url": "  "}], ORIGINAL),
        (
            [
                {"image_url": " ", "thumbnail_url": ""},
                {"image_url": ORIGINAL, "thumbnail_url": None},
            ],
            ORIGINAL,
        ),
        ([], None),
    ],
    ids=["thumbnail", "missing-thumbnail", "blank-thumbnail", "blank-first-image", "no-image"],
)
def test_collection_cover_uses_original_when_thumbnail_is_unavailable(images, expected):
    prompt = collected_prompt(images)
    db = list_database([prompt], total=1)
    user = SimpleNamespace(id=uuid.uuid4())
    with TestClient(collection_app(db, user)) as client:
        response = client.get("/api/users/me/collects")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["thumbnail_url"] == expected
    assert body["items"][0]["id"] == str(prompt.id)
    assert body["items"][0]["title"] == prompt.title
    assert body["total"] == 1
    assert body["page"] == 1 and body["page_size"] == 20 and body["has_more"] is False


def test_collections_preserve_pagination_user_scope_and_publication_boundary():
    prompt = collected_prompt([{"image_url": ORIGINAL, "thumbnail_url": THUMBNAIL}])
    db = list_database([prompt], total=41)
    user = SimpleNamespace(id=uuid.uuid4())
    with TestClient(collection_app(db, user)) as client:
        response = client.get("/api/users/me/collects?page=2&page_size=20")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 41
    assert body["page"] == 2 and body["page_size"] == 20 and body["has_more"] is True
    assert len(body["items"]) == 1
    assert db.execute.await_count == 2
    count_query, list_query = [call.args[0] for call in db.execute.await_args_list]
    for query in (count_query, list_query):
        compiled = query.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "user_collects.user_id =" in sql
        assert user.id in compiled.params.values()
        assert "prompts.is_published IS true" in sql
        assert "prompts.review_status IS NULL" in sql
        assert "approved" in compiled.params.values()
        assert "ORDER BY user_collects.created_at DESC" in sql
    assert list_query._limit_clause.value == 20
    assert list_query._offset_clause.value == 20


def test_collections_require_login_before_querying_records():
    db = list_database([], total=0)
    with TestClient(collection_app(db)) as client:
        response = client.get("/api/users/me/collects")

    assert response.status_code == 401
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=51"])
def test_collections_reject_invalid_pagination(query):
    db = list_database([], total=0)
    with TestClient(collection_app(db, SimpleNamespace(id=uuid.uuid4()))) as client:
        response = client.get("/api/users/me/collects?" + query)

    assert response.status_code == 422
    db.execute.assert_not_awaited()
