"""External runner bridge. stdout contains bounded report data/status, never credentials."""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime

import httpx

from app.config import get_settings
from app.services.google_search_console import GoogleSearchConsole, GscError, ServiceAccountToken
from app.services.seo_bundle_service import (
    MAX_BUNDLE_BYTES,
    export_bundle,
    import_bundle,
    record_external_failure,
)
from app.services.seo_sync_service import SAFE_ERRORS, sync_status


async def execute(args):
    if args.command == "export":
        filename = get_settings().gsc_credentials_file
        if not filename:
            raise GscError("credentials_unavailable")
        async with httpx.AsyncClient() as client:
            raw = await export_bundle(
                GoogleSearchConsole(client, ServiceAccountToken(filename)), previous=args.data_through
            )
        sys.stdout.buffer.write(raw + b"\n")
        return
    from app.database import AsyncSessionLocal, engine

    engine.echo = False
    if args.command == "failure":
        try:
            attempt = datetime.fromisoformat(args.attempt_at)
        except ValueError:
            raise GscError("invalid_bundle") from None
        recorded = await record_external_failure(AsyncSessionLocal, attempt, args.code)
        print(json.dumps({"recorded": recorded}, separators=(",", ":")))
        return
    if args.command == "import":
        raw = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
        result = await import_bundle(AsyncSessionLocal, raw)
        if result != "success":
            raise GscError(result if result == "busy" else "sync_failed")
    async with AsyncSessionLocal() as db:
        status = await sync_status(db)
    output = (
        {"status": "success", "data_through": status["data_through"]} if args.command == "import" else status
    )
    print(json.dumps(output, default=lambda value: value.isoformat(), separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--data-through", type=date.fromisoformat)
    sub.add_parser("status")
    sub.add_parser("import")
    failure = sub.add_parser("failure")
    failure.add_argument("--attempt-at", required=True)
    failure.add_argument("--code", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(execute(args))
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, GscError)
            else "timeout"
            if isinstance(error, TimeoutError)
            else "sync_failed"
        )
        print(
            code if code in SAFE_ERRORS | {"external_not_configured", "busy"} else "sync_failed",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
