"""Controlled gallery imports never fetch user URLs or read private namespaces."""

import io
from types import SimpleNamespace

import pytest

from app.services.prompt_analysis import public_sources, storage
from app.services.prompt_analysis.errors import AnalysisError


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(
        cos_bucket="gallery-test",
        cos_region="ap-guangzhou",
        cos_cdn_domain="https://images.example.test/media",
    )
    monkeypatch.setattr(public_sources, "get_settings", lambda: value)
    monkeypatch.setattr(storage, "get_settings", lambda: value)
    monkeypatch.setattr(storage, "require_storage_configuration", lambda: None)
    return value


@pytest.mark.parametrize(
    "url",
    [
        "http://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com:8443/prompts/a.jpg",
        "https://user:password@gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a.jpg?download=private",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a.jpg#fragment",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/../private/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a%2fprivate.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompt-analysis/a/image.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a.gif",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com.attacker.test/prompts/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com.attacker.test/library/a.jpg",
        "https://other-bucket.cos.ap-guangzhou.myqcloud.com/library/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/private/library/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/library-other/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/library/../prompt-analysis/a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/library//a.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/library/a%2fprivate.jpg",
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/library/a.jpg?signature=private",
        "https://images.example.test/other/prompts/a.jpg",
        "http://127.0.0.1/prompts/a.jpg",
        "file:///prompts/a.jpg",
        "not a URL",
    ],
)
def test_uncontrolled_sources_are_not_object_keys(settings, url):
    with pytest.raises(AnalysisError) as error:
        public_sources.object_key(url)
    assert error.value.code == "SOURCE_IMAGE_UNAVAILABLE"


def test_only_known_origin_or_cdn_prefix_is_mapped(settings):
    assert (
        public_sources.object_key("https://gallery-test.cos.ap-guangzhou.myqcloud.com/prompts/a.png")
        == "prompts/a.png"
    )
    assert (
        public_sources.object_key("https://images.example.test/media/submissions/user/a.jpg")
        == "submissions/user/a.jpg"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://gallery-test.cos.ap-guangzhou.myqcloud.com/library/2026/10/public-0.webp",
        "https://images.example.test/media/library/2026/10/public-0.webp",
    ],
)
def test_real_gallery_library_namespace_maps_only_under_configured_roots(settings, url):
    assert public_sources.object_key(url) == "library/2026/10/public-0.webp"


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("key", ["prompts/a.png", "library/2026/10/public-0.webp"])
async def test_sdk_reads_only_configured_bucket_with_bound_and_always_closes(
    settings, monkeypatch, oversized, key
):
    stream = io.BytesIO(b"x" * (5 * 1024 * 1024 + 1) if oversized else b"public image bytes")
    calls = []

    def get_object(**options):
        calls.append(options)
        return {"Body": SimpleNamespace(get_raw_stream=lambda: stream), "Content-Type": "image/png"}

    monkeypatch.setattr(storage.cos_service, "_build_client", lambda: SimpleNamespace(get_object=get_object))
    if oversized:
        with pytest.raises(AnalysisError) as error:
            await storage.read_public_prompt(key)
        assert error.value.status_code == 413
    else:
        assert await storage.read_public_prompt(key) == (b"public image bytes", "image/png")
    assert stream.closed and calls == [{"Bucket": "gallery-test", "Key": key}]
