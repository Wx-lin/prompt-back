"""
Alembic 迁移环境配置。

关键改动（相对 alembic init 生成的默认模板）：
1. target_metadata 指向 app.database.Base.metadata，并提前 import app.models
   确保所有 ORM 模型被注册，否则 autogenerate 识别不到新建的表。
2. 数据库连接地址从 app.config.get_settings() 读取，不写死在 alembic.ini 里，
   保证只有一处配置来源（.env），避免两边配置不一致导致的迁移错误。
3. 使用异步引擎（create_async_engine + run_sync），因为项目全程使用
   SQLAlchemy 异步 API，迁移环境也要配套异步驱动才能连上同一个数据库。
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 必须先 import，确保 app.models.__init__ 里的所有模型完成注册，
# 否则 Base.metadata 里不会包含这些表，autogenerate 就检测不到它们。
import app.models  # noqa: F401
from alembic import context
from app.config import get_settings
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# 用项目统一配置覆盖 alembic.ini 里的占位符，保证配置来源只有 .env 这一处
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 脚本，不连接真实数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：用异步引擎连接真实数据库执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
