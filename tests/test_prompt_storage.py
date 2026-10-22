"""Private signing and object reads, with no remote COS requests."""

import asyncio
import io
import logging
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.services import cos_service
from app.services.prompt_analysis import storage
from app.services.prompt_analysis.errors import AnalysisError


@pytest.fixture
def configured(monkeypatch):
    settings = get_settings()
    for key, value in {
        "cos_secret_id": "test-id",
        "cos_secret_key": "test-secret",
        "cos_bucket": "test-1250000000",
    }.items():
        monkeypatch.setattr(settings, key, value)


def test_private_signatures_never_enter_debug_logs(configured, caplog):
    with caplog.at_level(logging.DEBUG):
        url = storage.preview_url("prompt-analysis/private-log-marker/image.jpg")
    assert "q-signature=" in url
    assert "private-log-marker" not in caplog.text
    assert "q-signature=" not in caplog.text
    assert "sign_key" not in caplog.text


def test_private_read_is_bounded_and_closes_stream(configured, monkeypatch):
    data = io.BytesIO(b"x" * (2 * 1024 * 1024 + 1))
    client = SimpleNamespace(get_object=lambda **_: {"Body": SimpleNamespace(get_raw_stream=lambda: data)})
    monkeypatch.setattr(cos_service, "_build_client", lambda: client)
    with pytest.raises(ValueError):
        asyncio.run(storage.read_private("private-test-key"))
    assert data.closed


@pytest.mark.parametrize("missing", ["cos_secret_id", "cos_secret_key", "cos_bucket"])
def test_missing_private_storage_is_explicit(configured, monkeypatch, missing):
    monkeypatch.setattr(get_settings(), missing, "")
    with pytest.raises(AnalysisError) as error:
        storage.require_storage_configuration()
    assert error.value.code == "STORAGE_UNAVAILABLE"


def test_private_operations_keep_acl_and_filter_thread_logs(configured, monkeypatch, caplog):
    calls = []

    def put(**kwargs):
        logging.getLogger("qcloud_cos.cos_client").info("private-thread-marker %s", kwargs["Key"])
        calls.append(kwargs)

    monkeypatch.setattr(cos_service, "_build_client", lambda: SimpleNamespace(put_object=put))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(storage.put_private(b"image", "private-thread-marker"))
    assert calls[0]["ACL"] == "private"
    assert calls[0]["Body"] == b"image"
    assert "private-thread-marker" not in caplog.text


def test_private_read_returns_bytes_and_closes_stream(configured, monkeypatch):
    stream = io.BytesIO(b"stored-image")
    client = SimpleNamespace(get_object=lambda **_: {"Body": SimpleNamespace(get_raw_stream=lambda: stream)})
    monkeypatch.setattr(cos_service, "_build_client", lambda: client)
    assert asyncio.run(storage.read_private("test-key")) == b"stored-image"
    assert stream.closed
