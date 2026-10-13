import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.content_review_service import ContentReviewError, _parse_response


def payload(**changes):
    return json.dumps(
        dict(
            images=[dict(index=0, is_credible=True, reason="AI")],
            suggested_title="风景",
            cleaned_prompt_text="水墨山水",
            suggested_category="illustration",
            **changes,
        )
    )


@pytest.mark.parametrize(
    "images",
    [
        [dict(index=0, is_credible="false", reason="bad")],
        [],
        [dict(index=1, is_credible=True, reason="bad")],
        [dict(index=0, is_credible=True), dict(index=0, is_credible=True)],
        [dict(index=True, is_credible=True)],
    ],
)
def test_invalid_model_images_fail_closed(images):
    data = json.loads(payload())
    data["images"] = images
    with pytest.raises(ContentReviewError):
        _parse_response(json.dumps(data), 1)


def test_auto_publish_uploads_before_review_and_keeps_only_credible(monkeypatch):
    from app.services import auto_review_service as svc

    draft = SimpleNamespace(
        id="draft",
        status="pending",
        approved_prompt_id=None,
        images=[
            SimpleNamespace(local_path="a.jpg", sort_order=0),
            SimpleNamespace(local_path="b.jpg", sort_order=1),
        ],
        raw_content="original",
        raw_title="original",
        source_url="https://source",
        author_name="author",
        author_avatar_url="https://example.com/avatar.jpg",
        extracted_models=None,
    )
    db = Mock()
    db.execute = AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=draft)))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    events = []

    async def upload(path):
        events.append("upload")
        return "https://cos/" + str(path)

    async def review(**kwargs):
        assert events == ["upload", "upload"]
        assert len(kwargs["image_urls"]) == 2
        data = json.loads(payload())
        data["images"].append(dict(index=1, is_credible=False, reason="photo"))
        return _parse_response(json.dumps(data), 2)

    monkeypatch.setattr(svc, "_resolve_local_file_path", lambda path: path)
    monkeypatch.setattr(svc.cos_service, "upload_prompt_image", upload)
    monkeypatch.setattr(
        svc.cos_service, "upload_author_avatar", AsyncMock(return_value="https://cos.example/avatar.jpg")
    )
    monkeypatch.setattr(svc.content_review_service, "review_draft_content", review)
    result = asyncio.run(svc.auto_publish_draft(db, "draft"))
    assert result == "published"
    assert draft.status == "approved"
    added = [c.args[0] for c in db.add.call_args_list]
    assert len(added) == 2
    assert added[0].source_url == "https://source"
    assert added[0].author_avatar_url == "https://cos.example/avatar.jpg"
    assert added[0].is_published is True


@pytest.mark.parametrize("failure", ["api", "empty", "no_images", "untrusted", "upload"])
def test_failed_review_never_publishes(monkeypatch, failure):
    from app.services import auto_review_service as svc

    draft = SimpleNamespace(
        id="draft",
        status="pending",
        approved_prompt_id=None,
        images=[] if failure == "no_images" else [SimpleNamespace(local_path="a.jpg", sort_order=0)],
        raw_content="original",
        raw_title="original",
        source_url="https://source",
        author_name="author",
        author_avatar_url=None,
        extracted_models=None,
    )
    db = Mock()
    db.execute = AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=draft)))
    db.commit = AsyncMock()
    monkeypatch.setattr(svc, "_resolve_local_file_path", lambda p: p)
    monkeypatch.setattr(
        svc.cos_service,
        "upload_prompt_image",
        AsyncMock(
            side_effect=RuntimeError("upload") if failure == "upload" else None, return_value="https://cos/a"
        ),
    )
    result = _parse_response(payload(), 1)
    if failure == "empty":
        result.cleaned_prompt_text = " "
    if failure == "untrusted":
        result.images[0].is_credible = False
    monkeypatch.setattr(
        svc.content_review_service,
        "review_draft_content",
        AsyncMock(
            side_effect=ContentReviewError("failed") if failure == "api" else None, return_value=result
        ),
    )
    assert asyncio.run(svc.auto_publish_draft(db, "draft")) == "held"
    db.add.assert_not_called()
    assert draft.status == "pending"
    assert draft.review_note


def test_approved_is_idempotent():
    from app.services import auto_review_service as svc

    db = Mock()
    db.execute = AsyncMock(
        return_value=Mock(
            scalar_one_or_none=Mock(
                return_value=SimpleNamespace(status="approved", approved_prompt_id="existing")
            )
        )
    )
    assert asyncio.run(svc.auto_publish_draft(db, "draft")) == "skipped"
    db.add.assert_not_called()
