"""HTTP boundary behavior of the shared private image-to-prompt API."""

import asyncio

import httpx
import pytest

from app.main import app
from app.utils.deps import get_current_user


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("multipart/form-data", b"broken"),
        ("multipart/form-data; boundary=test", b"broken"),
    ],
)
def test_invalid_multipart_is_a_safe_client_error(content_type, body):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/api/prompt-assets", content=body, headers={"Content-Type": content_type}
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "INVALID_REQUEST"

    asyncio.run(run())


def test_private_prompt_api_requires_auth_and_uses_envelope():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for path in ("/api/prompt-analyses", "/api/prompt-assets/00000000-0000-0000-0000-000000000001"):
                response = await client.get(path)
                assert response.status_code == 401, response.text
                assert response.json()["success"] is False
                assert response.json()["error"]["code"] == "UNAUTHENTICATED"
                assert response.json()["request_id"]
                assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


def test_private_json_limit_is_checked_before_body_parsing():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/api/prompt-analyses",
                content=b"x" * (128 * 1024 + 1),
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 413, response.text
            assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    asyncio.run(run())


def test_legacy_contracts_are_unchanged():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            response = await client.post("/api/auth/wechat", json={"code": ""})
            assert response.status_code == 422
            assert "detail" in response.json() and "success" not in response.json()

    asyncio.run(run())


def test_chunked_upload_cannot_bypass_json_size_limit():
    async def chunks():
        for _ in range(9):
            yield b"x" * (16 * 1024)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/prompt-analyses", content=chunks())
            assert response.status_code == 413
            assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    asyncio.run(run())


@pytest.mark.parametrize("length", ["-1", "broken"])
def test_invalid_body_length_is_rejected(length):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/api/prompt-analyses", content=b"{}", headers={"Content-Length": length}
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "INVALID_REQUEST"

    asyncio.run(run())


def test_invalid_json_is_safe_and_does_not_echo_input():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/api/prompt-analyses",
                content=b'{"sensitive-marker"',
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
            assert "sensitive-marker" not in response.text

    asyncio.run(run())


def test_unexpected_error_does_not_disclose_private_exception(monkeypatch, caplog):
    async def broken_user():
        raise RuntimeError("secret-upstream-response")

    monkeypatch.setitem(app.dependency_overrides, get_current_user, broken_user)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/api/prompt-analyses")
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "INTERNAL_ERROR"
            assert "secret-upstream-response" not in response.text

    asyncio.run(run())
    assert "secret-upstream-response" not in caplog.text
