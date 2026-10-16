from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.seo import SeoImport


def payload(**changes):
    return {
        "engine": "google",
        "dimension": "daily",
        "start_date": "2026-10-01",
        "end_date": "2026-10-02",
        "rows": [{"key": "2026-10-01", "clicks": 0, "impressions": 0}],
        **changes,
    }


def test_valid_zero_and_missing_position():
    item = SeoImport.model_validate(payload())
    assert item.rows[0].position is None
    assert item.start_date == date(2026, 10, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"rows": [{"key": "2026-09-30", "clicks": 0, "impressions": 1}]},
        {"rows": [{"key": "2026-10-01", "clicks": 2, "impressions": 1}]},
        {"rows": [{"key": "2026-10-01", "clicks": -1, "impressions": 1}]},
        {"rows": [{"key": "2026-10-01", "clicks": 0, "impressions": 1, "position": float("inf")}]},
        {
            "dimension": "page",
            "rows": [{"key": "https://aigc-prompt.com.evil.org/x", "clicks": 0, "impressions": 1}],
        },
        {
            "dimension": "page",
            "rows": [{"key": "https://user@aigc-prompt.com/x", "clicks": 0, "impressions": 1}],
        },
        {"end_date": "2026-09-30"},
        {"rows": [{"key": "2026-10-01", "clicks": 0, "impressions": 1}] * 2},
    ],
)
def test_invalid_snapshot(changes):
    with pytest.raises(ValidationError):
        SeoImport.model_validate(payload(**changes))


def test_admin_import_auth_validation_and_body_limit():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.routers.seo import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post("/api/admin/seo/import", json=payload()).status_code == 401
        headers = {"X-Admin-Key": get_settings().admin_secret_key}
        assert client.post("/api/admin/seo/import", content="bad json", headers=headers).status_code == 422
        assert (
            client.post("/api/admin/seo/import", content="x" * 2_000_001, headers=headers).status_code == 413
        )
        assert (
            client.get(
                "/api/admin/seo?start_date=2026-10-02&end_date=2026-10-01", headers=headers
            ).status_code
            == 422
        )


def test_metrics_missing_positions_and_zero():
    from app.services.seo_service import metrics

    assert metrics([{"clicks": 0, "impressions": 0, "position": None}])["position"] is None
    assert metrics([{"clicks": 1, "impressions": 10, "position": None}])["position"] is None
    assert (
        metrics(
            [{"clicks": 1, "impressions": 10, "position": 2}, {"clicks": 1, "impressions": 30, "position": 4}]
        )["position"]
        == 3.5
    )


def test_position_overflow_rejected():
    with pytest.raises(ValidationError):
        SeoImport.model_validate(
            payload(rows=[{"key": "2026-10-01", "clicks": 1, "impressions": 10, "position": 1e308}])
        )
