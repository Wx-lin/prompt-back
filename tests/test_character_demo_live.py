import base64
import io

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from scripts.character_demo_live import _get_key, _images, app


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    _images.clear()
    monkeypatch.setattr("scripts.character_demo_live._get_key", lambda: "test-key")
    monkeypatch.setattr("scripts.character_demo_live._last_requests", [])


@pytest.mark.asyncio
async def test_generates_and_edits_without_exposing_key_or_provider_url(monkeypatch, tmp_path):
    requests = []
    (tmp_path / "character_01_urban_woman.png").write_bytes(image_bytes())
    monkeypatch.setattr("scripts.character_demo_live.ROLE_DIRECTORY", tmp_path)

    async def fake_generate(payload, key):
        requests.append(payload)
        assert key == "test-key"
        return image_bytes()

    monkeypatch.setattr("scripts.character_demo_live._call_provider", fake_generate)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 3210)),
        base_url="http://127.0.0.1:3139",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        data = {"role": "都市女性", "hair": "齐肩短发", "clothing": "粉色裙子", "expression": "微笑"}
        first = await client.post("/generate", json=data)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        result = first.json()
        assert result["image"].startswith("data:image/png;base64,")
        assert base64.b64decode(result["image"].split(",")[1]) == image_bytes()
        assert "test-key" not in first.text
        second = await client.post("/generate", json={**data, "hair": "长直发", "previous_id": result["id"]})
        assert second.status_code == 200
        assert requests[0]["reference"].startswith(b"\x89PNG")
        assert "全身" in requests[0]["prompt"]
        assert requests[1]["reference"] == image_bytes()
        changed_role = await client.post(
            "/generate", json={**data, "role": "都市男性", "previous_id": result["id"]}
        )
        assert changed_role.status_code == 404
        assert len(requests) == 2


@pytest.mark.asyncio
async def test_rejects_cross_origin_and_invalid_input_without_call(monkeypatch):
    async def fail(*args):
        pytest.fail("invalid requests must not call the provider")

    monkeypatch.setattr("scripts.character_demo_live._call_provider", fail)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 3210)), base_url="http://127.0.0.1:3139"
    ) as client:
        data = {"role": "都市女性", "hair": "b", "clothing": "c", "expression": "d"}
        assert (
            await client.post("/generate", headers={"Origin": "https://evil.example"}, json=data)
        ).status_code == 403
        assert (
            await client.post(
                "/generate",
                headers={"Origin": "http://localhost:3000"},
                json={**data, "url": "http://127.0.0.1"},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/generate",
                headers={"Origin": "http://localhost:3000"},
                json={**data, "previous_id": "unknown_identifier_for_test"},
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_rejects_remote_client_and_spoofed_host():
    data = {"role": "都市女性", "hair": "b", "clothing": "c", "expression": "d"}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.1.2", 1234)),
        base_url="http://127.0.0.1:3139",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        assert (await client.post("/generate", json=data)).status_code == 403
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="http://evil.example:3139",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        assert (await client.post("/generate", json=data)).status_code == 403


@pytest.mark.asyncio
async def test_openai_edit_multipart_request_and_base64_response(monkeypatch):
    from scripts.character_demo_live import MODEL, _call_provider

    original_client = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.openai.com/v1/images/edits"
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'name="image[]"' in request.content
        assert b"image/png" in request.content
        assert MODEL.encode() in request.content
        assert image_bytes() in request.content
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})

    monkeypatch.setattr(
        "scripts.character_demo_live.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert await _call_provider({"reference": image_bytes(), "prompt": "微笑"}, "test-key") == image_bytes()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"data": [{"url": "http://127.0.0.1/private"}]},
        {"data": [{"b64_json": "not valid base64!"}]},
        {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
        {"data": []},
    ],
)
async def test_provider_rejects_invalid_results_without_fetching_urls(monkeypatch, result):
    from scripts.character_demo_live import DemoError, _call_provider

    original_client = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=result)

    monkeypatch.setattr(
        "scripts.character_demo_live.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(DemoError):
        await _call_provider({"reference": image_bytes(), "prompt": "微笑"}, "test-key")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_rejects_oversized_response(monkeypatch):
    import scripts.character_demo_live as demo

    original_client = httpx.AsyncClient
    monkeypatch.setattr(demo, "MAX_PROVIDER_BODY", 32)
    monkeypatch.setattr(
        demo.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 33)), **kwargs
        ),
    )
    with pytest.raises(demo.DemoError, match="图片服务返回异常"):
        await demo._call_provider({"reference": image_bytes(), "prompt": "微笑"}, "test-key")


def test_key_comes_only_from_openai_configuration(monkeypatch):
    import scripts.character_demo_live as demo

    monkeypatch.setattr(
        demo, "dotenv_values", lambda path: {"DASHSCOPE_API_KEY": "old", "OPENAI_API_KEY": "file-key"}
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "old-env-key")
    assert _get_key() == "file-key"
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert _get_key() == "env-key"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(demo, "dotenv_values", lambda path: {"DASHSCOPE_API_KEY": "old"})
    assert _get_key() == ""


@pytest.mark.asyncio
async def test_previous_image_expires_before_provider_call(monkeypatch):
    import scripts.character_demo_live as demo

    async def fail(*args):
        pytest.fail("expired references must not call the provider")

    monkeypatch.setattr(demo, "_call_provider", fail)
    _images["expired_identifier_for_test"] = (
        demo.time.monotonic() - demo.TTL_SECONDS - 1,
        image_bytes(),
        "都市女性",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 3210)),
        base_url="http://127.0.0.1:3139",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        response = await client.post(
            "/generate",
            json={
                "role": "都市女性",
                "hair": "短发",
                "clothing": "西装",
                "expression": "微笑",
                "previous_id": "expired_identifier_for_test",
            },
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_provider_http_error_is_reported_without_retry_or_sensitive_details(monkeypatch):
    calls = []
    original_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "sensitive vendor error"})

    monkeypatch.setattr(
        "scripts.character_demo_live.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    from scripts.character_demo_live import DemoError, _call_provider

    with pytest.raises(DemoError, match="模型调用失败") as exc:
        await _call_provider({"reference": image_bytes(), "prompt": "微笑"}, "secret-key-for-test")
    assert "secret-key-for-test" not in str(exc.value)
    assert "sensitive vendor error" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_rejects_unknown_role_before_provider_request(monkeypatch):
    async def fail(*args):
        pytest.fail("unrecognized role must not call the provider")

    monkeypatch.setattr("scripts.character_demo_live._call_provider", fail)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 3210)),
        base_url="http://127.0.0.1:3139",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        data = {"role": "../../private", "hair": "短发", "clothing": "西装", "expression": "微笑"}
        assert (await client.post("/generate", json=data)).status_code == 422
