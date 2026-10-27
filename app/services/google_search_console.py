"""Read-only Search Console transport for the single production URL-prefix property."""

import asyncio
import inspect
import json
from datetime import date
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from pydantic import ValidationError

from app.schemas.google_search_console import GoogleSourceRow

SITE_URL = "https://aigc-prompt.com/"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
ENDPOINT = (
    "https://www.googleapis.com/webmasters/v3/sites/https%3A%2F%2Faigc-prompt.com%2F/searchAnalytics/query"
)


class GscError(Exception):
    """Only fixed, non-sensitive error codes can leave this boundary."""


class ServiceAccountToken:
    def __init__(self, filename):
        self.filename = filename
        self.credentials = None

    async def __call__(self):
        def refresh():
            try:
                if self.credentials is None:
                    info = json.loads(Path(self.filename).read_text())
                    if (
                        info.get("type") != "service_account"
                        or info.get("token_uri") != "https://oauth2.googleapis.com/token"
                    ):
                        raise ValueError("Unsupported credential type")
                    self.credentials = service_account.Credentials.from_service_account_info(
                        info, scopes=[SCOPE]
                    )
                if not self.credentials.valid:
                    request = Request()
                    self.credentials.refresh(lambda **kwargs: request(**{**kwargs, "timeout": 20}))
                return self.credentials.token
            except Exception:
                raise GscError("credentials_unavailable") from None

        return await asyncio.to_thread(refresh)


class GoogleSearchConsole:
    def __init__(self, client, token, *, page_size=25000, max_rows=50000, sleep=asyncio.sleep):
        self.client, self.token = client, token
        self.page_size, self.max_rows, self.sleep = page_size, max_rows, sleep

    async def _request(self, body):
        token = self.token()
        if inspect.isawaitable(token):
            token = await token
        for attempt in range(3):
            try:
                async with self.client.stream(
                    "POST",
                    ENDPOINT,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (401, 403):
                        raise GscError("permission_denied")
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.TransportError("retryable")
                    if response.status_code != 200:
                        raise GscError("upstream_rejected")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > 20_000_000:
                            raise GscError("invalid_response")
                        content.extend(chunk)
                    return json.loads(content)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == 2:
                    raise GscError("upstream_unavailable") from None
                await self.sleep(2**attempt)
            except (ValueError, TypeError):
                raise GscError("invalid_response") from None

    async def fetch_day(self, day: date, dimension: str):
        dimensions = {"daily": ["date"], "query": ["query"], "page": ["page"]}[dimension]
        rows = []
        while len(rows) < self.max_rows:
            limit = min(self.page_size, self.max_rows - len(rows))
            body = {
                "startDate": day.isoformat(),
                "endDate": day.isoformat(),
                "dimensions": dimensions,
                "type": "web",
                "dataState": "final",
                "aggregationType": "byPage" if dimension == "page" else "byProperty",
                "rowLimit": limit,
                "startRow": len(rows),
            }
            data = await self._request(body)
            try:
                incoming = data.get("rows", [])
                if not isinstance(incoming, list) or len(incoming) > limit:
                    raise ValueError()
                parsed = []
                for raw in incoming:
                    keys = raw["keys"]
                    if len(keys) != 1 or not isinstance(keys[0], str):
                        raise ValueError()
                    if dimension == "daily" and keys[0] != day.isoformat():
                        raise ValueError()
                    row = GoogleSourceRow(
                        key=keys[0],
                        clicks=self._count(raw["clicks"]),
                        impressions=self._count(raw["impressions"]),
                        position=raw.get("position"),
                    )
                    if dimension == "page" and not row.key.startswith(SITE_URL):
                        raise ValueError()
                    if dimension == "query" and (len(row.key) > 500 or not row.key.strip()):
                        raise ValueError()
                    parsed.append(row.model_dump())
                rows.extend(parsed)
                if len({row["key"] for row in rows}) != len(rows):
                    raise ValueError()
                if len(incoming) < limit:
                    return rows, False
            except (ValueError, TypeError, KeyError, ValidationError, AttributeError):
                raise GscError("invalid_response") from None
        return rows, True

    @staticmethod
    def _count(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            raise ValueError()
        return int(value)
