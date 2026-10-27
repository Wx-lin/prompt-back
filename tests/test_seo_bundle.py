"""External sync remains a strict source contract, not a bypass around metrics validation."""

import asyncio
from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.seo_bundle import SeoBundle
from app.services.google_search_console import GscError
from app.services.seo_bundle_service import export_bundle, parse_bundle


class EmptyProvider:
    async def fetch_day(self, day, dimension):
        return [], False


def test_bundle_roundtrip_empty_is_not_zero():
    bundle = asyncio.run(export_bundle(EmptyProvider(), previous=None, target=date(2026, 10, 20)))
    parsed = parse_bundle(bundle)
    assert parsed.end_date == date(2026, 10, 20) and len(parsed.partitions) == 84
    assert all(not part.rows for part in parsed.partitions)
    assert parsed.site_url == "https://aigc-prompt.com/"


@pytest.mark.parametrize(
    "change",
    [
        {"site_url": "https://evil.example/"},
        {"version": 2},
        {"data_state": "all"},
        {"partitions": []},
        {"end_date": "2026-10-21"},
    ],
)
def test_bundle_strict_shape(change):
    raw = parse_bundle(
        asyncio.run(export_bundle(EmptyProvider(), previous=None, target=date(2026, 10, 20)))
    ).model_dump(mode="json")
    raw.update(change)
    with pytest.raises(ValidationError):
        SeoBundle.model_validate(raw)


def test_size_limit_before_parse():
    with pytest.raises(GscError, match="bundle_too_large"):
        parse_bundle(b"x" * 64_000_001)


@pytest.mark.parametrize(
    "dimension,key,clicks,impressions",
    [
        ("page", "https://evil.example/", 0, 1),
        ("query", "abc", 2, 1),
        ("daily", "2026-10-01", 0, 1),
        ("query", "\x00", 0, 1),
    ],
)
def test_import_validates_each_source_row(dimension, key, clicks, impressions):
    from app.schemas.seo_bundle import SeoPartition

    with pytest.raises(ValidationError):
        SeoPartition(
            day=date(2026, 10, 20),
            dimension=dimension,
            rows=[{"key": key, "clicks": clicks, "impressions": impressions}],
            truncated=False,
        )


def test_external_failure_rejects_unsafe_codes_before_database(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.services import seo_bundle_service

    monkeypatch.setattr(seo_bundle_service, "get_settings", lambda: SimpleNamespace(gsc_sync_mode="external"))
    monkeypatch.setattr(seo_bundle_service, "configured", lambda: True)
    with pytest.raises(GscError, match="invalid_bundle"):
        asyncio.run(
            seo_bundle_service.record_external_failure(None, datetime.now(UTC), "private-upstream-secret")
        )


@pytest.mark.parametrize("impressions,accepted", [(0, True), (1, False)])
def test_bundle_zero_rank_requires_zero_impressions(impressions, accepted):
    from app.schemas.seo_bundle import SeoPartition

    raw = {
        "day": "2026-10-20",
        "dimension": "daily",
        "truncated": False,
        "rows": [{"key": "2026-10-20", "clicks": 0, "impressions": impressions, "position": 0}],
    }
    if accepted:
        assert SeoPartition.model_validate(raw).rows[0].position is None
    else:
        with pytest.raises(ValidationError):
            SeoPartition.model_validate(raw)
