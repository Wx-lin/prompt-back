"""Category upgrade contracts."""

import pytest
from pydantic import ValidationError

from app.routers.categories import CATEGORY_LABELS
from app.schemas.admin import AdminPromptCreate, AdminPromptUpdate
from app.schemas.crawl import CrawlDraftApprove, CrawlDraftReviewUpdate


def test_category_labels_cover_all_prd_categories():
    assert list(CATEGORY_LABELS) == [
        "portrait",
        "avatar",
        "poster",
        "product",
        "illustration",
        "character",
        "sticker",
        "wallpaper",
        "infographic",
        "branding",
        "architecture",
        "scene",
    ]
    assert CATEGORY_LABELS["avatar"] == "头像"


@pytest.mark.parametrize("key", ["photography", "image_edit", "workflow", "invalid"])
@pytest.mark.parametrize(
    "schema,field,other",
    [
        (AdminPromptCreate, "category", {"title": "t", "prompt_text": "t"}),
        (AdminPromptUpdate, "category", {}),
        (CrawlDraftApprove, "category", {"title": "t"}),
        (CrawlDraftReviewUpdate, "extracted_category", {}),
    ],
)
def test_write_rejects_old_and_invalid_categories(key, schema, field, other):
    with pytest.raises(ValidationError):
        schema(**{field: key, **other})


def test_uncertain_review_keeps_empty_category_for_human_review():
    import json

    from app.services.content_review_service import _parse_response

    result = _parse_response(
        json.dumps(
            {
                "images": [],
                "suggested_title": "教程",
                "cleaned_prompt_text": "操作教程",
                "suggested_category": "",
            }
        ),
        0,
    )
    assert result.suggested_category == ""


@pytest.mark.parametrize("key", list(CATEGORY_LABELS))
def test_new_categories_allowed_everywhere(key):
    import uuid

    from app.schemas.submission import SubmissionApprove, SubmissionCreate

    assert AdminPromptCreate(title="t", prompt_text="p", category=key).category == key
    assert CrawlDraftApprove(title="t", category=key).category == key
    assert SubmissionApprove(category=key).category == key
    assert (
        SubmissionCreate(
            title="t", prompt_text="p", category=key, models=["MJ V6"], upload_ids=[uuid.uuid4()]
        ).category
        == key
    )


def test_catalog_response_preserves_empty_categories_and_metadata():
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from app.routers.categories import list_categories

    db = Mock(execute=AsyncMock(return_value=Mock(all=Mock(return_value=[("portrait", 2)]))))
    rows = asyncio.run(list_categories(db))
    assert len(rows) == 12
    assert rows[0] == {
        "key": "portrait",
        "label": "人像写真",
        "label_en": "Portraits",
        "order": 1,
        "count": 2,
    }
    assert rows[-1]["count"] == 0 and rows[-1]["order"] == 12


@pytest.mark.parametrize("key", ["photography", "image_edit", "workflow", "unknown_historical"])
def test_admin_output_preserves_legacy_category(key):
    import uuid
    from datetime import UTC, datetime

    from app.schemas.admin import AdminPromptOut
    from app.schemas.crawl import CrawlDraftOut

    now = datetime.now(UTC)
    prompt = AdminPromptOut(
        id=uuid.uuid4(),
        title="旧内容",
        prompt_text="原文",
        category=key,
        models=[],
        is_published=True,
        view_count=0,
        like_count=0,
        collect_count=0,
        created_at=now,
        updated_at=now,
    )
    draft = CrawlDraftOut(
        id=uuid.uuid4(),
        source_platform="xhs",
        source_note_id="old",
        source_url="https://example.com",
        raw_content="原文",
        extracted_category=key,
        status="pending",
        created_at=now,
    )
    assert prompt.model_dump(mode="json")["category"] == key
    assert draft.model_dump(mode="json")["extracted_category"] == key


def test_admin_patch_category_may_be_omitted_but_not_cleared():
    assert "category" not in AdminPromptUpdate().model_fields_set
    with pytest.raises(ValidationError):
        AdminPromptUpdate(category=None)


@pytest.mark.parametrize("method", ["publish", "patch"])
def test_legacy_content_cannot_be_republished_without_valid_category(method, monkeypatch):
    import asyncio
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import admin_service
    from app.services.submission_service import SubmissionConflict

    prompt = SimpleNamespace(category="workflow", review_status=None, is_published=False)
    db = AsyncMock()
    db.scalar.return_value = prompt
    monkeypatch.setattr(admin_service, "get_prompt_for_admin", AsyncMock(return_value=prompt))

    async def run():
        with pytest.raises(SubmissionConflict):
            if method == "publish":
                await admin_service.set_published(db, uuid.uuid4(), True)
            else:
                await admin_service.update_prompt(db, uuid.uuid4(), AdminPromptUpdate(is_published=True))
        assert prompt.is_published is False
        db.commit.assert_not_awaited()

    asyncio.run(run())
