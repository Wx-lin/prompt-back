import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings
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

    def verify(token, transport, audience, *, certs_url):
        assert certs_url == "https://www.googleapis.com/oauth2/v1/certs"
        transport("https://www.googleapis.com/oauth2/v1/certs")

    monkeypatch.setattr(google_auth.google_requests.Request, "__call__", request)
    monkeypatch.setattr(google_auth.id_token, "verify_token", verify)
    with pytest.raises(google_auth.GoogleTokenInvalidError):
        google_auth.verify_google_id_token("test")
    assert calls and 0 < calls[0] <= 10


def test_google_verification_uses_configured_https_cert_endpoint(monkeypatch):
    captured = {}

    def verify(_token, _transport, _audience, *, certs_url):
        captured["certs_url"] = certs_url
        return {"sub": "google-user", "iss": "https://accounts.google.com"}

    monkeypatch.setattr(
        google_auth,
        "get_settings",
        lambda: SimpleNamespace(
            google_client_id="client-id",
            google_certs_url="https://aigc-google-certs.pages.dev/oauth2/v1/certs",
        ),
    )
    monkeypatch.setattr(google_auth.id_token, "verify_token", verify)

    assert google_auth.verify_google_id_token("test") == {
        "sub": "google-user",
        "iss": "https://accounts.google.com",
    }
    assert captured["certs_url"] == "https://aigc-google-certs.pages.dev/oauth2/v1/certs"


def test_google_verification_rejects_wrong_issuer(monkeypatch):
    monkeypatch.setattr(
        google_auth.id_token,
        "verify_token",
        lambda *_args, **_kwargs: {"sub": "google-user", "iss": "https://attacker.example"},
    )

    with pytest.raises(google_auth.GoogleTokenInvalidError, match="issuer"):
        google_auth.verify_google_id_token("test")


def test_google_certificate_endpoint_rejects_plain_http():
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(google_certs_url="http://example.com/oauth2/v1/certs")


@pytest.mark.asyncio
async def test_google_network_failure_returns_retryable_error(monkeypatch):
    def unavailable(_token):
        raise google_auth.GoogleVerificationUnavailableError("network unavailable")

    monkeypatch.setattr(auth, "verify_google_id_token", unavailable)
    with pytest.raises(HTTPException) as exc:
        await auth.google_login(GoogleLoginRequest(id_token="test"), None)
    assert exc.value.status_code == 503
