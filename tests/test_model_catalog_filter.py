from unittest.mock import AsyncMock

import pytest

from app.services import model_service


@pytest.mark.asyncio
async def test_catalog_excludes_non_models_without_discarding_real_versions(monkeypatch):
    labels = [
        "男明星",
        "AI头像",
        "Leica SL2",
        "75mm f/2",
        "Adobe Illustrator",
        "Midjourney 人像",
        "GPT Image 2.0",
        "Nano Banana Pro",
        "Gemini 2.5",
        "Grok Image 2.0",
        "Seedream 5Pro",
        "custom Model",
    ]
    monkeypatch.setattr(model_service, "published_labels", AsyncMock(return_value=labels))
    catalog = await model_service.list_models(AsyncMock())
    keys = {item["key"] for item in catalog}
    assert not keys.intersection(labels[:6] + ["custom Model"])
    assert {"GPT Image 2.0", "Nano Banana Pro", "Gemini 2.5", "Grok Image 2.0", "Seedream 5Pro"} <= keys
    assert labels[0] == "男明星"


@pytest.mark.parametrize("label", ["Midjourney V6 男明星", "GPT Image 2 AI头像", "FLUX.1 Leica SL2"])
def test_model_name_prefix_does_not_make_arbitrary_tags_valid(label):
    from app.utils.model_labels import is_catalog_model

    assert not is_catalog_model(label)
