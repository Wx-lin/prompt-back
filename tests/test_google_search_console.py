"""Fixed-property, finalized and bounded Google API transport contract."""

import asyncio
from datetime import date

import httpx
import pytest

from app.services.google_search_console import GoogleSearchConsole, GscError


def test_final_fixed_property_pagination_and_no_zero_fill():
    async def run():
        requests = []

        async def handler(request):
            import json

            body = json.loads(request.content)
            requests.append((request, body))
            if body["startRow"] == 0:
                return httpx.Response(
                    200,
                    json={"rows": [{"keys": ["ai 提示词"], "clicks": 1, "impressions": 10, "position": 4}]},
                )
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GoogleSearchConsole(client, lambda: "test-token", page_size=1)
            rows, truncated = await provider.fetch_day(date(2026, 10, 1), "query")
            assert rows[0]["key"] == "ai 提示词"
            assert not truncated and len(requests) == 2
            assert all(body["dataState"] == "final" and body["type"] == "web" for _, body in requests)
            assert "https%3A%2F%2Faigc-prompt.com%2F" in str(requests[0][0].url)
            assert requests[0][0].headers["authorization"] == "Bearer test-token"

    asyncio.run(run())


def test_retry_bound_and_safe_errors():
    async def run():
        attempts = []

        async def handler(request):
            attempts.append(1)
            return httpx.Response(503, text="private-token-upstream-body")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GoogleSearchConsole(client, lambda: "secret", sleep=lambda _: asyncio.sleep(0))
            with pytest.raises(GscError) as error:
                await provider.fetch_day(date(2026, 10, 1), "daily")
            assert str(error.value) == "upstream_unavailable"
            assert len(attempts) == 3

    asyncio.run(run())


def test_empty_rows_stays_empty_and_truncation_reported():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        ) as client:
            assert await GoogleSearchConsole(client, lambda: "x").fetch_day(date(2026, 10, 1), "daily") == (
                [],
                False,
            )
        response = {"rows": [{"keys": ["word"], "clicks": 0, "impressions": 1, "position": 2}]}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as client:
            assert (
                await GoogleSearchConsole(client, lambda: "x", page_size=1, max_rows=1).fetch_day(
                    date(2026, 10, 1), "query"
                )
            )[1]

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body,code",
    [
        (403, "private", "permission_denied"),
        (400, "private", "upstream_rejected"),
        (200, "bad json", "invalid_response"),
        (200, '{"rows":[{"keys":["2026-09-01"],"clicks":1,"impressions":2}]}', "invalid_response"),
        (200, '{"rows":"bad"}', "invalid_response"),
        (200, '{"rows":[{"keys":["2026-10-01"],"clicks":2.2,"impressions":3}]}', "invalid_response"),
    ],
)
def test_bad_responses_do_not_expose_body(status, body, code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body))
        ) as client:
            with pytest.raises(GscError, match=code):
                await GoogleSearchConsole(client, lambda: "x").fetch_day(date(2026, 10, 1), "daily")

    asyncio.run(run())


def test_credential_file_rejects_non_google_token_uri(tmp_path):
    from app.services.google_search_console import ServiceAccountToken

    file = tmp_path / "credential.json"
    file.write_text(
        '{"type":"service_account","token_uri":"https://evil.example/token","private_key":"secret"}'
    )
    with pytest.raises(GscError, match="credentials_unavailable"):
        asyncio.run(ServiceAccountToken(str(file))())


def test_credential_scope_timeout_and_reuse(tmp_path, monkeypatch):
    import json
    from uuid import uuid4

    fixture_value = str(uuid4())

    from app.services import google_search_console as module

    file = tmp_path / "credential.json"
    file.write_text(
        json.dumps({"type": "service_account", "token_uri": "https://oauth2.googleapis.com/token"})
    )

    class Credential:
        valid = False
        token = fixture_value

        def refresh(self, request):
            request(url="https://oauth2.googleapis.com/token", method="POST", timeout=1000)
            self.valid = True

    credential = Credential()
    seen = []

    def from_info(info, scopes):
        assert scopes == [module.SCOPE]
        seen.append("loaded")
        return credential

    monkeypatch.setattr(module.service_account.Credentials, "from_service_account_info", from_info)
    monkeypatch.setattr(module, "Request", lambda: lambda **kwargs: seen.append(kwargs["timeout"]))

    async def run():
        token = module.ServiceAccountToken(str(file))
        assert await token() == fixture_value
        assert await token() == fixture_value
        assert seen == ["loaded", 20]

    asyncio.run(run())


@pytest.mark.parametrize("impressions,position,accepted", [(0, 0, True), (1, 0, False)])
def test_google_zero_position_is_only_missing_for_zero_impressions(impressions, position, accepted):
    async def run():
        response = {
            "rows": [{"keys": ["2026-10-20"], "clicks": 0, "impressions": impressions, "position": position}]
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as client:
            provider = GoogleSearchConsole(client, lambda: "test-token")
            if accepted:
                rows, _ = await provider.fetch_day(date(2026, 10, 20), "daily")
                assert rows[0]["position"] is None and rows[0]["impressions"] == 0
            else:
                with pytest.raises(GscError, match="invalid_response"):
                    await provider.fetch_day(date(2026, 10, 20), "daily")

    asyncio.run(run())


def test_explicit_google_zero_row_counts_as_reported_zero_not_missing():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.schemas.google_search_console import GoogleSourceRow
    from app.services.seo_service import report_from_snapshots

    day = date(2026, 10, 20)
    row = GoogleSourceRow(key=day.isoformat(), clicks=0, impressions=0, position=0)
    snapshot = SimpleNamespace(
        start_date=day, end_date=day, dimension="daily", rows=[row.model_dump()], updated_at=datetime.now(UTC)
    )
    report = report_from_snapshots([snapshot], "google", day, day, {}, True)
    assert report["totals"] == {"clicks": 0, "impressions": 0, "ctr": 0, "position": None}
    assert report["coverage"]["daily"]["status"] == "complete"
