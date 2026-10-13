import uuid

import pytest
from pydantic import ValidationError

from app.schemas.homepage import FeaturedUpdate
from app.services.homepage_service import queue_revision


def test_featured_validation():
    identifier = uuid.uuid4()
    for ids in [[identifier, identifier], [uuid.uuid4() for _ in range(51)]]:
        with pytest.raises(ValidationError):
            FeaturedUpdate(prompt_ids=ids, revision="a" * 64)
    assert FeaturedUpdate(prompt_ids=[], revision="a" * 64).prompt_ids == []


def test_revision_is_order_sensitive_and_stable():
    first, second = uuid.uuid4(), uuid.uuid4()
    assert queue_revision([first, second]) == queue_revision([first, second])
    assert queue_revision([first, second]) != queue_revision([second, first])


def test_featured_api_auth_and_validation():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.admin import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/admin/homepage/featured").status_code == 401
    assert (
        client.put("/api/admin/homepage/featured", json={"prompt_ids": [], "revision": "a" * 64}).status_code
        == 401
    )


def test_thumbnail_skips_blank_first_image():
    from types import SimpleNamespace

    from app.services.homepage_service import thumbnail_for_prompt

    row = SimpleNamespace(
        images=[
            SimpleNamespace(image_url=" ", thumbnail_url=""),
            SimpleNamespace(image_url="https://example.com/image.jpg", thumbnail_url=" "),
        ]
    )
    assert thumbnail_for_prompt(row) == "https://example.com/image.jpg"
    assert thumbnail_for_prompt(SimpleNamespace(images=[])) is None
