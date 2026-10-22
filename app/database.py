"""
数据库连接模块（异步 SQLAlchemy）。

提供：
1. 全局异步 engine 和 session 工厂
2. ORM 模型基类 Base
3. FastAPI 依赖注入函数 get_db，供路由层通过 Depends(get_db) 获取数据库会话
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# Private prompts must not leak through SQL parameter logging, including locally.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,  # 每次取连接前先探活，避免连接池里拿到已失效的连接
    pool_size=5,
    max_overflow=0,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # commit 后不自动过期对象属性，避免后续访问时意外触发额外查询
)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类，alembic 的 autogenerate 依赖这个 metadata。"""

    pass


async def get_db() -> AsyncGenerator[AsyncSession]:
    """
    FastAPI 依赖注入函数：每个请求创建一个独立的数据库会话，请求结束后自动关闭。

    用法：
        @router.get("/xxx")
        async def handler(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        yield session
