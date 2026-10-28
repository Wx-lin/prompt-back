"""Forward/backward migration safety against the isolated real PostgreSQL schema."""

import os
import runpy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import response_data
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis test URLs required",
    ),
]


async def test_conversation_migration_preserves_legacy_and_refuses_history_loss(api, sessions):
    migration = runpy.run_path(
        str(Path(__file__).parents[1] / "alembic/versions/27f86a5d9c10_prompt_conversations.py")
    )

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            migration[direction]()

    async with sessions() as db:
        connection = await db.connection()
        await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
        old_columns = await connection.run_sync(
            lambda conn: {c["name"] for c in inspect(conn).get_columns("prompt_analyses")}
        )
        assert "conversation_id" not in old_columns and "idea_text" in old_columns
        await connection.run_sync(lambda conn: migrate(conn, "upgrade"))
        columns = await connection.run_sync(
            lambda conn: {c["name"] for c in inspect(conn).get_columns("prompt_analyses")}
        )
        assert {
            "conversation_id",
            "parent_analysis_id",
            "conversation_turn",
            "context_snapshot",
            "user_message",
        } <= columns
        await db.commit()
    first = response_data(await create_conversation(api), 202)
    async with sessions() as db:
        connection = await db.connection()
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
        await db.rollback()
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    async with sessions() as db:
        connection = await db.connection()
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
