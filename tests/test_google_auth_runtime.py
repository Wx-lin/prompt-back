import asyncio
import time

import pytest
from fastapi import HTTPException

from app.routers import auth
from app.schemas.auth import GoogleLoginRequest
from app.utils import google_auth


@pytest.mark.asyncio
async def test_google_verification_does_not_block_other_requests(monkeypatch):
    def slow_verifier(_token):
        time.sleep(0.3)
        raise google_auth.GoogleTokenInvalidError("invalid")

    monkeypatch.setattr(auth, "verify_google_id_token", slow_verifier)
    started = time.monotonic()
    login = asyncio.create_task(auth.google_login(GoogleLoginRequest(id_token="test"), None))
    await asyncio.sleep(0.02)
    elapsed = time.monotonic() - started
    with pytest.raises(HTTPException) as exc:
        await login
    assert exc.value.status_code == 400
    assert elapsed < 0.15


def test_google_certificate_transport_has_bounded_timeout(monkeypatch):
    calls = []

    def request(_self, _url, **kwargs):
        calls.append(kwargs["timeout"])
        raise ValueError("test")

    def verify(token, transport, audience):
        transport("https://www.googleapis.com/oauth2/v1/certs")

    monkeypatch.setattr(google_auth.google_requests.Request, "__call__", request)
    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token", verify)
    with pytest.raises(google_auth.GoogleTokenInvalidError):
        google_auth.verify_google_id_token("test")
    assert calls and 0 < calls[0] <= 10


@pytest.mark.asyncio
async def test_google_network_failure_returns_retryable_error(monkeypatch):
    def unavailable(_token):
        raise google_auth.GoogleVerificationUnavailableError("network unavailable")

    monkeypatch.setattr(auth, "verify_google_id_token", unavailable)
    with pytest.raises(HTTPException) as exc:
        await auth.google_login(GoogleLoginRequest(id_token="test"), None)
    assert exc.value.status_code == 503
