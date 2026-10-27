"""CLI produces only source/status JSON or fixed failure categories."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.google_search_console import GscError
from scripts import seo_sync


class Output:
    def __init__(self):
        self.buffer = BytesIO()


def test_export_has_no_token_or_database_queries(monkeypatch):
    output = Output()
    monkeypatch.setattr(seo_sync.sys, "stdout", output)
    monkeypatch.setattr(
        seo_sync, "get_settings", lambda: SimpleNamespace(gsc_credentials_file="/private/key.json")
    )
    export = AsyncMock(return_value=b'{"version":1}')
    monkeypatch.setattr(seo_sync, "export_bundle", export)
    asyncio.run(seo_sync.execute(SimpleNamespace(command="export", data_through=None)))
    assert output.buffer.getvalue() == b'{"version":1}\n'
    assert export.call_args.args[0].token.filename == "/private/key.json"


def test_import_reads_bounded_stdin_and_outputs_status(monkeypatch):
    from app import database

    @asynccontextmanager
    async def sessions():
        yield object()

    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(seo_sync.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"{}")))
    imported = AsyncMock(return_value="success")
    monkeypatch.setattr(seo_sync, "import_bundle", imported)
    monkeypatch.setattr(seo_sync, "sync_status", AsyncMock(return_value={"data_through": date(2026, 10, 20)}))
    output = StringIO()
    monkeypatch.setattr(seo_sync.sys, "stdout", output)
    asyncio.run(seo_sync.execute(SimpleNamespace(command="import")))
    assert output.getvalue().strip() == '{"status":"success","data_through":"2026-10-20"}'
    assert imported.call_args.args[1] == b"{}"
    imported.return_value = "busy"
    with pytest.raises(GscError, match="busy"):
        asyncio.run(seo_sync.execute(SimpleNamespace(command="import")))


def test_main_error_never_prints_exception_text(monkeypatch):
    monkeypatch.setattr(seo_sync.sys, "argv", ["seo_sync", "export"])
    monkeypatch.setattr(seo_sync, "execute", AsyncMock(side_effect=RuntimeError("private-key-upstream-body")))
    output = StringIO()
    monkeypatch.setattr(seo_sync.sys, "stderr", output)
    with pytest.raises(SystemExit) as error:
        seo_sync.main()
    assert error.value.code == 1 and output.getvalue() == "sync_failed\n"


def test_failure_cli_reports_only_guarded_recording_result(monkeypatch):
    recorded = AsyncMock(return_value=False)
    monkeypatch.setattr(seo_sync, "record_external_failure", recorded)
    output = StringIO()
    monkeypatch.setattr(seo_sync.sys, "stdout", output)
    asyncio.run(
        seo_sync.execute(
            SimpleNamespace(
                command="failure", attempt_at="2026-10-27T12:00:00+00:00", code="upstream_unavailable"
            )
        )
    )
    assert output.getvalue() == '{"recorded":false}\n'
    assert recorded.call_args.args[1].tzinfo is not None
    with pytest.raises(GscError, match="invalid_bundle"):
        asyncio.run(
            seo_sync.execute(
                SimpleNamespace(command="failure", attempt_at="not-a-date", code="upstream_unavailable")
            )
        )
