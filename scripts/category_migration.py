"""Offline preview first; explicit commands for audited export/apply/rollback."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services import category_migration_service as service


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("preview", "export", "apply", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        if name in ("preview", "apply"):
            command.add_argument("--snapshot", type=Path, required=True)
            command.add_argument("--review", type=Path, required=True)
        if name in ("apply", "rollback"):
            command.add_argument("--batch-id", required=True)
        if name != "preview":
            command.add_argument(
                "--expected-target",
                required=True,
                help="Exact database host:port/name; credentials are read from configured environment",
            )
    return result


def load(path):
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("Audit input exceeds 50 MiB")
    return json.loads(path.read_text())


def private_output(path):
    # Reserve before any database action; never overwrite an existing audit log.
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")


async def run(args):
    plan = (
        service.prepare_plan(load(args.snapshot), load(args.review))
        if args.command in ("preview", "apply")
        else None
    )
    if args.command == "preview":
        return plan
    url = make_url(get_settings().database_url)
    target = f"{url.host or 'localhost'}:{url.port or 5432}/{url.database}"
    if target != args.expected_target:
        raise ValueError("Configured database differs from --expected-target")
    engine = create_async_engine(url, echo=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            if args.command == "export":
                async with db.begin():
                    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                    return await service.export_snapshot(db)
            if args.command == "apply":
                return await service.apply_plan(db, plan, args.batch_id)
            return await service.rollback_batch(db, args.batch_id)
    finally:
        await engine.dispose()


def main():
    args = parser().parse_args()
    try:
        with private_output(args.output) as output:
            try:
                result = asyncio.run(run(args))
            except Exception as exc:
                # SQLAlchemy failures can contain credentials/SQL parameters.
                json.dump(
                    {
                        "command": args.command,
                        "failed": type(exc).__name__,
                        "batch_id": getattr(args, "batch_id", None),
                    },
                    output,
                )
                raise
            json.dump(result, output, ensure_ascii=False, indent=2)
        summary = (
            result.get("summary", result)
            if args.command != "export"
            else {key: len(value) for key, value in result.items()}
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"{type(exc).__name__}: audit operation failed; check input, target and output file")
        return 1
    except Exception as exc:
        print(
            f"{type(exc).__name__}: database operation failed; "
            "inspect the private audit output and transactional journal"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
